"""OWNER SETUP application E2E: real bridge, real owner manager, real listener.

WHAT IS REAL HERE
-----------------
* a real temporary Git source checkout (a committed base plus the owner's own
  uncommitted WIP) and the REAL ``OwnerSessionManager`` injected through the
  documented ``state.set_owner_manager`` hook with its DEFAULT factories,
* the real ``webhost.api.owner`` handlers driven ONLY over a headless
  ``HostBridge`` on one reused ``QCoreApplication``: ``collab.owner.
  previewCreate`` / ``create`` / ``start`` / ``shareOnce`` / ``localPreview``
  / ``select`` / ``stop`` / ``status`` plus ``shutdown()``,
* the private metadata the OWNER ITSELF creates on explicit calls: a 0700
  namespace holding a real bare ``store.git`` + ``hub.git``, a real
  ``init_session`` and one real published child commit per task, and a real
  ``LoopbackServer`` on literal 127.0.0.1. The coordinator and the listener are
  created BY the owner RPCs -- nothing is pre-seeded in this file,
* the real ``CollaborationHost`` + ``LoopbackSnapshotClient`` credentialed
  HTTP snapshot, the real explicit ``collab.approve`` -> opaque handle ->
  ``run.start`` binding, and the real ``engine_factory`` (real routing and
  preflight, the real ``NativeWorkerAttemptAdapter`` with ``worker_safe_point``
  taken from the host session),
* and the real delivery RPCs into the real ``SharedDeliveryService`` /
  ``capture_proposal`` / ``publish_proposal`` / ``assemble_candidate`` against
  the OWNER-CREATED store/hub, including ONE candidate whose ``verify=True``
  verdict comes from a genuine ``python3 -m pytest -q`` SUBPROCESS against the
  materialized candidate directory (so this file must be run with an
  interpreter on PATH that has pytest -- i.e. the repo venv).

WHAT IS FAKE (declared up front, never hidden)
----------------------------------------------
* model providers are scripted backends: no network, no real model,
* the NATIVE run's per-task verification process runner is fake and returns a
  canned PASS so the run can settle. THAT PASS IS NOT EVIDENCE FOR ANY
  CANDIDATE. The single verified candidate below is checked by the REAL pytest
  subprocess, and its own ``.pytest_cache`` side effect proves which runner
  produced which verdict,
* the ONE failure injected in this file is a ``close()`` that raises OSError
  once, because no public seam can make a real socket close fail.

SIMULATION BOUNDARY
-------------------
ONE application host, ONE process, ONE bridge, ONE native run. Bob is a
DECLARED member with no task of his own: in this file he only ever performs an
intentional one-shot credential share, so what is proven here is the OWNER's own
lifecycle end to end -- not two simultaneous members (the two-member delivery
matrix already lives in ``test_collab_shared_delivery_e2e.py``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("PySide6")

import engine_factory  # noqa: E402
import runtime_paths  # noqa: E402
import ui_prefs  # noqa: E402
import webhost.api.collab  # noqa: F401,E402  (registers ONLY the collab handlers)
import webhost.api.delivery  # noqa: F401,E402  (registers the five delivery handlers)
import webhost.api.owner as owner_api  # noqa: E402  (registers the owner handlers)
import webhost.api.run as run_api  # noqa: E402
from agent_runtime import ModelStopReason, ModelToolCall, ModelTurn, ModelUsage  # noqa: E402
from collab_runtime.client import LoopbackSnapshotClient, SnapshotClientError  # noqa: E402
from collab_runtime.host import CollaborationHost  # noqa: E402
from collab_runtime.owner import OwnerSessionManager  # noqa: E402
from collab_runtime.proposals import read_proposal  # noqa: E402
from collab_runtime.store import GitStore  # noqa: E402
from run_runtime import RunRuntime, RunStatus, RunStore  # noqa: E402
from run_runtime.events import RunEventType  # noqa: E402
from webhost import state  # noqa: E402
from webhost.bridge import HostBridge  # noqa: E402

# The real Qt app, the bounded RPC/pump helpers, the scripted backends and the
# real-pipeline-ports-with-fake-models factory are the SAME ones the existing
# collaboration application E2E uses: importing them (instead of
# re-implementing) keeps every file talking about one bridge surface.
from test_collab_application_e2e import (  # noqa: E402
    PLAN_JSON, REVIEW_APPROVED, ROUTING, RecordingBackend, _completed, _drain,
    _git, _process_result, _pump, _reset_active, make_ports_factory, qapp, rpc,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git yok")


# ---------------------------------------------------------------- the world --

SESSION_ID = "owner-app-e2e"
TARGET_VERSION = "owner app e2e v1"
TASK_ID = "t-a"
ALICE, BOB = "alice", "bob"
MEMBERS = [ALICE, BOB]
TASK_GOAL = "Fix the bug in a.txt."
# The shared goal is intentionally free of any credential: that is exactly what
# makes every "the token never appears" assertion below precise.
SHARED_GOAL = "Coordinate the tiny app safely, one task at a time."

BASE_A = 'STATUS = "buggy"\n'
# Alice's own uncommitted WIP line INSIDE her selected owned file: capture is
# cumulative from the session base for selected paths, so it must survive.
WIP_LINE = 'LOCAL_NOTE = "alice keeps this line"\n'
WIP_A = BASE_A + WIP_LINE
FIXED_A = WIP_A + '\nSTATUS = "fixed"\n'
# The trusted project check: it FAILS on the committed base commit (the file
# still says "buggy") and PASSES only after the model's edit, so a PASS below is
# a real verdict and never a vacuous one.
CONTRACT = ('from pathlib import Path\n\n\n'
            'def test_status_is_fixed():\n'
            '    source = (Path(__file__).resolve().parents[1] / "a.txt")'
            '.read_text(encoding="utf-8")\n'
            '    assert \'STATUS = "fixed"\' in source\n')
# Alice's untracked file OUTSIDE every owned path: never selectable, never
# serialized into the artifact, still in her checkout at the end.
SCRATCH = "alice private scratch\n"

# Exact owner DTO shapes (webhost/api/owner.py + OwnerSessionManager.status).
STATUS_KEYS = {
    "state", "projectRoot", "sessionId", "targetVersion", "baseCommit", "revision",
    "goal", "ownerId", "memberIds", "tasks", "storePath", "hubPath", "endpoint",
    "epoch", "exportedMembers", "retryRequired", "createdPaths",
}
PREVIEW_KEYS = {
    "previewId", "projectRoot", "sessionId", "targetVersion", "baseCommit", "goal",
    "ownerId", "memberIds", "tasks", "mode", "warnings",
}
LOCAL_PREVIEW_KEYS = {"preview", "storePath", "hubPath", "endpoint", "memberId",
                      "taskId", "epoch"}
REVEAL_KEYS = {"endpoint", "sessionId", "baseCommit", "targetVersion", "memberId",
               "credential", "storePath", "hubPath", "taskIds", "epoch", "scope"}
# No credential-shaped word may appear in a NORMAL owner reply or event.
SECRET_WORDS = ("credential", "token", "secret", "password", "hash", "bearer")


# ------------------------------------------------------------- small helpers --


def _rpc(w, method, params=None, call_id=1, timeout=120.0):
    return rpc(w.bridge, method, params, call_id=call_id, timeout=timeout)


def _ok(w, method, params=None, call_id=1):
    reply = _rpc(w, method, params, call_id)
    assert reply["ok"], (method, reply)
    return reply["result"]


def _refused(w, method, params=None, call_id=1, code=None):
    reply = _rpc(w, method, params, call_id)
    assert not reply["ok"], (method, reply)
    assert set(reply["error"]) == {"code", "message"}, reply
    if code is not None:
        assert reply["error"]["code"] == code, (method, reply)
    return reply["error"]


def _create_params(**overrides):
    params = {
        "sessionId": SESSION_ID, "targetVersion": TARGET_VERSION, "goal": SHARED_GOAL,
        "ownerId": ALICE, "memberIds": list(MEMBERS),
        "tasks": [{"id": TASK_ID, "owner": ALICE, "goal": TASK_GOAL,
                   "scopes": ["a.txt"], "status": "running"}],
    }
    params.update(overrides)
    return params


def _port_of(endpoint: str) -> int:
    return int(endpoint.rsplit(":", 1)[1])


def _serving(port: int, timeout: float = 0.5) -> bool:
    """Is anything accepting connections on this literal loopback port?"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout):
            return True
    except OSError:
        return False


def _snapshot_code(endpoint: str, credential: str) -> str | None:
    """One REAL read-only HTTP snapshot: its fixed wire code, or None on success."""
    try:
        snapshot = LoopbackSnapshotClient(endpoint, credential=credential).snapshot()
    except SnapshotClientError as exc:
        return exc.code
    return None if snapshot.state.session_id == SESSION_ID else "wrong-session"


def _fake_native_ports(w, worker):
    """The REAL engine_factory with only the model and verification seams fake.

    ``worker_safe_point`` is still threaded through the real factory call, so
    the collaboration safe point is only ever reached by production code.
    """
    factory = make_ports_factory(
        planner=RecordingBackend([_completed(PLAN_JSON)]), worker=worker,
        reviewer=RecordingBackend([_completed(REVIEW_APPROVED)]),
        process_results=[_process_result(0)],   # fake per-task PASS: NOT a candidate verdict
    )
    w.monkeypatch.setattr(engine_factory, "build_pipeline_ports", factory)
    return worker


def _write_turn(call_id, path, content):
    """A write turn with a distinct tool-call id (a repeat would be a replay)."""
    return ModelTurn("", (ModelToolCall(call_id, "write_file", {"path": path, "content": content}),),
                     ModelStopReason.TOOL_USE, ModelUsage())


def _git_clone(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(source), str(target)], check=True,
                   capture_output=True, text=True)


# ------------------------------------------------ wire / leak / source invariants --


def _no_secret_word(dto, where):
    """A NORMAL owner DTO or event may not carry any credential-shaped field."""
    text = json.dumps(dto, ensure_ascii=False, default=str).lower()
    for word in SECRET_WORDS:
        assert word not in text, f"{where} bir kimlik alanı taşıyor: {word}"


def _no_token(w, token, *extra, where="kayıtlı yüzey", allow_ids=()):
    """A raw member token may reach NO host-visible or stored surface.

    ``allow_ids`` lists the intentional share reply (the one documented place
    a raw credential is allowed to exist) so it can be excluded exactly.
    """
    allowed = set(allow_ids)
    replies = [r for r in w.replies if r.get("id") not in allowed]
    blobs = [json.dumps(w.events, ensure_ascii=False, default=str),
             json.dumps(replies, ensure_ascii=False, default=str),
             repr(run_api._active), repr(w.manager.status()),
             repr(w.manager._config), repr(w.manager)]
    for item in extra:
        blobs.append(item if isinstance(item, str)
                     else json.dumps(item, ensure_ascii=False, default=str))
    for blob in blobs:
        assert token not in blob, f"kimlik bilgisi {where} sızdı"
    # Every stored byte: private metadata, private cursors, workspaces, run DB.
    for root in (w.private, w.workspaces):
        for path in sorted(root.rglob("*")) if root.exists() else []:
            if path.is_file() and not path.is_symlink():
                assert token.encode("ascii") not in path.read_bytes(), path
    for name in ("runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3-journal"):
        candidate = w.tmp / name
        if candidate.exists():
            assert token.encode("ascii") not in candidate.read_bytes(), name


def _checkout_state(root: Path) -> tuple:
    """Everything an owner session must never touch: HEAD, index/worktree
    status, local config, refs and every non-``.git`` file's bytes."""
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == ".git":
            continue
        files[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return (
        _git(["rev-parse", "HEAD"], root),
        _git(["status", "--porcelain"], root),
        _git(["config", "--local", "--list"], root),
        _git(["for-each-ref", "--format=%(refname) %(objectname)"], root),
        tuple(sorted(files.items())),
    )


def _await_settled(w, run_id, timeout=180.0) -> dict:
    """Wait for THIS run's run.finished plus its worker/retire tail."""
    assert _pump(lambda: sum(1 for e in w.events if e["channel"] == "run.finished"
                             and e["payload"].get("runId") == run_id) > 0, timeout), (
        "run.finished gelmedi")
    worker = run_api._active.get("worker")
    if worker is not None:
        assert _pump(lambda: not worker.isRunning(), 60), "worker QThread bitmedi"
    _pump(lambda: not run_api._draining_workers, 30)
    index = [i for i, e in enumerate(w.events)
             if e["channel"] == "run.finished" and e["payload"].get("runId") == run_id][0]
    return w.events[index]["payload"]


def _run_check_payloads(w, run_id, kind):
    """The durable per-task verification payloads of THIS run."""
    return [event.payload for event in w.runtime.events(run_id, limit=500).events
            if event.type == kind]


# -------------------------------------------------------------------- fixture -


def _make_tiny_app(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    _git(["config", "user.name", "T"], root)
    _git(["config", "user.email", "t@example.com"], root)
    (root / "a.txt").write_text(BASE_A, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text(CONTRACT, encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "tiny app base"], root)
    return _git(["rev-parse", "HEAD"], root)


@pytest.fixture
def w(tmp_path, monkeypatch, qapp):
    """One app host, one private owner root, one source checkout (+ its origin)."""
    origin = tmp_path / "origin"
    head = _make_tiny_app(origin)
    source = tmp_path / "alice-a"
    _git_clone(origin, source)
    assert _git(["rev-parse", "HEAD"], source) == head
    # Alice's own uncommitted WIP: one line inside her owned file, one untracked
    # file outside every owned path.
    (source / "a.txt").write_text(WIP_A, encoding="utf-8")
    (source / "notes.txt").write_text(SCRATCH, encoding="utf-8")

    workspaces = tmp_path / "workspaces"
    monkeypatch.setattr(runtime_paths, "workspaces_dir", lambda: workspaces)
    monkeypatch.setattr(engine_factory, "workspaces_dir", lambda: workspaces)
    monkeypatch.setattr(ui_prefs, "load",
                        lambda: {**ui_prefs.DEFAULTS, "ai_engine": "auto", "decision_layer": "off"})

    runtime = RunRuntime(RunStore(tmp_path / "runtime.sqlite3"))
    state.set_project(str(source))
    state.set_run_runtime(runtime)
    state.set_delivery_service(None)

    # The private owner root lives OUTSIDE the source checkout and the app's own
    # private cursor namespace hangs off it. Nothing is pre-seeded: the manager
    # creates its metadata ONLY on an explicit create.
    private = tmp_path / "private"
    manager = OwnerSessionManager(private)              # REAL, default factories
    state.set_owner_manager(manager)                    # documented injection hook
    host = CollaborationHost(private / "cursors")       # REAL app host
    state.set_collaboration_host(host)

    bridge = HostBridge()
    events: list[dict] = []
    replies: list[dict] = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    bridge.reply.connect(lambda raw: replies.append(json.loads(raw)))

    world = SimpleNamespace(
        tmp=tmp_path, origin=origin, source=source, head=head, private=private,
        manager=manager, host=host, workspaces=workspaces, runtime=runtime,
        bridge=bridge, events=events, replies=replies, monkeypatch=monkeypatch,
    )
    _reset_active()
    try:
        yield world
    finally:
        _drain()
        # ALWAYS stop the owner, whatever the test did, then prove there is no leak.
        thread = owner_api._SHUTDOWN_THREAD
        if thread is not None and thread.is_alive():
            thread.join(60)
        try:
            manager.stop()
        except Exception:
            pass
        status = manager.status()
        assert status["endpoint"] is None, f"dinleyici sızdı: {status}"
        assert status["state"] in {"stopped", "configured", "unconfigured"}, status
        state.set_owner_manager(None)
        state.set_collaboration_host(None)
        owner_api._SHUTDOWN_THREAD = None


# ================================================ 1. A: setup -> run -> delivery


def test_owner_create_start_local_preview_run_and_delivery_never_touch_the_source(w):
    """The whole explicit owner path, end to end, over the real bridge.

    * ``previewCreate`` writes NOTHING (no private dir, no listener, no token)
      and the committed HEAD plus the owner's dirty WIP survive it untouched,
    * the explicit ``create`` publishes exactly ONE context commit plus one
      child commit per task into a real 0700 private store/hub, binds the
      source by its HEAD, starts NO server and holds NO credential,
    * ``start`` binds a REAL literal 127.0.0.1 listener; a wrong confirmation
      reveals nothing and a normal status DTO carries no secret,
    * ``localPreview`` authenticates over real HTTP INSIDE the manager and
      returns a credential-free DTO that is NOT an approval,
    * the explicit ``collab.approve`` + real ``run.start`` bind the run, the
      native worker writes the selected owned file in isolation and the run
      settles in the canonical WAITING_USER,
    * the real delivery RPCs capture ONLY the selected path into the
      OWNER-CREATED hub, and the one ``verify=True`` verdict is a REAL
      ``python3 -m pytest -q`` subprocess, never the fake runner,
    * the source HEAD/index/refs/config/file bytes are identical before and
      after, and no credential reaches any reply, event, private JSON or DB.
    """
    before = _checkout_state(w.source)

    # ---- 1. the preview is inert -------------------------------------------
    preview = _ok(w, "collab.owner.previewCreate", _create_params(), call_id=100)
    assert set(preview) == PREVIEW_KEYS, sorted(set(preview) ^ PREVIEW_KEYS)
    assert preview["mode"] == "create" and preview["projectRoot"] == str(w.source)
    assert preview["baseCommit"] == w.head and preview["sessionId"] == SESSION_ID
    assert preview["memberIds"] == MEMBERS and preview["ownerId"] == ALICE
    assert [task["id"] for task in preview["tasks"]] == [TASK_ID]
    assert "metadata-only" in preview["warnings"][0]
    assert not w.private.exists(), "önizleme özel bir dizin bile oluşturdu"
    assert w.manager.status()["state"] == "unconfigured"
    assert w.manager.status()["endpoint"] is None
    assert _checkout_state(w.source) == before

    # ---- 2. explicit create: private metadata only -------------------------
    created = _ok(w, "collab.owner.create", {"previewId": preview["previewId"]}, call_id=101)
    assert set(created) == STATUS_KEYS, sorted(set(created) ^ STATUS_KEYS)
    _no_secret_word(created, "create yanıtı")
    assert created["state"] == "configured" and created["endpoint"] is None
    assert created["epoch"] == 0 and created["exportedMembers"] == []
    assert created["retryRequired"] is False and created["goal"] == SHARED_GOAL
    assert created["baseCommit"] == w.head and created["projectRoot"] == str(w.source)
    # The membership is the full owner DECLARATION (owner included), not the
    # set of task owners: Bob has no task and is still a declared member.
    assert created["ownerId"] == ALICE and created["memberIds"] == MEMBERS
    assert [task["owner"] for task in created["tasks"]] == [ALICE]
    assert created["createdPaths"] and all(Path(p).exists() for p in created["createdPaths"])

    hub, store_path = Path(created["hubPath"]), Path(created["storePath"])
    assert hub.parent == store_path.parent
    assert hub.parent.parent == w.private.resolve()
    assert w.source.resolve() != w.private.resolve()
    assert w.source.resolve() not in hub.parents and hub != w.source.resolve()
    assert stat.S_IMODE(w.private.stat().st_mode) == 0o700
    assert stat.S_IMODE(hub.parent.stat().st_mode) == 0o700
    assert hub.stat().st_uid == os.getuid() and store_path.stat().st_uid == os.getuid()
    assert _git(["rev-parse", "--is-bare-repository"], hub) == "true"

    store = GitStore(store=store_path, remote=str(hub))
    revision, live = store.fetch_state()
    assert revision == created["revision"]
    assert live.session_id == SESSION_ID and live.base_commit == w.head
    assert live.context.goal == SHARED_GOAL and live.context.decisions == ()
    assert {t.id: (t.owner, t.goal, t.scopes, t.status) for t in live.tasks.values()} == {
        TASK_ID: (ALICE, TASK_GOAL, ("a.txt",), "running")}
    # ONE initial state commit (R0) plus one published child per task, and every
    # task records that exact R0 as its context revision.
    chain = _git(["rev-list", "--first-parent", revision], store_path).splitlines()
    assert len(chain) == 1 + len(created["tasks"])
    r0 = chain[-1]
    assert all(t.context_revision == r0 for t in live.tasks.values())
    assert created["tasks"][0]["contextRevision"] == r0
    # NO artifact at all yet: the hub carries only the session branch.
    assert _git(["for-each-ref", "--format=%(refname)"], hub).splitlines() == [
        "refs/heads/imece-session"]
    assert store.remote_proposal_head("native-never-published") is None
    # Credentials NONE and the source is byte-identical.
    assert w.manager._credentials == {}
    assert _checkout_state(w.source) == before

    # ---- 3. explicit start: a real literal loopback listener ---------------
    started = _ok(w, "collab.owner.start", {}, call_id=102)
    assert set(started) == STATUS_KEYS and started["state"] == "running"
    endpoint = started["endpoint"]
    assert endpoint.startswith("http://127.0.0.1:")
    assert endpoint == f"http://127.0.0.1:{_port_of(endpoint)}" and _serving(_port_of(endpoint))
    assert started["epoch"] == 1 and started["exportedMembers"] == []
    _refused(w, "collab.owner.shareOnce", {"memberId": ALICE, "confirmSecret": False},
             call_id=103, code="owner_confirmation_required")
    status = _ok(w, "collab.owner.status", call_id=104)
    assert set(status) == STATUS_KEYS and status["state"] == "running"
    _no_secret_word(status, "status yanıtı")
    assert status["endpoint"] == endpoint and status["exportedMembers"] == []
    assert sorted(w.manager._credentials) == sorted(MEMBERS)
    assert _checkout_state(w.source) == before

    # ---- 4. localPreview: real HTTP auth, no credential, no approval ------
    local = _ok(w, "collab.owner.localPreview",
                {"memberId": ALICE, "taskId": TASK_ID}, call_id=105)
    assert set(local) == LOCAL_PREVIEW_KEYS, sorted(set(local) ^ LOCAL_PREVIEW_KEYS)
    _no_secret_word(local, "localPreview yanıtı")
    assert local["memberId"] == ALICE and local["taskId"] == TASK_ID
    assert local["endpoint"] == endpoint and local["epoch"] == 1
    assert local["storePath"] == created["storePath"]
    assert local["hubPath"] == created["hubPath"]
    host_preview = local["preview"]
    assert host_preview["projectRoot"] == str(w.source)
    assert host_preview["baseCommit"] == w.head and host_preview["sessionId"] == SESSION_ID
    assert host_preview["task"]["owner"] == ALICE and host_preview["task"]["scopes"] == ["a.txt"]
    assert host_preview["context"]["goal"] == SHARED_GOAL
    # The shortcut is NOT an approval: the host holds a candidate only.
    assert host_preview["previewId"] in w.host._candidates
    assert w.host._approved == {}
    # An explicit collab.preview with a FORGED credential is refused by the
    # real listener, i.e. the shortcut's authority is the manager's own token.
    _refused(w, "collab.preview", {"endpoint": endpoint, "credential": "f" * 43,
                                   "memberId": ALICE, "taskId": TASK_ID},
             call_id=106, code="collab_invalid")

    # ---- 5. explicit approve -> opaque handle -> the real native run ------
    approval = _ok(w, "collab.approve", {"previewId": host_preview["previewId"]},
                   call_id=107)
    assert approval["resetCursor"] is False
    assert approval["preview"] == host_preview      # the SAME immutable snapshot
    handle = approval["approvalHandle"]
    assert isinstance(handle, str) and len(handle) >= 32
    assert set(w.host._approved) == {handle}

    worker = _fake_native_ports(w, RecordingBackend([
        _write_turn("a-write", "a.txt", FIXED_A), _completed("a.txt rewritten.")]))
    checkout_at_start = _checkout_state(w.source)
    run_id = _ok(w, "run.start", {"task": TASK_GOAL, "routing": ROUTING,
                                  "mentions": ["a.txt"],
                                  "collabApprovalHandle": handle}, call_id=108)["runId"]
    session = run_api._active["collab_session"]
    workspace_root = Path(run_api._active["workspace"].root)
    assert session is not None and session.project_root == w.source
    finished = _await_settled(w, run_id)
    assert finished["status"] == "done", finished
    assert w.runtime.get_run(run_id).status is RunStatus.WAITING_USER
    assert session.active is False and session.accepted_binding is not None
    binding = session.accepted_binding
    assert binding.revision == revision and binding.context_hash == live.context_hash
    assert worker.sessions == 1
    # The model really saw the shared goal/context, and NO credential at all.
    assert any(SHARED_GOAL in text for text in worker.inputs), worker.inputs
    # The run's OWN verification plan is the REAL trusted project check...
    started_checks = _run_check_payloads(w, run_id, RunEventType.VERIFICATION_CHECK_STARTED)
    done_checks = _run_check_payloads(w, run_id, RunEventType.VERIFICATION_CHECK_COMPLETED)
    assert [payload["check_id"] for payload in started_checks] == ["python_pytest"]
    assert started_checks[0]["argv"][0] in {"python3", "python"}
    assert started_checks[0]["argv"][1:] == ["-m", "pytest", "-q"]
    # ...but this run's runner is the FAKE one: a canned, empty, 1 ms result.
    # That PASS is NOT evidence for any candidate (see the candidate section).
    assert [payload["status"] for payload in done_checks] == ["pass"]
    assert [(p["exit_code"], p["timed_out"], p["duration_ms"], p["stdout"], p["stderr"])
            for p in done_checks] == [(0, False, 1, "", "")]
    # The model touched ONLY the selected owned file: the untracked scratch file is
    # still in the worktree with its original bytes (and is never captured).
    assert (workspace_root / "a.txt").read_text(encoding="utf-8") == FIXED_A
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == SCRATCH
    assert _checkout_state(w.source) == checkout_at_start == before

    # ---- 6. real delivery into the OWNER-CREATED store/hub ----------------
    ticket = _ok(w, "collab.delivery.preview", {
        "runId": run_id, "storePath": created["storePath"],
        "hubPath": created["hubPath"], "paths": ["a.txt"]}, call_id=109)
    assert ticket["paths"] == ["a.txt"] and ticket["owner"] == ALICE
    assert ticket["taskId"] == TASK_ID and ticket["fileCount"] == 1
    assert ticket["outOfScopePaths"] == [] and ticket["artifactBytes"] > 0
    # Provenance is the immutable snapshot the RUN ACCEPTED, copied verbatim.
    assert ticket["contextRevision"] == binding.revision
    assert ticket["contextHash"] == binding.context_hash
    assert ticket["expectedRevision"] == revision
    assert store.remote_proposal_head(ticket["proposalId"]) is None

    published = _ok(w, "collab.delivery.publish",
                    {"runId": run_id, "previewId": ticket["previewId"]}, call_id=110)
    assert published["proposalId"] == ticket["proposalId"]
    assert published["contextRevision"] == binding.revision
    assert published["paths"] == ["a.txt"] and published["outOfScopeAuthorized"] is False
    assert store.remote_proposal_head(published["proposalId"]) == published["proposalRevision"]

    frozen = read_proposal(store, published["proposalId"])
    assert [f.path for f in frozen.files] == ["a.txt"]
    assert frozen.files[0].after_bytes.decode("utf-8") == FIXED_A
    assert SCRATCH not in frozen.files[0].after_bytes.decode("utf-8")

    listed = _ok(w, "collab.delivery.list", {
        "runId": run_id, "storePath": created["storePath"],
        "hubPath": created["hubPath"]}, call_id=111)
    assert [entry["proposalId"] for entry in listed["proposals"]] == [published["proposalId"]]
    entry = listed["proposals"][0]
    assert entry["owner"] == ALICE and entry["taskId"] == TASK_ID
    assert entry["fileCount"] == 1 and entry["currentContextHashMatches"] is True

    out = w.tmp / "candidates"
    out.mkdir()
    unverified = _ok(w, "collab.delivery.candidate", {
        "runId": run_id, "storePath": created["storePath"],
        "hubPath": created["hubPath"], "proposalIds": [published["proposalId"]],
        "outputPath": str(out / "unverified"), "verify": False}, call_id=112)
    assert unverified["conflicts"] == []
    assert unverified["candidate"]["verification"] == {
        "status": "not_run", "plan_id": None, "checks": [], "changed_content": False}
    assert unverified["candidate"]["proposal_ids"] == [published["proposalId"]]
    # verify=False is never an implicit success: no check could have run there.
    assert not (out / "unverified" / ".pytest_cache").exists()

    verified = _ok(w, "collab.delivery.candidate", {
        "runId": run_id, "storePath": created["storePath"],
        "hubPath": created["hubPath"], "proposalIds": [published["proposalId"]],
        "outputPath": str(out / "verified"), "verify": True}, call_id=113)
    receipt = verified["candidate"]
    assert verified["conflicts"] == [] and receipt["base_commit"] == w.head
    assert receipt["verification"]["status"] == "pass"
    assert receipt["verification"]["plan_id"] is not None
    assert receipt["verification"]["changed_content"] is False
    assert receipt["verification"]["fingerprint_complete"] is True
    assert [(c["check_id"], c["status"], c["exit_code"], c["timed_out"])
            for c in receipt["verification"]["checks"]] == [("python_pytest", "pass", 0, False)]
    # The REAL subprocess left its own trace; the fake runner never could.
    assert (out / "verified" / ".pytest_cache").exists()
    assert (out / "verified" / "a.txt").read_text(encoding="utf-8") == FIXED_A
    assert (out / "verified" / "tests" / "test_a.py").read_text(encoding="utf-8") == CONTRACT
    # ...and that same real check is NOT vacuous: on an untouched copy of the
    # committed base it fails. (A scratch copy, so the source stays pristine.)
    base_copy = w.tmp / "base-copy"
    shutil.copytree(w.source, base_copy, ignore=shutil.ignore_patterns(".git"))
    (base_copy / "a.txt").write_text(_git(["show", f"{w.head}:a.txt"], w.source) + "",
                                     encoding="utf-8")
    baseline = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=str(base_copy),
        capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert baseline.returncode != 0, baseline.stdout
    assert 'STATUS = "fixed"' not in (base_copy / "a.txt").read_text(encoding="utf-8")

    # ---- 7. nothing moved --------------------------------------------------
    assert _ok(w, "run.rejectProposals", call_id=114) == {}
    assert _checkout_state(w.source) == before
    assert not (w.source / ".imece").exists()
    assert (w.source / "a.txt").read_text(encoding="utf-8") == WIP_A
    assert (w.source / "notes.txt").read_text(encoding="utf-8") == SCRATCH
    assert set(_git(["status", "--porcelain"], w.source).splitlines()) == {
        "M a.txt", "?? notes.txt"}

    # ---- 8. the ONE intentional share, then the whole hygiene sweep -------
    revealed = _ok(w, "collab.owner.shareOnce",
                   {"memberId": ALICE, "confirmSecret": True}, call_id=115)
    assert set(revealed) == REVEAL_KEYS, sorted(set(revealed) ^ REVEAL_KEYS)
    assert revealed["scope"] == "loopback-only" and revealed["taskIds"] == [TASK_ID]
    assert revealed["endpoint"] == endpoint and revealed["epoch"] == 1
    token = revealed["credential"]
    assert token and len(token) >= 32 and token == w.manager._credentials[ALICE]
    # It is a REAL working credential over the REAL listener...
    assert _snapshot_code(endpoint, token) is None
    # ...and the ONLY surface it may ever appear on.
    assert token not in json.dumps(w.events, ensure_ascii=False, default=str)
    assert token not in json.dumps(ui_prefs.load(), ensure_ascii=False, default=str)
    assert token not in SHARED_GOAL and token not in live.context.goal
    assert token not in json.dumps(binding.to_dict(), ensure_ascii=False)
    assert token not in json.dumps(ui_prefs.DEFAULTS, ensure_ascii=False, default=str)
    for text in worker.inputs + worker.all_texts:
        assert token not in text
    _no_token(w, token, where="kayıtlı yüzey", allow_ids=(115,))


# ============================== 2. B: one-shot sharing, rotation, app shutdown


def test_sharing_is_one_shot_per_epoch_and_a_real_restart_rotates_every_token(w):
    """A credential is revealed once per member per epoch; a real stop plus a
    real restart on the SAME configuration rotates every token.

    * the explicit raw DTO is the only reply that may carry a token, a second
      reveal inside the epoch is refused, and no other reply/event/pref/private
      JSON/DB/model input holds it,
    * ``stop`` drains asynchronously for real: the port stops serving, the
      server and the token map are released, and no delivery lease survives,
    * ``collab.owner.select`` re-opens the STOPPED session on the very same
      private metadata WITHOUT rewriting it,
    * the restart is accepted on the same literal port, the epoch and every
      token are new, and the OLD tokens are denied by the real server. (No
      claim is made about which port a real OS picks: the port is whatever this
      listener bound and the same one is reused deliberately.)
    """
    preview = _ok(w, "collab.owner.previewCreate", _create_params(), call_id=200)
    created = _ok(w, "collab.owner.create", {"previewId": preview["previewId"]}, call_id=201)
    first = _ok(w, "collab.owner.start", {}, call_id=202)
    endpoint, port = first["endpoint"], _port_of(first["endpoint"])
    store_path, hub_path = created["storePath"], created["hubPath"]

    # ---- one explicit reveal per member -----------------------------------
    alice = _ok(w, "collab.owner.shareOnce", {"memberId": ALICE, "confirmSecret": True},
                call_id=203)
    bob = _ok(w, "collab.owner.shareOnce", {"memberId": BOB, "confirmSecret": True},
              call_id=204)
    assert alice["taskIds"] == [TASK_ID] and bob["taskIds"] == []
    assert alice["credential"] != bob["credential"]
    alice_token, bob_token = alice["credential"], bob["credential"]
    assert alice_token == w.manager._credentials[ALICE]
    assert _snapshot_code(endpoint, alice_token) is None
    assert _snapshot_code(endpoint, bob_token) is None
    # A forged credential of the right shape is denied by the real listener.
    assert _snapshot_code(endpoint, "e" * 43) == "access_denied"
    # One-shot: a second reveal for either member is refused for that member.
    _refused(w, "collab.owner.shareOnce", {"memberId": ALICE, "confirmSecret": True},
             call_id=205, code="owner_already_shared")
    _refused(w, "collab.owner.shareOnce", {"memberId": BOB, "confirmSecret": True},
             call_id=206, code="owner_already_shared")
    assert w.manager.status()["exportedMembers"] == sorted(MEMBERS)
    # Every OTHER reply, event and stored surface is token-free.
    assert alice_token not in json.dumps(w.events, ensure_ascii=False, default=str)
    assert bob_token not in json.dumps(w.events, ensure_ascii=False, default=str)
    assert alice_token not in json.dumps(ui_prefs.load(), ensure_ascii=False, default=str)
    assert alice_token not in SHARED_GOAL and alice_token not in created["goal"]
    _no_token(w, alice_token, where="durum yüzeyi", allow_ids=(203,))
    _no_token(w, bob_token, where="durum yüzeyi", allow_ids=(204,))

    # ---- a real stop: async drain, port gone, everything released ---------
    stopped = _ok(w, "collab.owner.stop", call_id=210)
    assert set(stopped) == STATUS_KEYS and stopped["state"] == "stopped"
    assert stopped["endpoint"] is None and stopped["epoch"] == first["epoch"]
    assert stopped["exportedMembers"] == [] and stopped["retryRequired"] is False
    assert w.manager._server is None and w.manager._credentials == {}
    assert not _serving(port)
    assert _snapshot_code(endpoint, alice_token) == "connection_error"
    assert run_api._delivery_leases == {} and run_api._draining_workers == []

    # ---- re-open the STOPPED session on the SAME metadata, no rewrite -----
    store = GitStore(store=store_path, remote=str(hub_path))
    head_before_select = store.fetch_state()[0]
    reopened = _ok(w, "collab.owner.select", {"storePath": store_path, "hubPath": hub_path,
                                              "ownerId": ALICE, "memberIds": list(MEMBERS)},
                   call_id=211)
    assert set(reopened) == STATUS_KEYS and reopened["state"] == "configured"
    assert reopened["revision"] == head_before_select == created["revision"]
    assert reopened["sessionId"] == SESSION_ID and reopened["baseCommit"] == w.head
    assert reopened["goal"] == SHARED_GOAL and reopened["memberIds"] == MEMBERS
    assert reopened["storePath"] == store_path and reopened["hubPath"] == hub_path
    assert reopened["endpoint"] is None and reopened["epoch"] == first["epoch"]
    assert reopened["tasks"] == created["tasks"]
    assert store.fetch_state()[0] == head_before_select, "select metadata'yi yeniden yazdı"
    _no_secret_word(reopened, "select yanıtı")

    # ---- restart rotates EVERY token and the old ones are denied ---------
    # The SAME literal port is accepted again after the real stop: no claim is
    # made about which port a fresh start would pick, this one is requested.
    again = _ok(w, "collab.owner.start", {"port": port}, call_id=212)
    assert again["state"] == "running" and again["epoch"] > first["epoch"]
    assert again["endpoint"] == endpoint and _serving(port)
    assert set(w.manager._credentials) == set(MEMBERS)
    for old in (alice_token, bob_token):
        assert old not in w.manager._credentials.values()
        assert _snapshot_code(again["endpoint"], old) == "access_denied"
    fresh = _ok(w, "collab.owner.shareOnce", {"memberId": ALICE, "confirmSecret": True},
                call_id=213)
    assert fresh["credential"] != alice_token and fresh["epoch"] == again["epoch"]
    assert _snapshot_code(again["endpoint"], fresh["credential"]) is None
    _no_token(w, fresh["credential"], where="restart sonrası yüzey", allow_ids=(213,))

    # ---- the application shutdown queues the real single owner-stop -------
    run_api.shutdown()
    thread = owner_api._SHUTDOWN_THREAD
    assert thread is not None and thread.name == "owner-shutdown"
    run_api.shutdown()                              # at most ONE owned worker
    assert owner_api._SHUTDOWN_THREAD is thread
    assert _pump(lambda: w.manager.status()["state"] == "stopped", 60), (
        f"owner kapanmadı: {w.manager.status()}")
    assert not thread.is_alive()
    assert w.manager.status()["endpoint"] is None
    assert w.manager._credentials == {}
    assert not _serving(port) and _snapshot_code(endpoint, fresh["credential"]) == (
        "connection_error")
    assert run_api._delivery_leases == {} and run_api._draining_workers == []


# ================================ 3. C: a project switch while the owner runs


def test_a_changed_project_keeps_the_owner_truthful_and_burns_no_secret(w):
    """A different project root (same base commit) never lies and never burns.

    * while the owner session keeps RUNNING on its own root, the global status
      still reports that old root truthfully,
    * ``shareOnce`` and ``localPreview`` are refused for the wrong root BEFORE
      the one-shot is consumed, and adding no candidate, so the very same calls
      still work once the original root is open again (nothing was burned),
    * a preview created in an older project generation can never be consumed
      under the new root: it is dropped, nothing is written, and the running
      configuration is untouched,
    * a collaboration candidate from the previous root is dropped by the real
      host on the switch and can no longer be approved,
    * the sources, the private metadata and the running listener are unchanged
      by the switch itself.
    """
    other = w.tmp / "bob-b"
    _git_clone(w.origin, other)                       # the SAME base commit
    assert _git(["rev-parse", "HEAD"], other) == w.head
    before_a, before_b = _checkout_state(w.source), _checkout_state(other)

    # One preview is created on root A and stays unconsumed.
    late = _ok(w, "collab.owner.previewCreate", _create_params(), call_id=300)

    # ---- a preview from an OLDER project generation is dropped -----------
    # Nothing is configured yet, so the refusal is the real generation check:
    # the preview was bound to root A and cannot be consumed under root B.
    state.set_project(str(other))
    _refused(w, "collab.owner.create", {"previewId": late["previewId"]},
             call_id=301, code="owner_preview_stale")
    unconfigured = _ok(w, "collab.owner.status", call_id=302)
    assert unconfigured["state"] == "unconfigured" and unconfigured["revision"] is None
    assert unconfigured["storePath"] is None and unconfigured["hubPath"] is None
    assert not w.private.exists(), "reddedilen create özel metadata yazdı"
    assert _checkout_state(w.source) == before_a and _checkout_state(other) == before_b

    # ---- back on the owner root the unconsumed preview is still usable -----
    # (the stale attempt refused BEFORE consuming it, so nothing burned)
    state.set_project(str(w.source))
    configured = _ok(w, "collab.owner.create", {"previewId": late["previewId"]}, call_id=303)
    assert configured["state"] == "configured" and configured["endpoint"] is None
    assert configured["projectRoot"] == str(w.source) and configured["baseCommit"] == w.head

    started = _ok(w, "collab.owner.start", {}, call_id=304)
    endpoint, port = started["endpoint"], _port_of(started["endpoint"])
    alice_token = _ok(w, "collab.owner.shareOnce",
                      {"memberId": ALICE, "confirmSecret": True},
                      call_id=307)["credential"]
    assert w.manager.status()["exportedMembers"] == [ALICE]

    # ---- the user opens the OTHER checkout while the owner keeps RUNNING ---
    state.set_project(str(other))
    status = _ok(w, "collab.owner.status", call_id=308)
    assert status["state"] == "running" and status["projectRoot"] == str(w.source)
    assert status["endpoint"] == endpoint and status["epoch"] == started["epoch"]
    assert status["baseCommit"] == w.head and _serving(port)
    # Wrong root: refused BEFORE any burn and BEFORE any candidate exists.
    _refused(w, "collab.owner.shareOnce", {"memberId": BOB, "confirmSecret": True},
             call_id=309, code="owner_wrong_project")
    _refused(w, "collab.owner.localPreview", {"memberId": ALICE, "taskId": TASK_ID},
             call_id=310, code="owner_wrong_project")
    assert w.host._candidates == {} and w.host._approved == {}
    assert w.manager.status()["exportedMembers"] == [ALICE]   # Bob's one-shot unspent
    # The token that exists so far is absent from every event and from every
    # reply except its own intentional share.
    _no_token(w, alice_token, where="proje değişimi sırasında", allow_ids=(307,))

    # ---- back on the owner root: the unspent one-shot really still works --
    state.set_project(str(w.source))
    assert _ok(w, "collab.owner.shareOnce", {"memberId": BOB, "confirmSecret": True},
               call_id=311)["memberId"] == BOB
    local = _ok(w, "collab.owner.localPreview", {"memberId": ALICE, "taskId": TASK_ID},
                call_id=312)
    assert local["preview"]["previewId"] in w.host._candidates
    assert w.host._approved == {}
    candidate_id = local["preview"]["previewId"]

    # ---- a second switch drops that candidate for good --------------------
    state.set_project(str(other))
    _refused(w, "collab.approve", {"previewId": candidate_id},
             call_id=313, code="collab_invalid")
    assert w.host._candidates == {} and w.host._approved == {}
    # A running session also refuses any new create, and writes nothing.
    _refused(w, "collab.owner.create", {"previewId": "whatever"},
             call_id=314, code="owner_stop_required")
    still = _ok(w, "collab.owner.status", call_id=315)
    assert still["state"] == "running" and still["revision"] == configured["revision"]
    assert still["endpoint"] == endpoint
    assert _checkout_state(w.source) == before_a
    assert _checkout_state(other) == before_b
    assert _git(["for-each-ref", "--format=%(refname)"], configured["hubPath"]).splitlines() == [
        "refs/heads/imece-session"]

    # Back on the owner root the session is still exactly where it was: the
    # switch cost nothing and released nothing (stopping from the OTHER root is
    # the dedicated known-gap case below).
    state.set_project(str(w.source))
    assert _ok(w, "collab.owner.status", call_id=316)["endpoint"] == endpoint
    assert _serving(port)
    assert _snapshot_code(endpoint, w.manager._credentials[BOB]) is None


def test_stop_is_allowed_from_a_different_project_root(w):
    """The owner stop works even when the user already opened another root.

    ``collab.owner.stop`` submits with the SESSION's own root and the
    generation captured at call time, so a project switch can neither strand a
    live listener nor refuse the stop: the listener closes, the tokens are
    released and the private metadata survives untouched for a later reopen.
    """
    other = w.tmp / "bob-b"
    _git_clone(w.origin, other)
    preview = _ok(w, "collab.owner.previewCreate", _create_params(), call_id=400)
    created = _ok(w, "collab.owner.create", {"previewId": preview["previewId"]}, call_id=401)
    started = _ok(w, "collab.owner.start", {}, call_id=402)
    endpoint, port = started["endpoint"], _port_of(started["endpoint"])
    token = _ok(w, "collab.owner.shareOnce", {"memberId": ALICE, "confirmSecret": True},
                call_id=403)["credential"]
    assert _serving(port) and _snapshot_code(endpoint, token) is None

    state.set_project(str(other))                     # the user moved on
    stopped = _ok(w, "collab.owner.stop", call_id=404)
    assert set(stopped) == STATUS_KEYS
    assert stopped["state"] == "stopped" and stopped["endpoint"] is None
    assert stopped["projectRoot"] == str(w.source)     # the receipt stays truthful
    assert stopped["revision"] == created["revision"]
    assert not _serving(port) and w.manager._credentials == {}
    assert _snapshot_code(endpoint, token) == "connection_error"
    # The private metadata is untouched: returning to the bound root starts it
    # again. (A start for the OTHER root is refused, not silently rebound.)
    _refused(w, "collab.owner.start", call_id=405, code="owner_source_head_mismatch")
    state.set_project(str(w.source))
    assert _ok(w, "collab.owner.start", call_id=406)["state"] == "running"
    assert w.manager.status()["revision"] == created["revision"]
    assert w.manager.status()["epoch"] > started["epoch"]


# ================================================ 4. D: a stop whose close fails


def test_a_failed_close_keeps_the_owner_owned_until_an_explicit_retry(w, monkeypatch):
    """A ``close()`` that raises is a FIXED error, never a false "closed".

    * the bridge reports one sanitized code and nothing else,
    * the status keeps ``cleanup_failed`` + ``retryRequired``, and RETAINS the
      endpoint, the server object, the exported members and every credential,
    * a new start is refused while that cleanup is unresolved,
    * the explicit retry closes the real listener, releases everything, and the
      SAME configuration starts again with a new epoch and new tokens.
    """
    preview = _ok(w, "collab.owner.previewCreate", _create_params(), call_id=500)
    created = _ok(w, "collab.owner.create", {"previewId": preview["previewId"]}, call_id=501)
    started = _ok(w, "collab.owner.start", {}, call_id=502)
    endpoint, port = started["endpoint"], _port_of(started["endpoint"])
    token = _ok(w, "collab.owner.shareOnce", {"memberId": ALICE, "confirmSecret": True},
                call_id=503)["credential"]
    tokens = dict(w.manager._credentials)

    # The ONE injected fault: the real socket close raises exactly once.
    fault = {"armed": True}
    real_close = w.manager._server.close

    def failing_close():
        if fault["armed"]:
            fault["armed"] = False
            raise OSError("injected close fault")
        return real_close()

    monkeypatch.setattr(w.manager._server, "close", failing_close)

    error = _refused(w, "collab.owner.stop", call_id=504, code="owner_cleanup_failed")
    assert "Traceback" not in error["message"] and str(w.tmp) not in error["message"]

    status = _ok(w, "collab.owner.status", call_id=505)
    assert status["state"] == "cleanup_failed" and status["retryRequired"] is True
    assert status["endpoint"] == endpoint, "başarısız stop endpoint'i sildi"
    assert status["exportedMembers"] == [ALICE]
    assert w.manager._server is not None, "başarısız stop sahipliği bıraktı"
    assert w.manager._credentials == tokens
    assert _serving(port), "başarısız stop dinleyiciyi öldürdü"
    assert _snapshot_code(endpoint, token) is None
    _no_secret_word(status, "cleanup_failed status")

    # A fresh start is refused while the unresolved cleanup still owns it.
    _refused(w, "collab.owner.start", call_id=506, code="owner_not_configured")
    assert _ok(w, "collab.owner.status", call_id=507)["state"] == "cleanup_failed"

    # The explicit retry really closes the listener and releases everything.
    retried = _ok(w, "collab.owner.stop", call_id=508)
    assert retried["state"] == "stopped" and retried["endpoint"] is None
    assert retried["retryRequired"] is False and retried["exportedMembers"] == []
    assert w.manager._server is None and w.manager._credentials == {}
    assert not _serving(port) and _snapshot_code(endpoint, token) == "connection_error"

    # The SAME configuration comes back, with a new epoch and NEW tokens.
    restarted = _ok(w, "collab.owner.start", call_id=509)
    assert restarted["state"] == "running" and restarted["epoch"] > started["epoch"]
    assert restarted["sessionId"] == SESSION_ID and restarted["baseCommit"] == w.head
    assert restarted["storePath"] == created["storePath"]
    assert restarted["hubPath"] == created["hubPath"]
    assert restarted["revision"] == created["revision"], "yeniden başlatma metadata'yı değiştirdi"
    assert token not in w.manager._credentials.values()
    assert _snapshot_code(restarted["endpoint"], token) == "access_denied"
    fresh = _ok(w, "collab.owner.shareOnce", {"memberId": ALICE, "confirmSecret": True},
                call_id=510)
    assert fresh["credential"] != token
    assert _snapshot_code(restarted["endpoint"], fresh["credential"]) is None
    _no_token(w, token, where="retry sonrası yüzey", allow_ids=(503,))