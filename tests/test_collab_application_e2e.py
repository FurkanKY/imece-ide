"""Collaboration application E2E: real bridge, real host, real worker lifecycle.

Everything on the collaboration side is REAL here: a temporary Git source
checkout, a real loopback HTTP/SSE server over a real Git-backed metadata
store, the real ``LoopbackSnapshotClient``, the real host-owned
``RevisionConsumer``, the real private POSIX cursor namespace with its OS
lease, and the real ``CollaborationHost`` injected into ``webhost.state``.

The host is driven ONLY through its public bridge surface:
``collab.preview`` / ``collab.approve`` / ``collab.discard`` /
``collab.status`` and ``run.start`` / ``run.followUp`` / ``run.cancel`` /
``run.applyProposals`` / ``run.rejectProposals`` on a headless ``HostBridge``
driven by one reused ``QCoreApplication``.

Only the transport-less seams are fakes: model providers are scripted
backends and the verification process runner is a fake. ``engine_factory`` is
the REAL factory (real routing/preflight, the real
``NativeWorkerAttemptAdapter`` with ``worker_safe_point`` taken from the host
session), so the collaboration safe point is only ever reached through
production code paths.

The shared task goal deliberately contains no configuration token, so every
"the credential never appears" assertion below is exact.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication  # noqa: E402

import engine_factory  # noqa: E402
import runtime_paths  # noqa: E402
import ui_prefs  # noqa: E402
import webhost.api.collab  # noqa: F401,E402  (registers ONLY the collab handlers)
import webhost.api.run as run_api  # noqa: E402
from agent_runtime import (  # noqa: E402
    ModelStopReason, ModelToolCall, ModelTurn, ModelUsage, UserInput,
)
from collab_runtime.client import LoopbackSnapshotClient  # noqa: E402
from collab_runtime.context import SNAPSHOT_SECTION_HEADER  # noqa: E402
from collab_runtime.coordinator import Coordinator  # noqa: E402
from collab_runtime.host import (  # noqa: E402
    CollaborationHost, HostCollaborationError, _CheckpointLease,
)
from collab_runtime.models import (  # noqa: E402
    build_context, build_initial_state, build_task,
)
from collab_runtime.store import GitStore  # noqa: E402
from engine_factory import PipelinePorts  # noqa: E402
from executor_runtime.native_verification import NativeVerificationAttemptAdapter  # noqa: E402
from run_runtime import RunRuntime, RunStatus, RunStore  # noqa: E402
from run_runtime.events import RunEventType  # noqa: E402
from webhost import state  # noqa: E402
from webhost.bridge import HostBridge  # noqa: E402
from test_collab_transport import servers  # noqa: F401,E402  (listener registry fixture)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git yok")

SESSION_ID = "app-e2e"
TARGET_VERSION = "app e2e v1"
TASK_ID = "app-task"
MEMBER = "alice"
# A distinctive, valid loopback member credential: [A-Za-z0-9_-]{32,256}.
CREDENTIAL = "e2e-credential-" + "k" * 40
TASK_TEXT = "Fix the bug in a.txt"
PLAN_SUMMARY = "Step one: correct the body of a.txt."
PLAN_JSON = (
    '{"summary":"' + PLAN_SUMMARY + '","steps":[{"title":"Step 1","objective":"Fix it."}],'
    '"acceptance_criteria":["a.txt says fixed"],"risks":[],'
    '"task_profile":{"complexity":"LOW","scope":"LOCAL"}}'
)
REVIEW_APPROVED = '{"verdict":"APPROVED","summary":"Good fix.","findings":[]}'
# Routing uses ids that exist in the REAL provider catalog.
ROUTING = {"planner": "gemini", "coder": "deepseek", "reviewer": "openai"}
PLANNER_PROVIDER, WORKER_PROVIDER, REVIEWER_PROVIDER = "gemini", "deepseek", "openai"


# --------------------------------------------------------------- small helpers


def _completed(text: str) -> ModelTurn:
    return ModelTurn(text, (), ModelStopReason.COMPLETED, ModelUsage())


def _write_turn(path: str, content: str) -> ModelTurn:
    return ModelTurn("", (ModelToolCall("c1", "write_file", {"path": path, "content": content}),),
                     ModelStopReason.TOOL_USE, ModelUsage())


def _process_result(exit_code: int):
    from process_runtime.models import ProcessResult

    return ProcessResult(
        argv=("true",), cwd=".", exit_code=exit_code, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
        stdout_bytes=0, stderr_bytes=0,
    )


def _git(args, cwd) -> str:
    done = subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _pump(predicate, timeout=25.0) -> bool:
    """Pump the Qt queue from the MAIN thread while polling."""
    app = QCoreApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _poll(predicate, timeout=25.0) -> bool:
    """Plain worker-thread-safe polling (never touches the Qt queue)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def rpc(bridge, method, params=None, call_id=1, timeout=25.0) -> dict:
    """One bounded async RPC: exact call id, then disconnect the reply slot."""
    seen: list[dict] = []

    def on_reply(raw: str) -> None:
        payload = json.loads(raw)
        if payload.get("id") == call_id:
            seen.append(payload)

    bridge.reply.connect(on_reply)
    try:
        bridge.call(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        assert _pump(lambda: bool(seen), timeout), f"{method}: yanıt gelmedi"
    finally:
        bridge.reply.disconnect(on_reply)
    return seen[0]


# ------------------------------------------------------- scripted model fakes


class RecordingBackend:
    """A scripted ModelBackend that records EVERY real session/turn/input.

    ``gate(attempt, turn, text)`` runs on the pipeline worker thread just
    before that turn's answer is produced, so a test can publish server-side
    revisions or hold the turn without touching a production seam.
    """

    def __init__(self, turns, gate=None):
        self._turns = list(turns)
        self._gate = gate
        self.sessions = 0
        self.responds: list[tuple[int, int, str]] = []
        self.instructions: list[str] = []
        self._first_text: dict[int, str] = {}

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        self.sessions += 1
        attempt = self.sessions
        self.instructions.append(instructions)
        owner = self

        class Session:
            def __init__(self):
                self.turn = 0

            def respond(self, input_items):
                index = self.turn
                self.turn += 1
                user = next((i for i in input_items if isinstance(i, UserInput)), None)
                if user is not None:
                    owner._first_text.setdefault(attempt, user.text)
                text = user.text if user is not None else owner._first_text.get(attempt, "")
                owner.responds.append((attempt, index, text))
                if owner._gate is not None:
                    owner._gate(attempt, index, text)
                value = owner._turns.pop(0)
                if isinstance(value, BaseException):
                    raise value
                return value

        return Session()

    @property
    def inputs(self) -> list[str]:
        """The recorded prompt of each ATTEMPT, in attempt order."""
        return [self._first_text[key] for key in sorted(self._first_text)]

    @property
    def all_texts(self) -> list[str]:
        return [text for _, _, text in self.responds]


class _FakeRunner:
    def __init__(self, results):
        self._results = list(results)

    def run(self, workspace, request, *, cancel_token=None):
        return self._results.pop(0)


def make_ports_factory(*, planner, worker, reviewer, process_results, seen=None):
    """The REAL engine_factory with only the model/verification seams injected."""
    original = engine_factory.build_pipeline_ports
    backends = {PLANNER_PROVIDER: planner, WORKER_PROVIDER: worker, REVIEWER_PROVIDER: reviewer}

    def factory(runtime, run_id, routing, **kwargs):
        if seen is not None:
            seen.append(kwargs.get("worker_safe_point"))
        ports = original(runtime, run_id, routing,
                         backend_factory=lambda provider_id: backends[provider_id], **kwargs)
        return PipelinePorts(
            ports.planner, ports.worker, ports.reviewer,
            NativeVerificationAttemptAdapter(
                runtime, run_id, process_runner=_FakeRunner(list(process_results)),
            ),
            ports.change_provider,
        )

    return factory


def capture_sessions(monkeypatch) -> dict:
    """Observe the real session object BEFORE the QThread can reach the model."""
    captured: dict = {}
    original = run_api._start_pipeline_run

    def wrapper(*args, **kwargs):
        captured["session"] = kwargs.get("collab_session")
        return original(*args, **kwargs)

    monkeypatch.setattr(run_api, "_start_pipeline_run", wrapper)
    return captured


# ------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


def _make_source(root: Path, content="buggy\n") -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    _git(["config", "user.name", "T"], root)
    _git(["config", "user.email", "t@example.com"], root)
    (root / "a.txt").write_text(content, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "init"], root)
    return _git(["rev-parse", "HEAD"], root)


@pytest.fixture
def ctx(tmp_path, monkeypatch, qapp, servers):
    source = tmp_path / "source"
    head = _make_source(source)
    workspaces = tmp_path / "workspaces"
    monkeypatch.setattr(runtime_paths, "workspaces_dir", lambda: workspaces)
    monkeypatch.setattr(engine_factory, "workspaces_dir", lambda: workspaces)
    monkeypatch.setattr(ui_prefs, "load",
                        lambda: {**ui_prefs.DEFAULTS, "ai_engine": "auto", "decision_layer": "off"})
    runtime = RunRuntime(RunStore(tmp_path / "runtime.sqlite3"))
    state.set_project(str(source))
    state.set_run_runtime(runtime)

    trust = tmp_path / "private"
    trust.mkdir(mode=0o700)
    cursor_root = trust / "collab-cursors"
    host = CollaborationHost(cursor_root)
    state.set_collaboration_host(host)

    hub = GitStore.create_bare(tmp_path / "hub.git", what="hub")
    store = GitStore(store=GitStore.create_bare(tmp_path / "store.git"), remote=str(hub))
    revision = store.init_session(build_initial_state(
        session_id=SESSION_ID, target_version=TARGET_VERSION, base_commit=head,
    ))
    task_revision = revision
    revision = store.upsert_task(build_task(
        task_id=TASK_ID, owner=MEMBER, goal=TASK_TEXT, scopes=["a.txt"], status="running",
        context_revision=task_revision,
    ), expected_revision=revision)
    coordinator = Coordinator(store, session_id=SESSION_ID, owner_id=MEMBER,
                              member_credentials={MEMBER: CREDENTIAL})
    listener = servers(coordinator).start()

    created_runs: list[str] = []
    real_create_run = runtime.create_run

    def create_run(*args, **kwargs):
        run = real_create_run(*args, **kwargs)
        created_runs.append(run.run_id)
        return run

    monkeypatch.setattr(runtime, "create_run", create_run)
    bridge = HostBridge()
    events: list[dict] = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))

    def publish_context(goal: str, decisions=()) -> str:
        nonlocal revision
        revision = coordinator.update_context(
            CREDENTIAL, build_context(goal=goal, decisions=list(decisions), interfaces={}),
            expected_revision=revision,
        )
        return revision

    def publish_task(**overrides) -> str:
        nonlocal revision
        fields = {"task_id": TASK_ID, "owner": MEMBER, "goal": TASK_TEXT, "scopes": ["a.txt"],
                  "status": "running", "context_revision": task_revision}
        fields.update(overrides)
        current, session_state = store.fetch_state()
        revision = store.publish(session_state.with_task(build_task(**fields)),
                                 expected_revision=current)
        return revision

    ctx = SimpleNamespace(
        tmp=tmp_path, source=source, head=head, host=host, trust=trust, cursor_root=cursor_root,
        store=store, coordinator=coordinator, listener=listener, base_url=listener.base_url,
        runtime=runtime, bridge=bridge, events=events, release=threading.Event(),
        created_runs=created_runs, workspaces=workspaces,
        publish_context=publish_context, publish_task=publish_task,
    )
    _reset_active()
    try:
        yield ctx
    finally:
        ctx.release.set()
        _drain()


def _reset_active() -> None:
    run_api._active.update({
        "worker": None, "coordinator": None, "run_id": None, "proposals": [], "engine": "legacy",
        "workspace": None, "cancel_event": None, "pipeline_ports": None, "task": None,
        "plan_text": None, "pinned_paths": [], "decision_gate": None, "collab_session": None,
        "activity_streamer": None,
    })


def _drain() -> None:
    """Workers first, then the real shutdown path, then process-level state."""
    worker = run_api._active.get("worker")
    if worker is not None:
        _pump(lambda: not worker.isRunning(), 40)
        assert not worker.isRunning(), "test bitti ama pipeline worker hâlâ çalışıyor"
    run_api.shutdown()
    _pump(lambda: True, 0.2)
    for leftover in list(run_api._draining_workers):
        session, workspace = leftover[1], leftover[2]
        for close in (lambda: session.close(), lambda: workspace.dispose() if workspace else None):
            try:
                close()
            except Exception:
                pass
    run_api._draining_workers.clear()
    _reset_active()
    state._active = None
    state.set_collaboration_host(None)
    state.set_collaboration_status_cache(None)
    state.set_run_runtime(None)


# --------------------------------------------------- host-visible projections


def _cursor_files(ctx):
    return sorted(ctx.cursor_root.glob("*.json")) if ctx.cursor_root.exists() else []


def _lock_files(ctx):
    return sorted(ctx.cursor_root.glob("*.lock")) if ctx.cursor_root.exists() else []


def _consumed(ctx) -> str:
    files = _cursor_files(ctx)
    assert len(files) == 1, f"tam olarak bir özel imleç dosyası bekleniyordu: {files}"
    return json.loads(files[0].read_text(encoding="utf-8"))["consumed_revision"]


def _source_state(ctx):
    return (
        _git(["rev-parse", "HEAD"], ctx.source),
        _git(["status", "--porcelain"], ctx.source),
        (ctx.source / "a.txt").read_text(encoding="utf-8"),
    )


def _lock_is_free(ctx) -> bool:
    locks = _lock_files(ctx)
    if not locks:
        return True
    try:
        lease = _CheckpointLease(locks[0])
    except HostCollaborationError:
        return False
    lease.close()
    return True


def _secret_absent(ctx, *extra_texts) -> None:
    """The credential may not appear in ANY host-visible or stored surface."""
    blob = json.dumps(ctx.events, ensure_ascii=False) + "\n" + "\n".join(str(t) for t in extra_texts)
    assert CREDENTIAL not in blob, "kimlik bilgisi bir köprü yüzeyine sızdı"
    assert CREDENTIAL not in repr(run_api._active)
    if ctx.cursor_root.exists():
        for path in list(ctx.cursor_root.iterdir()):
            assert CREDENTIAL.encode("ascii") not in path.read_bytes(), path
    for name in ("runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3-journal"):
        candidate = ctx.tmp / name
        if candidate.exists():
            assert CREDENTIAL.encode("ascii") not in candidate.read_bytes(), name


def _approved(ctx, *, reset_cursor=False, call_id=1):
    """preview -> approve over the real bridge; returns (preview, approval)."""
    preview = rpc(ctx.bridge, "collab.preview", {
        "endpoint": ctx.base_url, "credential": CREDENTIAL,
        "memberId": MEMBER, "taskId": TASK_ID,
    }, call_id=call_id)
    assert preview["ok"], preview
    dto = preview["result"]
    assert set(dto) == {
        "previewId", "projectRoot", "endpoint", "memberId", "taskId", "sessionId",
        "targetVersion", "baseCommit", "revision", "task", "context",
    }
    assert dto["projectRoot"] == str(ctx.source.resolve())
    assert dto["task"]["owner"] == MEMBER and dto["task"]["goal"] == TASK_TEXT
    assert dto["task"]["scopes"] == ["a.txt"] and dto["task"]["status"] == "running"
    assert dto["baseCommit"] == ctx.head
    assert CREDENTIAL not in json.dumps(dto)
    approval = rpc(ctx.bridge, "collab.approve",
                   {"previewId": dto["previewId"], "resetCursor": reset_cursor},
                   call_id=call_id + 1)
    assert approval["ok"], approval
    assert approval["result"]["resetCursor"] is reset_cursor
    assert approval["result"]["preview"]["revision"] == dto["revision"]
    assert CREDENTIAL not in json.dumps(approval["result"])
    return dto, approval["result"]


def _run_events(ctx, run_id):
    return [e["payload"]["ev"] for e in ctx.events
            if e["channel"] == "run.event" and e["payload"].get("runId") == run_id]


def _finished_count(ctx, run_id) -> int:
    return sum(1 for e in ctx.events
               if e["channel"] == "run.finished" and e["payload"].get("runId") == run_id)


def _await_finished(ctx, run_id, since=0, timeout=90.0):
    """Wait for the NEXT run.finished of this run plus its worker/retire tail."""
    assert _pump(lambda: _finished_count(ctx, run_id) > since, timeout), (
        f"run.finished gelmedi: {[e['payload'].get('ev', {}).get('type') for e in ctx.events]}")
    worker = run_api._active.get("worker")
    if worker is not None:
        assert _pump(lambda: not worker.isRunning(), 40), "worker QThread bitmedi"
    # The terminal retire path runs from worker.finished (a queued signal).
    _pump(lambda: not run_api._draining_workers, 20)
    index = [i for i, e in enumerate(ctx.events)
             if e["channel"] == "run.finished" and e["payload"].get("runId") == run_id][since]
    return ctx.events[index]["payload"], since + 1


def _canonical_types(ctx, run_id):
    return [e.type for e in ctx.runtime.events(run_id, limit=500).events]


# ============================================================ 1. full E2E run


def test_approved_run_binds_each_attempt_once_and_never_leaks_the_credential(
        ctx, monkeypatch, capsys, caplog):
    """Credential -> preview -> approve -> run.start -> fix loop -> WAITING_USER.

    Proves over the real bridge only: revisions published while the run is in
    flight cannot retarget an attempt that already started, a FAILED
    verification produces a SECOND attempt whose input carries the LATEST
    canonical snapshot plus the untouched original task/plan/pins, stream
    events never buy a model call, the run settles on the canonical
    WAITING_USER, the worker-owned consumer and OS lease are released, the
    user's checkout is byte-identical, and the credential appears nowhere.
    """
    safe_points: list[object] = []
    captured = capture_sessions(monkeypatch)
    box: dict = {}
    observed: dict[int, dict] = {}

    def gate(attempt, turn, text):
        session = captured["session"]
        if attempt == 1 and turn == 0:
            # Two NEW revisions published while the run is in flight: exactly
            # what a collaborator would do mid-attempt.
            box["first"] = ctx.publish_context("context published mid flight",
                                               decisions=["dizayn kararı"])
            box["latest"] = ctx.publish_context("latest canonical context",
                                                decisions=["dizayn kararı"])
            assert _poll(lambda: session.status()["pendingCount"] == 2), session.status()
            observed[1] = session.status()
        elif attempt == 1 and turn == 1:
            # Second turn of the SAME attempt: nothing new may be bound.
            observed[11] = session.status()
        elif attempt == 2 and turn == 0:
            assert _poll(lambda: session.status()["pendingCount"] == 0), session.status()
            observed[2] = session.status()
            assert _poll(lambda: _consumed(ctx) == box["latest"]), _consumed(ctx)

    worker = RecordingBackend([
        _write_turn("a.txt", "still buggy\n"), _completed("Birinci deneme bitti."),
        _write_turn("a.txt", "fixed\n"), _completed("a.txt düzeltildi."),
    ], gate=gate)
    planner = RecordingBackend([_completed(PLAN_JSON)])
    reviewer = RecordingBackend([_completed(REVIEW_APPROVED)])
    monkeypatch.setattr(engine_factory, "build_pipeline_ports",
                        make_ports_factory(planner=planner, worker=worker, reviewer=reviewer,
                                           process_results=[_process_result(1), _process_result(0)],
                                           seen=safe_points))

    preview, approval = _approved(ctx)
    baseline = preview["revision"]

    started = rpc(ctx.bridge, "run.start", {
        "task": TASK_TEXT, "routing": ROUTING, "mentions": ["a.txt"],
        "collabApprovalHandle": approval["approvalHandle"],
    }, call_id=10)
    assert started["ok"], started
    run_id = started["result"]["runId"]
    session = captured["session"]
    assert session is run_api._active["collab_session"]

    finished, count = _await_finished(ctx, run_id)
    assert finished["status"] == "done", finished
    assert finished["engine"] == "pipeline"

    # The factory received the HOST's safe point; it never built one itself.
    assert safe_points == [session]

    # Canonical outcome: WAITING_USER, two executions, one fix attempt.
    assert ctx.runtime.get_run(run_id).status is RunStatus.WAITING_USER
    types = _canonical_types(ctx, run_id)
    assert RunEventType.RUN_WAITING_USER in types
    assert RunEventType.RUN_COMPLETED not in types
    assert types.count(RunEventType.EXECUTION_STARTED) == 2
    assert types.count(RunEventType.FIX_ATTEMPT_STARTED) == 1
    assert types.count(RunEventType.PLAN_STARTED) == 1

    # Exactly two FRESH worker sessions and four turns: the two stream events
    # never caused an extra model call.
    assert worker.sessions == 2
    assert [(a, t) for a, t, _ in worker.responds] == [(1, 0), (1, 1), (2, 0), (2, 1)]

    first_input, second_input = worker.inputs
    assert "latest canonical context" not in first_input
    assert "context published mid flight" not in first_input
    assert "latest canonical context" in second_input
    assert "context published mid flight" not in second_input
    for text in (first_input, second_input):
        assert text.count(SNAPSHOT_SECTION_HEADER) == 1
        assert f"base_commit={ctx.head}" in text
        assert TASK_TEXT in text          # the ORIGINAL user task is untouched
        assert PLAN_SUMMARY in text       # and so is the generated plan
        assert "- a.txt" in text          # and the ordered, paths-only pin list
    assert f"revision={baseline}" in first_input
    assert box["first"] != box["latest"]
    assert f"revision={box['latest']}" in second_input

    # Received moved while consumed (and the durable cursor) stayed put.
    assert observed[1]["pendingCount"] == 2
    assert observed[1]["consumedRevision"] == baseline
    assert observed[1]["receivedRevision"] == box["latest"]
    assert observed[11]["pendingCount"] == 2
    assert observed[11]["consumedRevision"] == baseline
    assert observed[2]["pendingCount"] == 0
    assert observed[2]["consumedRevision"] == box["latest"]
    assert observed[2]["receivedRevision"] == box["latest"]

    # Worker/consumer/lease are gone; the session is retained for a follow-up.
    status = session.status()
    assert status["active"] is False and status["state"] == "closed"
    assert status["consumedRevision"] == box["latest"]
    assert session._consumer is None and session._lease is None
    assert run_api._active["collab_session"] is session
    assert _lock_is_free(ctx), "OS kilidi çalışan bir tüketiciye devredildi"
    assert len(_cursor_files(ctx)) == 1 and len(_lock_files(ctx)) == 1
    assert _consumed(ctx) == box["latest"]
    assert ctx.cursor_root.stat().st_mode & 0o777 == 0o700

    # The user's checkout never moved: HEAD, index and file content identical.
    assert _source_state(ctx) == (ctx.head, "", "buggy\n")

    # The terminal status is a detached, credential-free DTO.
    reply = rpc(ctx.bridge, "collab.status", {"runId": run_id}, call_id=11)
    collaboration = reply["result"]["collaboration"]
    assert collaboration["active"] is False
    assert collaboration["state"] == "closed"
    assert collaboration["consumedRevision"] == box["latest"]
    assert collaboration["pendingCount"] == 0
    assert (collaboration["sessionId"], collaboration["taskId"],
            collaboration["memberId"]) == (SESSION_ID, TASK_ID, MEMBER)
    assert rpc(ctx.bridge, "collab.status", {"runId": "other-run"}, call_id=12)["result"] == {
        "collaboration": None}

    # Model instructions and prompts are credential-free too.
    model_blob = "\n".join(planner.instructions + worker.instructions + reviewer.instructions
                           + worker.all_texts)
    assert CREDENTIAL not in model_blob
    captured_output = capsys.readouterr()
    assert CREDENTIAL not in captured_output.out and CREDENTIAL not in captured_output.err
    assert CREDENTIAL not in caplog.text
    _secret_absent(ctx, model_blob, caplog.text, captured_output.out, captured_output.err)


# ===================================================== 2. follow-up + refusal


def test_follow_up_replays_the_durable_cursor_and_never_retargets_a_changed_task(
        ctx, monkeypatch):
    """WAITING_USER -> follow-up with a closed consumer, then a changed goal."""
    safe_points: list[object] = []
    captured = capture_sessions(monkeypatch)
    follow_inputs: list[str] = []
    follow_status: dict = {}

    def gate(attempt, turn, text):
        if attempt == 3 and turn == 0:
            follow_inputs.append(text)
            follow_status.update(captured["session"].status())

    # The follow-up attempt must write something NEW: re-writing the identical
    # content yields no diff, so the fix loop legitimately reports
    # "stalled"/EXHAUSTED and the run never reaches WAITING_USER again.
    # One review turn is consumed per settle-to-WAITING_USER (two here).
    worker = RecordingBackend([
        _write_turn("a.txt", "still buggy\n"), _completed("Birinci deneme."),
        _write_turn("a.txt", "fixed\n"), _completed("Düzeltildi."),
        _write_turn("a.txt", "fixed again\n"), _completed("Takip düzeltmesi."),
    ], gate=gate)
    monkeypatch.setattr(engine_factory, "build_pipeline_ports",
                        make_ports_factory(planner=RecordingBackend([_completed(PLAN_JSON)]),
                                           worker=worker,
                                           reviewer=RecordingBackend([_completed(REVIEW_APPROVED),
                                                                      _completed(REVIEW_APPROVED)]),
                                           process_results=[_process_result(1), _process_result(0),
                                                            _process_result(0)],
                                           seen=safe_points))
    _, approval = _approved(ctx)
    started = rpc(ctx.bridge, "run.start", {
        "task": TASK_TEXT, "routing": ROUTING, "collabApprovalHandle": approval["approvalHandle"],
    }, call_id=20)
    run_id = started["result"]["runId"]
    session = captured["session"]
    finished, count = _await_finished(ctx, run_id)
    assert finished["status"] == "done"
    assert ctx.runtime.get_run(run_id).status is RunStatus.WAITING_USER
    assert session.status()["active"] is False
    assert safe_points == [session]

    cursor_path = _cursor_files(ctx)[0]
    baseline_cursor = cursor_path.read_bytes()

    # A brand new revision, published while NO consumer is running.
    after_run = ctx.publish_context("context published after the consumer closed")

    follow = rpc(ctx.bridge, "run.followUp", {"feedback": "Sadece a.txt dosyasına dokun."}, call_id=21)
    assert follow["ok"], follow
    assert follow["result"]["runId"] == run_id
    finished, count = _await_finished(ctx, run_id, since=count)
    assert finished["status"] == "done"
    assert ctx.runtime.get_run(run_id).status is RunStatus.WAITING_USER

    # Same private namespace and path; the durable cursor WON and replayed the
    # delta it had missed instead of silently rebasing on a fresh head.
    assert _cursor_files(ctx)[0] == cursor_path
    assert cursor_path.read_bytes() != baseline_cursor
    assert _consumed(ctx) == after_run
    assert follow_status["consumedRevision"] == after_run == follow_status["receivedRevision"]
    assert follow_status["pendingCount"] == 0
    assert session.status()["active"] is False
    assert run_api._active["collab_session"] is session
    assert worker.sessions == 3      # one fresh session per attempt, never per event

    follow_input = follow_inputs[0]
    assert "context published after the consumer closed" in follow_input
    assert f"revision={after_run}" in follow_input  # the LATEST canonical snapshot
    assert TASK_TEXT in follow_input      # the ORIGINAL user task survives
    assert PLAN_SUMMARY in follow_input   # and so does the original plan
    # The follow-up instruction rides on the trigger, it never rewrites the task.
    assert "Sadece a.txt dosyasına dokun." in follow_input

    # The server retargets the task: the run fails closed, it never follows it.
    ctx.publish_task(goal="Tamamen farklı bir hedef")
    calls_before, sessions_before = len(worker.responds), worker.sessions
    source_before = _source_state(ctx)
    refused = rpc(ctx.bridge, "run.followUp", {"feedback": "Yeni hedefe uy."}, call_id=22)
    assert refused["ok"], refused
    finished, count = _await_finished(ctx, run_id, since=count)
    assert finished["status"] == "failed", finished
    assert ctx.runtime.get_run(run_id).status is RunStatus.FAILED
    assert len(worker.responds) == calls_before and worker.sessions == sessions_before
    assert _source_state(ctx) == source_before
    # No auto fallback and no new backend: the ports were reused, not rebuilt.
    assert len(safe_points) == 1
    assert _lock_is_free(ctx)
    _secret_absent(ctx)


# ================================================================ 3. guards


@pytest.mark.parametrize("case", [
    "legacy_engine_pref", "empty_handle", "acp_coder", "unknown_handle",
    "stale_source_head", "factory_failure",
])
def test_every_approval_guard_refuses_without_a_legacy_run_or_a_model_call(
        ctx, monkeypatch, case):
    """No approval path may fall back to the legacy engine or reach a model."""
    built: list[object] = []
    legacy_calls: list[str] = []

    def refusing_ports(*_args, **_kwargs):
        built.append(True)
        raise AssertionError("collab reddedilirken hiçbir port inşa edilmemeli")

    def refusing_legacy(*_args, **_kwargs):
        legacy_calls.append("legacy")
        raise AssertionError("klasik motor çalıştırılmamalı")

    monkeypatch.setattr(run_api, "_start_legacy_run", refusing_legacy)
    monkeypatch.setattr(engine_factory, "build_pipeline_ports", refusing_ports)

    routing = dict(ROUTING)
    handle = "unknown-approval-handle"
    moved_head = None
    # A real preview/approve is performed for every case that needs a LIVE
    # server round-trip; the two forged-handle cases then replace that handle
    # explicitly, so each case actually exercises the handle it is named for.
    if case != "empty_handle":
        _, approval = _approved(ctx)
        handle = approval["approvalHandle"]
    if case == "empty_handle":
        handle = ""                       # refused BEFORE any canonical Run
    elif case == "unknown_handle":
        handle = "forged-approval-handle-never-issued"   # refused at bind_run
    if case == "stale_source_head":
        (ctx.source / "moved.txt").write_text("moved\n", encoding="utf-8")
        _git(["add", "-A"], ctx.source)
        _git(["commit", "-q", "-m", "moved head"], ctx.source)
        moved_head = _git(["rev-parse", "HEAD"], ctx.source)
    if case == "legacy_engine_pref":
        monkeypatch.setattr(ui_prefs, "load",
                            lambda: {**ui_prefs.DEFAULTS, "ai_engine": "legacy",
                                     "decision_layer": "off"})
    if case == "acp_coder":
        routing = {"planner": "gemini", "coder": "claude", "reviewer": "openai"}
    if case == "factory_failure":
        def exploding(*_args, **_kwargs):
            raise RuntimeError("pipeline portları kurulamadı")

        monkeypatch.setattr(engine_factory, "build_pipeline_ports", exploding)

    reply = rpc(ctx.bridge, "run.start",
                {"task": TASK_TEXT, "routing": routing, "collabApprovalHandle": handle},
                call_id=30)

    assert not reply["ok"], reply
    assert reply["error"]["code"] in {"collab_invalid", "collab_stale", "collab_unsupported",
                                      "collab_unavailable"}
    assert CREDENTIAL not in json.dumps(reply)
    assert legacy_calls == [] and built == []
    assert run_api._active["worker"] is None
    assert run_api._active["run_id"] is None
    assert run_api._active["coordinator"] is None
    assert run_api._active["collab_session"] is None
    assert run_api._active["workspace"] is None
    assert rpc(ctx.bridge, "collab.status", {}, call_id=31)["result"] == {"collaboration": None}
    # Nothing was ever activated, so no private namespace or cursor exists.
    assert not ctx.cursor_root.exists()
    assert not ctx.workspaces.exists() or not any(ctx.workspaces.iterdir())

    if case in ("empty_handle", "legacy_engine_pref", "acp_coder"):
        assert ctx.created_runs == [], "koşu hiç kanonikleştirilmemeliydi"
    else:
        assert len(ctx.created_runs) == 1, ctx.created_runs
        record = ctx.runtime.get_run(ctx.created_runs[0])
        assert record.status is RunStatus.FAILED, record
        assert record.error_code

    head_now, porcelain, content = _source_state(ctx)
    if moved_head is None:
        assert (head_now, porcelain, content) == (ctx.head, "", "buggy\n")
    else:
        assert (head_now, porcelain) == (moved_head, "")
        assert content == "buggy\n"
    _secret_absent(ctx)


# ================================== 4. terminal cache + explicit cursor reset


def test_terminal_status_cache_and_explicit_reset_rebind_only_the_approved_head(
        ctx, monkeypatch):
    """Terminal cache first, then an explicit reset that rebase-consents once."""
    second_planner_entered = threading.Event()
    second_planner_release = threading.Event()

    def planner_gate(attempt, turn, text):
        if attempt >= 2 and turn == 0:
            second_planner_entered.set()
            assert second_planner_release.wait(60), "ikinci koşunun planner kapısı açılmadı"

    def worker_gate(attempt, turn, text):
        if attempt == 1 and turn == 0:
            # Delivered after attempt 1 bound its input, and never consumed:
            # this run changes nothing, so no second attempt ever accepts it.
            box["late"] = ctx.publish_context("delivered after the only input boundary")

    box: dict = {}
    planner = RecordingBackend([_completed(PLAN_JSON), _completed(PLAN_JSON)], gate=planner_gate)
    worker = RecordingBackend([_completed("Hiçbir değişiklik gerekmedi."),
                               _completed("Yine değişiklik yok.")], gate=worker_gate)
    monkeypatch.setattr(engine_factory, "build_pipeline_ports",
                        make_ports_factory(planner=planner, worker=worker,
                                           reviewer=RecordingBackend([_completed(REVIEW_APPROVED)]),
                                           process_results=[_process_result(0), _process_result(0)]))
    # ---- run 1: terminal, so the session is released and cached -----------
    preview, approval = _approved(ctx)
    baseline = preview["revision"]
    started = rpc(ctx.bridge, "run.start", {
        "task": TASK_TEXT, "routing": ROUTING, "collabApprovalHandle": approval["approvalHandle"],
    }, call_id=40)
    first_run = started["result"]["runId"]
    finished, count = _await_finished(ctx, first_run)
    assert finished["status"] == "done", finished
    assert ctx.runtime.get_run(first_run).status is RunStatus.SUCCEEDED
    assert run_api._active["collab_session"] is None
    assert RunEventType.RUN_COMPLETED in _canonical_types(ctx, first_run)

    cursor_path = _cursor_files(ctx)[0]
    assert _consumed(ctx) == baseline, "teslim edilen revizyon tüketilmemiş olmalı"
    assert box["late"] != baseline
    cursor_bytes = cursor_path.read_bytes()
    assert _lock_is_free(ctx)

    cached = rpc(ctx.bridge, "collab.status", {"runId": first_run}, call_id=41)["result"]["collaboration"]
    assert cached["state"] == "closed"
    assert cached["code"] == "run_finished"
    assert cached["active"] is False
    assert cached["consumedRevision"] == baseline
    assert cached["pendingCount"] == 0

    # ---- run 2: an EXPLICIT reset consent for a newer approved revision --
    ahead = ctx.publish_context("approved after the first run")
    preview2, approval2 = _approved(ctx, reset_cursor=True, call_id=42)
    assert preview2["revision"] == ahead
    # The core invariant: neither preview nor approve ever moves the cursor.
    assert cursor_path.read_bytes() == cursor_bytes

    started2 = rpc(ctx.bridge, "run.start", {
        "task": TASK_TEXT, "routing": ROUTING, "collabApprovalHandle": approval2["approvalHandle"],
    }, call_id=43)
    second_run = started2["result"]["runId"]
    assert second_run != first_run
    assert second_planner_entered.wait(60), "ikinci koşunun planner'ı başlamadı"

    # The consent is ARMED, not spent: the consumer is deliberately not started
    # yet and the durable cursor is still the old one.
    mid = rpc(ctx.bridge, "collab.status", {"runId": second_run}, call_id=44)["result"]["collaboration"]
    assert mid["active"] is True, mid
    assert mid["state"] == "stopped", mid
    assert mid["consumedRevision"] == baseline
    assert cursor_path.read_bytes() == cursor_bytes

    second_planner_release.set()
    # A NEW run has its own run.finished counter: `since` must restart at 0
    # (a follow-up on the SAME run keeps the previous count instead).
    finished2, _second_count = _await_finished(ctx, second_run, since=0)
    assert finished2["status"] == "done", finished2
    # The reset rebased the cursor onto the EXACT approved revision.
    assert _cursor_files(ctx)[0] == cursor_path
    assert _consumed(ctx) == ahead
    second_input = worker.inputs[1]
    assert second_input.count(SNAPSHOT_SECTION_HEADER) == 1
    assert f"revision={ahead}" in second_input
    assert "approved after the first run" in second_input
    final = rpc(ctx.bridge, "collab.status", {"runId": second_run}, call_id=45)["result"]["collaboration"]
    assert final["active"] is False and final["state"] == "closed"
    assert final["consumedRevision"] == ahead
    _secret_absent(ctx)


# ==================================================== 5. cancel while waiting


def test_cancel_while_the_worker_waits_settles_cancelled_and_releases_everything(
        ctx, monkeypatch):
    captured = capture_sessions(monkeypatch)

    def gate(attempt, turn, text):
        if attempt == 1 and turn == 0:
            entered.set()
            assert ctx.release.wait(60), "model turu hiç bırakılmadı"

    entered = threading.Event()
    worker = RecordingBackend([_write_turn("a.txt", "fixed\n"), _completed("bitti.")], gate=gate)
    monkeypatch.setattr(engine_factory, "build_pipeline_ports",
                        make_ports_factory(planner=RecordingBackend([_completed(PLAN_JSON)]),
                                           worker=worker,
                                           reviewer=RecordingBackend([_completed(REVIEW_APPROVED)]),
                                           process_results=[_process_result(0)]))
    _, approval = _approved(ctx)
    started = rpc(ctx.bridge, "run.start", {
        "task": TASK_TEXT, "routing": ROUTING, "collabApprovalHandle": approval["approvalHandle"],
    }, call_id=50)
    run_id = started["result"]["runId"]
    session = captured["session"]
    assert entered.wait(60), "worker model turuna girmedi"
    assert session.status()["active"] is True
    workspace_root = Path(run_api._active["workspace"].root)
    assert workspace_root.is_dir()

    cancelled = rpc(ctx.bridge, "run.cancel", {}, call_id=51)
    # The envelope carries its call id: assert the payload, not a bare dict.
    assert cancelled["ok"] is True and cancelled["result"] == {}, cancelled
    assert any(ev.get("text") == "Durduruluyor…" for ev in _run_events(ctx, run_id))

    ctx.release.set()
    finished, _ = _await_finished(ctx, run_id)
    # Canonical CANCELLED, never a generic failure.
    assert finished["status"] == "cancelled", finished
    assert ctx.runtime.get_run(run_id).status is RunStatus.CANCELLED
    assert RunEventType.RUN_CANCELLED in _canonical_types(ctx, run_id)

    # The cancel token prevented the queued tool write, and the worktree is gone.
    # The cancelled run's retire path disposes the worktree BEFORE run.finished
    # is published, so "no write ever landed" is proven from the canonical log
    # and the empty proposal set -- NOT by reading the disposed worktree.
    assert RunEventType.PROPOSAL_READY not in _canonical_types(ctx, run_id)
    assert run_api._active["proposals"] == []
    assert not any(ev.get("type") == "proposal" for ev in _run_events(ctx, run_id))
    assert run_api._active["workspace"] is None
    assert not workspace_root.exists()
    assert run_api._active["collab_session"] is None
    assert session.status()["active"] is False
    assert session._consumer is None and session._lease is None
    assert _lock_is_free(ctx)
    assert _source_state(ctx) == (ctx.head, "", "buggy\n")
    _secret_absent(ctx)


# ================================================ 6. shutdown while blocked


def test_shutdown_while_the_worker_is_blocked_defers_every_cleanup_to_the_exit(
        ctx, monkeypatch):
    entered = threading.Event()

    def gate(attempt, turn, text):
        if attempt == 1 and turn == 0:
            entered.set()
            assert ctx.release.wait(60), "model turu hiç bırakılmadı"

    worker = RecordingBackend([_write_turn("a.txt", "fixed\n"), _completed("bitti.")], gate=gate)
    monkeypatch.setattr(engine_factory, "build_pipeline_ports",
                        make_ports_factory(planner=RecordingBackend([_completed(PLAN_JSON)]),
                                           worker=worker,
                                           reviewer=RecordingBackend([_completed(REVIEW_APPROVED)]),
                                           process_results=[_process_result(0)]))
    _, approval = _approved(ctx)
    started = rpc(ctx.bridge, "run.start", {
        "task": TASK_TEXT, "routing": ROUTING, "collabApprovalHandle": approval["approvalHandle"],
    }, call_id=60)
    run_id = started["result"]["runId"]
    session = run_api._active["collab_session"]
    thread = run_api._active["worker"]
    assert entered.wait(60), "worker model turuna girmedi"
    workspace_root = Path(run_api._active["workspace"].root)

    run_api.shutdown()

    # Nothing was destroyed while the QThread still owns those resources.
    assert thread.isRunning()
    assert workspace_root.is_dir()
    assert run_api._active["workspace"] is not None
    assert session.status()["active"] is True
    assert session._lease is not None
    assert not _lock_is_free(ctx), "kilit, çalışan worker varken serbest bırakılmamalı"
    assert (workspace_root / "a.txt").read_text(encoding="utf-8") == "buggy\n"

    ctx.release.set()
    assert _pump(lambda: thread.isFinished(), 60), "worker QThread zombi kaldı"
    assert _pump(lambda: session.status()["active"] is False, 30)
    assert _pump(lambda: run_api._active["workspace"] is None, 30)
    assert _pump(lambda: not run_api._draining_workers, 30)
    assert _lock_is_free(ctx)
    assert not workspace_root.exists()
    assert ctx.runtime.get_run(run_id).status in {RunStatus.CANCELLED, RunStatus.FAILED}
    assert _source_state(ctx) == (ctx.head, "", "buggy\n")
    _secret_absent(ctx)


# ============================================== 7. busy apply/reject guards


def test_apply_and_reject_are_refused_while_the_worker_is_running(ctx, monkeypatch):
    entered = threading.Event()

    def gate(attempt, turn, text):
        if attempt == 1 and turn == 0:
            entered.set()
            assert ctx.release.wait(60), "model turu hiç bırakılmadı"

    worker = RecordingBackend([_write_turn("a.txt", "fixed\n"), _completed("bitti.")], gate=gate)
    monkeypatch.setattr(engine_factory, "build_pipeline_ports",
                        make_ports_factory(planner=RecordingBackend([_completed(PLAN_JSON)]),
                                           worker=worker,
                                           reviewer=RecordingBackend([_completed(REVIEW_APPROVED)]),
                                           process_results=[_process_result(0)]))
    _, approval = _approved(ctx)
    started = rpc(ctx.bridge, "run.start", {
        "task": TASK_TEXT, "routing": ROUTING, "collabApprovalHandle": approval["approvalHandle"],
    }, call_id=70)
    run_id = started["result"]["runId"]
    assert entered.wait(60), "worker model turuna girmedi"
    session = run_api._active["collab_session"]
    assert session.status()["active"] is True

    applied = rpc(ctx.bridge, "run.applyProposals", {"paths": ["a.txt"]}, call_id=71)
    assert not applied["ok"] and applied["error"]["code"] == "busy", applied
    rejected = rpc(ctx.bridge, "run.rejectProposals", {}, call_id=72)
    assert not rejected["ok"] and rejected["error"]["code"] == "busy", rejected

    # No filesystem write, no checkpoint, no canonical settlement.
    assert _source_state(ctx) == (ctx.head, "", "buggy\n")
    assert not (ctx.source / ".imece").exists()
    assert ctx.runtime.get_run(run_id).status is RunStatus.RUNNING
    types = _canonical_types(ctx, run_id)
    assert RunEventType.PROPOSAL_APPLIED not in types
    assert RunEventType.PROPOSAL_REJECTED not in types

    ctx.release.set()
    finished, _ = _await_finished(ctx, run_id)
    assert finished["status"] == "done"
    assert ctx.runtime.get_run(run_id).status is RunStatus.WAITING_USER
    _secret_absent(ctx)


# ============================================== 8. stale project mid preview


def test_a_project_switch_during_preview_answers_stale_and_writes_nothing(
        ctx, monkeypatch):
    """The async stale guard: a preview answered for a project the user left."""
    entered = threading.Event()
    release = threading.Event()
    real_client = LoopbackSnapshotClient

    def gated_factory(endpoint, *, credential):
        inner = real_client(endpoint, credential=credential)

        class Gated:
            def snapshot(self):
                entered.set()
                assert release.wait(60), "preview preflight hiç bırakılmadı"
                return inner.snapshot()

        return Gated()

    ctx.host = CollaborationHost(ctx.cursor_root, client_factory=gated_factory)
    state.set_collaboration_host(ctx.host)
    other = ctx.tmp / "other"
    other.mkdir()
    other_head = _make_source(other, content="other\n")

    seen: list[dict] = []
    ctx.bridge.reply.connect(lambda raw: seen.append(json.loads(raw)))
    ctx.bridge.call(json.dumps({"id": 80, "method": "collab.preview", "params": {
        "endpoint": ctx.base_url, "credential": CREDENTIAL,
        "memberId": MEMBER, "taskId": TASK_ID}}))
    assert entered.wait(60), "preview isteği sunucuya ulaşmadı"
    state.set_project(str(other))          # the user opens a different project
    release.set()

    assert _pump(lambda: any(r.get("id") == 80 for r in seen), 30), "preview yanıtı yok"
    reply = next(r for r in seen if r.get("id") == 80)
    assert not reply["ok"] and reply["error"]["code"] == "collab_stale", reply
    # No candidate and no approval survive for the project the user left.
    assert ctx.host._candidates == {}
    assert ctx.host._approved == {}
    assert not ctx.cursor_root.exists()
    assert _source_state(ctx) == (ctx.head, "", "buggy\n")
    assert _git(["rev-parse", "HEAD"], other) == other_head
    assert _git(["status", "--porcelain"], other) == ""
    _secret_absent(ctx, json.dumps(seen))