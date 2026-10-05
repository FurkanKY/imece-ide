"""Collaboration SHARED DELIVERY application E2E: real bridge, real host, real Git.

WHAT IS REAL HERE
-----------------
* a real tiny Git source repository, then TWO INDEPENDENT CHECKOUTS (clones
  ``alice-a`` and ``bob-b``) of the SAME base commit SHA,
* TWO SEPARATE local bare client stores mirroring ONE shared collaboration
  hub — all three live OUTSIDE both checkouts and outside both worktrees,
* the real ``LoopbackServer`` + ``Coordinator`` with two member credentials,
  ONE session, ONE shared context and FOUR task-metadata records,
* the real ``CollaborationHost`` in ``webhost.state``, a real
  ``LoopbackSnapshotClient`` revision consumer and a real private POSIX cursor
  lease,
* the real ``engine_factory`` (real routing/preflight, the real
  ``NativeWorkerAttemptAdapter`` with ``worker_safe_point`` taken from the host
  session),
* ALL FIVE delivery RPCs through the real ``HostBridge`` into the real
  ``SharedDeliveryService`` / ``capture_proposal`` / ``publish_proposal`` /
  ``assemble_candidate`` production path,
* and, for the FINAL candidates, the real ``VerificationRunner`` running the
  real trusted project check as a genuine ``python3 -m pytest -q``
  SUBPROCESS against the materialized candidate directory (so this file must
  be run with an interpreter on PATH that has pytest — i.e. the repo venv).

WHAT IS FAKE (declared up front, never hidden)
----------------------------------------------
* model providers are scripted backends (no network, no real model),
* the NATIVE run's verification process runner is fake: it returns a canned
  PASS so each member's per-task unit check "completes" and the run settles.
  THAT PASS IS NOT EVIDENCE FOR ANY CANDIDATE. Every candidate receipt below
  comes from the REAL pytest subprocess, and the frontend-only /
  backend-only / combined candidates are asserted to FAIL / FAIL / PASS with
  their own distinct, honest exit codes.

HOW THE TWO MEMBERS ARE SIMULATED (read before trusting the numbers)
-------------------------------------------------------------------
This is ONE application host in ONE process with ONE ``HostBridge``. Alice's
and Bob's workflows run SEQUENTIALLY on it, because ``run.start``
legitimately refuses to open a second canonical run while one is
non-terminal, and because every delivery RPC borrows the single ACTIVE run's
idle WAITING_USER workspace. So this file proves "two independent checkouts,
two independent member stores, one shared hub, two independently captured and
published proposals, combined and really verified". It does NOT claim two
simultaneous processes or two machines; the sequential order is an explicit
application-lifecycle fact, not a shortcut around one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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
import webhost.api.run as run_api  # noqa: E402
from agent_runtime import (  # noqa: E402
    ModelStopReason, ModelToolCall, ModelTurn, ModelUsage,
)
from collab_runtime.coordinator import Coordinator  # noqa: E402
from collab_runtime.delivery import SharedDeliveryService  # noqa: E402
from collab_runtime.host import CollaborationHost  # noqa: E402
from collab_runtime.models import build_context, build_initial_state, build_task  # noqa: E402
from collab_runtime.proposals import read_proposal  # noqa: E402
from collab_runtime.store import GitStore  # noqa: E402
from run_runtime import RunRuntime, RunStatus, RunStore  # noqa: E402
from run_runtime.events import RunEventType  # noqa: E402
from webhost import state  # noqa: E402
from webhost.bridge import HostBridge  # noqa: E402

# The Qt application, the bounded RPC/pump helpers and the scripted backends
# are the REAL ones already exercised by the collaboration application E2E.
# Importing them (instead of re-implementing) keeps both files talking about
# the same bridge surface.
from test_collab_application_e2e import (  # noqa: E402
    PLAN_JSON, REVIEW_APPROVED, ROUTING, RecordingBackend, _completed, _drain,
    _git, _process_result, _pump, _reset_active, make_ports_factory,
    qapp, rpc,
)
from test_collab_transport import servers  # noqa: F401,E402  (listener registry fixture)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git yok")

# Captured at import, before any monkeypatching, so a SECOND member's fake
# ports still wrap the REAL factory instead of the first member's wrapper.
_REAL_BUILD_PORTS = engine_factory.build_pipeline_ports


# ----------------------------------------------------------------- the world

SESSION_ID = "delivery-e2e"
TARGET_VERSION = "shared delivery v1"
ALICE, BOB = "alice", "bob"
# Distinct, valid loopback member credentials: [A-Za-z0-9_-]{32,256}.
CREDENTIALS = {ALICE: "delivery-e2e-credential-" + "a" * 40,
               BOB: "delivery-e2e-credential-" + "b" * 40}

# The ONE shared context; the rename instruction both members must honour.
SHARED_GOAL = "Shared goal: rename the response key 'text' to 'message' across the tiny app."
SHARED_DECISIONS = ["frontend and backend must land together"]

# task id -> (owner, goal, scopes): two disjoint owners plus two owners that
# fight over the SAME note file, so every delivery rule has a real fixture.
TASKS = {
    "t-ui": (ALICE, "Frontend: read the renamed response key.", ["frontend.py"]),
    "t-api": (BOB, "Backend: publish the renamed response key.", ["backend.py"]),
    "t-note-a": (ALICE, "Alice rewrites the shared note.", ["conflict.txt"]),
    "t-note-b": (BOB, "Bob rewrites the shared note.", ["conflict.txt"]),
}

BASE_BACKEND = 'GREETING = "hello"\n\nRESPONSE = {"text": GREETING}\n'
BASE_FRONTEND = "from backend import RESPONSE\n\n\ndef display():\n    return RESPONSE[\"text\"]\n"
BASE_NOTE = "alpha\nbeta\ngamma\n"
BASE_APP = 'GREETING = "base"\n'
BASE_CONTRACT = (
    "from backend import RESPONSE\n"
    "from frontend import display\n"
    "\n"
    "\n"
    "def test_backend_publishes_the_message_key():\n"
    '    assert "message" in RESPONSE\n'
    "\n"
    "\n"
    "def test_display_returns_the_greeting():\n"
    '    assert display() == "hello"\n'
)
# Alice's own uncommitted WIP line INSIDE her selected owned file: it must
# survive into the captured artifact, because capture is cumulative from the
# session base for the selected paths.
ALICE_WIP_LINE = 'USER_WIP = "alice-local-note"\n'
ALICE_WIP_FRONTEND = BASE_FRONTEND + "\n" + ALICE_WIP_LINE
# Alice's fake model writes ONLY her owned file and keeps the WIP line.
ALICE_FRONTEND_AFTER = (
    "from backend import RESPONSE\n"
    "\n"
    + ALICE_WIP_LINE
    + "\n"
    "\n"
    "def display():\n"
    '    return RESPONSE["message"]\n'
)
BOB_BACKEND_AFTER = 'GREETING = "hello"\n\nRESPONSE = {"message": GREETING}\n'
ALICE_NOTE_AFTER = "alpha\nALICE\nbeta\ngamma\n"
BOB_NOTE_AFTER = "alpha\nBOB\nbeta\ngamma\n"
# A mutation applied to the worktree AFTER a preview was taken: the ticket must
# still publish the pre-mutation bytes.
LATE_EDIT = ALICE_FRONTEND_AFTER + '\nLATE_EDIT = "after the preview"\n'
# Alice's pre-run WIP OUTSIDE every selected owned path: never selectable,
# never serialized, still in her checkout at the end.
ALICE_SCRATCH = "alice private scratch\n"

# Exact DTO shapes (no raw code / no base64 / no credentials).
PREVIEW_KEYS = {
    "previewId", "proposalId", "runId", "sessionId", "taskId", "owner", "baseCommit",
    "contextRevision", "contextHash", "expectedRevision", "paths", "outOfScopePaths",
    "fileCount", "artifactBytes", "contentDigest", "warnings",
}
PUBLISH_KEYS = {
    "proposalId", "sessionRevision", "proposalRevision", "contextRevision", "contextHash",
    "paths", "outOfScopePaths", "outOfScopeAuthorized",
}
LIST_KEYS = {
    "proposalId", "proposalRevision", "taskId", "owner", "sessionId", "baseCommit",
    "contextRevision", "contextHash", "fileCount", "currentContextHashMatches",
}
CANDIDATE_KEYS = {
    "command", "session_id", "session_revision", "binding_revision", "context_hash",
    "base_commit", "proposal_ids", "proposals", "candidate_dir", "file_count",
    "content_fingerprint", "conflicts", "verification", "notes",
}
VERIFICATION_KEYS = {
    "status", "plan_id", "checks", "changed_content", "fingerprint_before",
    "fingerprint_after", "fingerprint_complete",
}
CHECK_KEYS = {"check_id", "status", "exit_code", "timed_out"}
_B64 = re.compile(r"^[A-Za-z0-9+/]{120,}={0,2}$")


# --------------------------------------------------------------- tiny app ---


def _git_clone(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--config", "core.autocrlf=false", "-q", str(source), str(target)], check=True,
                   capture_output=True, text=True)


def _make_tiny_app(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    _git(["config", "user.name", "T"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "backend.py").write_text(BASE_BACKEND, encoding="utf-8")
    (root / "frontend.py").write_text(BASE_FRONTEND, encoding="utf-8")
    (root / "conflict.txt").write_text(BASE_NOTE, encoding="utf-8")
    (root / "app.py").write_text(BASE_APP, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_contract.py").write_text(BASE_CONTRACT, encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "tiny app base"], root)
    return _git(["rev-parse", "HEAD"], root)


# --------------------------------------------------------- wire invariants ---


def _no_secret(*blobs) -> None:
    """No configured member credential in any host-visible or stored surface."""
    for blob in blobs:
        text = blob if isinstance(blob, str) else json.dumps(blob, ensure_ascii=False, default=str)
        for credential in CREDENTIALS.values():
            assert credential not in text, "bir üye kimlik bilgisi köprü yüzeyine sızdı"


def _assert_clean(reply, result_keys=None) -> dict:
    """No configured credential and no base64 blob anywhere in a reply."""
    _no_secret(reply)
    if result_keys is not None:
        assert set(reply["result"]) == set(result_keys), sorted(reply["result"])
    for value in _walk(reply):
        if isinstance(value, str):
            assert not _B64.match(value), f"ham içerik/base64 bir DTO'ya sızdı: {value[:24]}..."
    return reply


def _walk(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)
    else:
        yield value


def _preview_dto(reply):
    assert reply["ok"], reply
    dto = reply["result"]
    assert set(dto) == PREVIEW_KEYS, sorted(set(dto) ^ PREVIEW_KEYS)
    return _assert_clean(reply, PREVIEW_KEYS)["result"]


def _publish_dto(reply):
    assert reply["ok"], reply
    dto = reply["result"]
    assert set(dto) == PUBLISH_KEYS, sorted(set(dto) ^ PUBLISH_KEYS)
    return _assert_clean(reply, PUBLISH_KEYS)["result"]


def _candidate_dto(reply):
    assert reply["ok"], reply
    result = reply["result"]
    assert set(result) == {"candidate", "conflicts"}, sorted(result)
    receipt = result["candidate"]
    assert receipt is None or set(receipt) == CANDIDATE_KEYS, sorted(set(receipt or ()))
    if receipt is not None:
        assert set(receipt["verification"]) <= VERIFICATION_KEYS, receipt["verification"]
        for check in receipt["verification"]["checks"]:
            assert set(check) == CHECK_KEYS, sorted(check)
    return _assert_clean(reply)["result"]


def _checkout_state(root: Path) -> tuple:
    """Everything a delivery RPC must never touch: HEAD, index/worktree
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


def _await_settled(w, run_id, timeout=120.0) -> dict:
    """Wait for THIS run's run.finished plus its worker/retire tail."""
    assert _pump(lambda: sum(1 for e in w.events if e["channel"] == "run.finished"
                             and e["payload"].get("runId") == run_id) > 0, timeout), (
        "run.finished gelmedi")
    worker = run_api._active.get("worker")
    if worker is not None:
        assert _pump(lambda: not worker.isRunning(), 40), "worker QThread bitmedi"
    _pump(lambda: not run_api._draining_workers, 20)
    index = [i for i, e in enumerate(w.events)
             if e["channel"] == "run.finished" and e["payload"].get("runId") == run_id][0]
    return w.events[index]["payload"]


def _event_types(w, run_id):
    return [e.type for e in w.runtime.events(run_id, limit=500).events]


# ------------------------------------------------------------------ fixture -


@pytest.fixture
def w(tmp_path, monkeypatch, qapp, servers):
    """One app host, two checkouts, two stores, one shared hub."""
    origin = tmp_path / "origin"
    head = _make_tiny_app(origin)
    clone_a, clone_b = tmp_path / "alice-a", tmp_path / "bob-b"
    _git_clone(origin, clone_a)
    _git_clone(origin, clone_b)
    assert _git(["rev-parse", "HEAD"], clone_a) == head == _git(["rev-parse", "HEAD"], clone_b)

    # Alice's own uncommitted WIP: one line inside her owned file, one
    # untracked file outside every owned path.
    (clone_a / "frontend.py").write_text(ALICE_WIP_FRONTEND, encoding="utf-8")
    (clone_a / "notes.txt").write_text(ALICE_SCRATCH, encoding="utf-8")

    workspaces = tmp_path / "workspaces"
    monkeypatch.setattr(runtime_paths, "workspaces_dir", lambda: workspaces)
    monkeypatch.setattr(engine_factory, "workspaces_dir", lambda: workspaces)
    monkeypatch.setattr(ui_prefs, "load",
                        lambda: {**ui_prefs.DEFAULTS, "ai_engine": "auto", "decision_layer": "off"})

    runtime = RunRuntime(RunStore(tmp_path / "runtime.sqlite3"))
    state.set_project(str(clone_a))
    state.set_run_runtime(runtime)
    state.set_delivery_service(None)

    trust = tmp_path / "private"
    trust.mkdir(mode=0o700)
    cursor_root = trust / "collab-cursors"
    host = CollaborationHost(cursor_root)
    state.set_collaboration_host(host)

    hub = GitStore.create_bare(tmp_path / "hub.git", what="hub")
    store_a_path = GitStore.create_bare(tmp_path / "store-a.git", what="store")
    store_b_path = GitStore.create_bare(tmp_path / "store-b.git", what="store")
    store_a = GitStore(store=store_a_path, remote=str(hub))
    store_b = GitStore(store=store_b_path, remote=str(hub))

    revision = store_a.init_session(build_initial_state(
        session_id=SESSION_ID, target_version=TARGET_VERSION, base_commit=head))
    revision = store_a.update_context(build_context(
        goal=SHARED_GOAL, decisions=list(SHARED_DECISIONS), interfaces={}),
        expected_revision=revision)
    context_revision = revision
    for task_id, (owner, goal, scopes) in TASKS.items():
        revision = store_a.upsert_task(build_task(
            task_id=task_id, owner=owner, goal=goal, scopes=list(scopes),
            status="running", context_revision=context_revision),
            expected_revision=revision)
    baseline_revision = revision

    coordinator = Coordinator(store_a, session_id=SESSION_ID, owner_id=ALICE,
                              member_credentials=dict(CREDENTIALS))
    listener = servers(coordinator).start()

    bridge = HostBridge()
    events: list[dict] = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))

    world = SimpleNamespace(
        tmp=tmp_path, origin=origin, head=head, clone_a=clone_a, clone_b=clone_b,
        workspaces=workspaces, host=host, trust=trust, cursor_root=cursor_root,
        hub=hub, store_a=store_a, store_b=store_b,
        store_a_path=store_a_path, store_b_path=store_b_path,
        coordinator=coordinator, base_url=listener.base_url,
        runtime=runtime, bridge=bridge, events=events,
        context_revision=context_revision, baseline_revision=baseline_revision,
        monkeypatch=monkeypatch, release=threading.Event(),
    )
    _reset_active()
    try:
        yield world
    finally:
        world.release.set()
        _drain()
        state.set_delivery_service(None)


def _store_of(w, member):
    return w.store_a_path if member == ALICE else w.store_b_path


def _install_fake_ports(w, *, planner, worker, reviewer, process_results):
    """Wrap the REAL factory with scripted roles + a fake native verification.

    Each call re-points the attribute at the real factory first, so the second
    member's fake never nests inside the first member's wrapper.
    """
    w.monkeypatch.setattr(engine_factory, "build_pipeline_ports", _REAL_BUILD_PORTS)
    factory = make_ports_factory(planner=planner, worker=worker, reviewer=reviewer,
                                 process_results=process_results)
    w.monkeypatch.setattr(engine_factory, "build_pipeline_ports", factory)
    return factory


def _write_turn_uniq(call_id, path, content):
    """A write turn with a DISTINCT tool-call id (a repeated id is a replay and
    the real agent refuses it, which would mask the delivery behaviour)."""
    return ModelTurn("", (ModelToolCall(call_id, "write_file", {"path": path, "content": content}),),
                     ModelStopReason.TOOL_USE, ModelUsage())


def _member_run(w, *, member, task_id, writes, call_id=100):
    """preview -> approve -> run.start -> WAITING_USER over the real bridge.

    Each member gets its OWN three backend instances (independent LLM sessions
    per role). ``writes`` is the ordered list of ``(path, content)`` pairs the
    fake worker writes, followed by a closing turn.
    """
    owner, goal, scopes = TASKS[task_id]
    assert owner == member
    turns = [_write_turn_uniq(f"{task_id}-{index}", path, content)
             for index, (path, content) in enumerate(writes)]
    turns.append(_completed("owned file(s) rewritten."))
    worker = RecordingBackend(turns)
    _install_fake_ports(
        w, planner=RecordingBackend([_completed(PLAN_JSON)]), worker=worker,
        reviewer=RecordingBackend([_completed(REVIEW_APPROVED)]),
        process_results=[_process_result(0)])          # fake native PASS per task

    preview = rpc(w.bridge, "collab.preview", {
        "endpoint": w.base_url, "credential": CREDENTIALS[member],
        "memberId": member, "taskId": task_id,
    }, call_id=call_id)
    assert preview["ok"], preview
    dto = preview["result"]
    assert dto["task"]["owner"] == member and dto["task"]["scopes"] == scopes
    assert dto["baseCommit"] == w.head and dto["sessionId"] == SESSION_ID
    _no_secret(preview)

    approval = rpc(w.bridge, "collab.approve", {"previewId": dto["previewId"]},
                   call_id=call_id + 1)
    assert approval["ok"], approval
    _no_secret(approval)

    started = rpc(w.bridge, "run.start", {
        "task": goal, "routing": ROUTING,
        "mentions": [path for path, _ in writes],
        "collabApprovalHandle": approval["result"]["approvalHandle"],
    }, call_id=call_id + 2)
    assert started["ok"], started
    run_id = started["result"]["runId"]
    session = run_api._active["collab_session"]
    workspace = run_api._active["workspace"]

    finished = _await_settled(w, run_id)
    assert finished["status"] == "done", json.dumps(finished, ensure_ascii=False)
    assert w.runtime.get_run(run_id).status is RunStatus.WAITING_USER
    assert session is not None and session.active is False
    assert RunEventType.PROPOSAL_READY in _event_types(w, run_id)
    return SimpleNamespace(
        run_id=run_id, session=session, workspace=workspace,
        workspace_root=Path(workspace.root), worker=worker,
        proposals=list(run_api._active["proposals"] or []),
        active_paths=tuple(p["path"] for p in (run_api._active["proposals"] or [])),
    )


def _rpc(w, method, params, call_id=1, timeout=120.0):
    return rpc(w.bridge, method, params, call_id=call_id, timeout=timeout)


def _publish(w, run_id, member, paths, *, call_id=500, proposal_id=None,
             mutate_after_preview=None, allow_out_of_scope=None):
    """Real preview (+ optional worktree mutation) then the real publish."""
    params = {"runId": run_id, "storePath": str(_store_of(w, member)),
              "hubPath": str(w.hub), "paths": list(paths)}
    if proposal_id is not None:
        params["proposalId"] = proposal_id
    ticket = _preview_dto(_rpc(w, "collab.delivery.preview", params, call_id=call_id))
    if mutate_after_preview is not None:
        mutate_after_preview()
    publish_params = {"runId": run_id, "previewId": ticket["previewId"]}
    if allow_out_of_scope is not None:
        publish_params["allowOutOfScope"] = allow_out_of_scope
    return ticket, _publish_dto(
        _rpc(w, "collab.delivery.publish", publish_params, call_id=call_id + 1))


# ==================================================== 1. two members, one hub


def test_two_members_publish_to_one_private_hub_and_only_the_combined_candidate_verifies(w):
    """Two clones, two stores, one hub: publish, list, then FAIL / FAIL / PASS.

    Proves over the real bridge only:
    * each member captures from its OWN real Git worktree root, never from the
      original checkout and never including WIP outside the selected paths,
    * publication is a private hub ref plus an unchanged-state metadata child
      (the store never auto-recomputes anything),
    * ``collab.delivery.list`` is metadata-only,
    * the frontend-only and backend-only candidates FAIL against the REAL
      pytest subprocess while the combined one PASSES,
    * ``verify=false`` stays ``not_run`` (never an implicit success),
    * neither original checkout moves a single byte, and no credential leaks.
    """
    # ---- member A: alice / t-ui, with her own pre-run WIP ------------------
    alice_checkout = _checkout_state(w.clone_a)
    alice = _member_run(w, member=ALICE, task_id="t-ui",
                        writes=[("frontend.py", ALICE_FRONTEND_AFTER)], call_id=100)
    assert alice.active_paths == ("frontend.py",), alice.active_paths
    # The fake model touched ONLY her owned file: every other source is base.
    assert (alice.workspace_root / "notes.txt").read_text(encoding="utf-8") == ALICE_SCRATCH
    assert (alice.workspace_root / "backend.py").read_text(encoding="utf-8") == BASE_BACKEND
    assert (alice.workspace_root / "app.py").read_text(encoding="utf-8") == BASE_APP
    assert alice.worker.sessions == 1

    # The UI caller may only select paths that are in the ACTIVE proposal set.
    refused = _rpc(w, "collab.delivery.preview", {
        "runId": alice.run_id, "storePath": str(w.store_a_path), "hubPath": str(w.hub),
        "paths": ["conflict.txt"]}, call_id=140)
    assert not refused["ok"] and refused["error"]["code"] == "delivery_invalid", refused
    _no_secret(refused)

    ticket_a, receipt_a = _publish(w, alice.run_id, ALICE, ["frontend.py"], call_id=150)
    assert ticket_a["paths"] == ["frontend.py"] and ticket_a["outOfScopePaths"] == []
    assert ticket_a["owner"] == ALICE and ticket_a["taskId"] == "t-ui"
    assert ticket_a["fileCount"] == 1 and ticket_a["artifactBytes"] > 0
    assert ticket_a["expectedRevision"] == w.baseline_revision
    assert "cumulative from the session base" in ticket_a["warnings"][1]
    assert "out-of-scope paths require explicit confirmation" in ticket_a["warnings"][2]
    assert receipt_a["proposalId"] == ticket_a["proposalId"]

    # The recorded provenance is the binding the run ACCEPTED, not a recompute
    # and not the bare preview prefix.
    binding = alice.session.accepted_binding
    assert receipt_a["contextRevision"] == binding.revision == w.baseline_revision
    assert receipt_a["contextHash"] == binding.context_hash
    # The metadata child carries UNCHANGED state: publishing advanced the
    # session revision by one child commit, without recomputing anything, and
    # the proposal root is a separate parentless commit.
    assert receipt_a["sessionRevision"] != w.baseline_revision
    assert receipt_a["sessionRevision"] != receipt_a["proposalRevision"]
    assert w.store_a.is_ancestor(w.context_revision, receipt_a["contextRevision"])
    assert receipt_a["proposalRevision"] == w.store_a.remote_proposal_head(
        receipt_a["proposalId"])
    # ...and it recomputed nothing: every task and the shared context are the
    # exact metadata records the global setup published.
    _head, live = w.store_a.fetch_state()
    assert live.context.goal == SHARED_GOAL and live.context.decisions == tuple(SHARED_DECISIONS)
    assert {tid: (t.owner, t.goal, t.scopes) for tid, t in live.tasks.items()} == {
        tid: (owner, goal, tuple(scopes)) for tid, (owner, goal, scopes) in TASKS.items()}
    assert binding.to_dict() == alice.session.accepted_binding.to_dict()
    assert binding is not alice.session.accepted_binding       # a clone per read

    # The published artifact is the real frozen capture; her unselected WIP
    # file was never serialized.
    proposal_a = read_proposal(w.store_a, receipt_a["proposalId"])
    assert [f.path for f in proposal_a.files] == ["frontend.py"]
    after_a = proposal_a.files[0].after_bytes.decode("utf-8")
    assert ALICE_WIP_LINE in after_a and 'RESPONSE["message"]' in after_a
    assert ALICE_SCRATCH not in after_a and "app.py" not in after_a

    # Release the canonical run WITHOUT applying her checkout work.
    rejected = rpc(w.bridge, "run.rejectProposals", {}, call_id=170)
    assert rejected["ok"] and rejected["result"] == {}, rejected
    assert run_api._active["proposals"] == []
    assert RunEventType.PROPOSAL_REJECTED in _event_types(w, alice.run_id)
    assert _checkout_state(w.clone_a) == alice_checkout

    # ---- member B: bob / t-api, a different checkout and store -------------
    state.set_project(str(w.clone_b))
    bob_checkout = _checkout_state(w.clone_b)
    bob = _member_run(w, member=BOB, task_id="t-api",
                      writes=[("backend.py", BOB_BACKEND_AFTER)], call_id=200)
    assert bob.active_paths == ("backend.py",), bob.active_paths
    assert (bob.workspace_root / "frontend.py").read_text(encoding="utf-8") == BASE_FRONTEND
    assert bob.worker is not alice.worker

    ticket_b, receipt_b = _publish(w, bob.run_id, BOB, ["backend.py"], call_id=250)
    assert ticket_b["paths"] == ["backend.py"] and ticket_b["owner"] == BOB
    proposal_b = read_proposal(w.store_b, receipt_b["proposalId"])
    assert [f.path for f in proposal_b.files] == ["backend.py"]

    # ---- both members see each other over ONE hub, metadata only -----------
    listed = _rpc(w, "collab.delivery.list", {
        "runId": bob.run_id, "storePath": str(w.store_b_path), "hubPath": str(w.hub)},
        call_id=260)
    assert listed["ok"], listed
    assert set(listed["result"]) == {"proposals"}
    entries = {}
    for entry in listed["result"]["proposals"]:
        assert set(entry) == LIST_KEYS, sorted(set(entry) ^ LIST_KEYS)
        entries[entry["proposalId"]] = entry
    _assert_clean(listed, {"proposals"})
    assert set(entries) == {receipt_a["proposalId"], receipt_b["proposalId"]}, entries
    assert entries[receipt_a["proposalId"]]["owner"] == ALICE
    assert entries[receipt_b["proposalId"]]["owner"] == BOB
    assert all(e["fileCount"] == 1 and e["baseCommit"] == w.head for e in entries.values())
    assert all(e["currentContextHashMatches"] for e in entries.values())

    # ---- real candidate verification: FAIL / FAIL / PASS -------------------
    out = w.tmp / "candidates"
    out.mkdir()

    def assemble(name, ids, *, verify, call_id):
        return _candidate_dto(_rpc(w, "collab.delivery.candidate", {
            "runId": bob.run_id, "storePath": str(w.store_b_path), "hubPath": str(w.hub),
            "proposalIds": list(ids), "outputPath": str(out / name),
            "verify": verify}, call_id=call_id))

    both = [receipt_a["proposalId"], receipt_b["proposalId"]]
    frontend_only = assemble("frontend-only", [receipt_a["proposalId"]], verify=True, call_id=300)
    backend_only = assemble("backend-only", [receipt_b["proposalId"]], verify=True, call_id=310)
    combined = assemble("combined", both, verify=True, call_id=320)

    # The REAL trusted pytest subprocess disagreed in all three cases, with its
    # own honest exit codes -- not one canned fake PASS reused everywhere.
    outcomes = {name: (r["candidate"]["verification"]["status"],
                       [c["exit_code"] for c in r["candidate"]["verification"]["checks"]],
                       [c["check_id"] for c in r["candidate"]["verification"]["checks"]])
                for name, r in (("frontend-only", frontend_only), ("backend-only", backend_only),
                                ("combined", combined))}
    if os.name == "nt" and all(
            result["candidate"]["verification"].get("fingerprint_complete") is False
            and not result["candidate"]["verification"]["checks"]
            for result in (frontend_only, backend_only, combined)):
        assert all(outcome[0] == "error" for outcome in outcomes.values()), outcomes
        assert combined["candidate"]["proposal_ids"] == sorted(both)
        pytest.skip("Windows safe pre-verification fingerprint is incomplete; no checks executed")
    assert outcomes["frontend-only"][0] == "fail", outcomes
    assert outcomes["backend-only"][0] == "fail", outcomes
    assert outcomes["combined"][0] == "pass", outcomes
    assert outcomes["frontend-only"][1] == [1] and outcomes["backend-only"][1] == [1], outcomes
    assert outcomes["combined"][1] == [0], outcomes
    assert {tuple(o[2]) for o in outcomes.values()} == {("python_pytest",)}, outcomes

    for result in (frontend_only, backend_only, combined):
        receipt = result["candidate"]
        assert result["conflicts"] == []
        assert receipt["verification"]["plan_id"] is not None
        assert receipt["verification"]["fingerprint_complete"] is True
        assert receipt["verification"]["changed_content"] is False
        assert (receipt["verification"]["fingerprint_before"] ==
                receipt["verification"]["fingerprint_after"])
        assert receipt["base_commit"] == w.head
        assert receipt["command"] == "candidate"
    assert combined["candidate"]["proposal_ids"] == sorted(both)

    # Only the combined candidate really satisfies the shared contract.
    assert (out / "combined" / "backend.py").read_text(encoding="utf-8") == BOB_BACKEND_AFTER
    assert (out / "combined" / "frontend.py").read_text(encoding="utf-8") == ALICE_FRONTEND_AFTER
    assert (out / "frontend-only" / "backend.py").read_text(encoding="utf-8") == BASE_BACKEND
    assert (out / "frontend-only" / "tests" / "test_contract.py").read_text(
        encoding="utf-8") == BASE_CONTRACT

    # verify=false is explicit, defaults off, and is NEVER an implicit pass.
    unverified = assemble("combined-unverified", both, verify=False, call_id=330)
    assert unverified["candidate"]["verification"] == {
        "status": "not_run", "plan_id": None, "checks": [], "changed_content": False}
    default_verify = _candidate_dto(_rpc(w, "collab.delivery.candidate", {
        "runId": bob.run_id, "storePath": str(w.store_b_path), "hubPath": str(w.hub),
        "proposalIds": both, "outputPath": str(out / "default-verify")}, call_id=340))
    assert default_verify["candidate"]["verification"]["status"] == "not_run"

    # ---- neither original checkout moved ----------------------------------
    assert _checkout_state(w.clone_a) == alice_checkout
    assert _checkout_state(w.clone_b) == bob_checkout
    assert not (w.clone_a / ".imece").exists() and not (w.clone_b / ".imece").exists()
    assert (w.clone_a / "notes.txt").read_text(encoding="utf-8") == ALICE_SCRATCH
    # (the shared `_git` helper strips the output, hence the missing XY marker
    # space: exactly Alice's two own pre-run WIP entries and nothing else)
    assert set(_git(["status", "--porcelain"], w.clone_a).splitlines()) == {
        "M frontend.py", "?? notes.txt"}
    assert _git(["status", "--porcelain"], w.clone_b) == ""

    _no_secret(w.events, listed, combined, frontend_only, backend_only)


# ========================= 2. frozen preview artifact + a moved context revision


def test_preview_freezes_the_artifact_and_a_moved_context_refuses_preview_and_confirm(w):
    """A preview ticket is immutable, and a new context revision refuses both a
    NEW preview and the already-taken ticket -- with NO orphan ref."""
    alice = _member_run(w, member=ALICE, task_id="t-ui",
                        writes=[("frontend.py", ALICE_FRONTEND_AFTER)], call_id=400)
    checkout_before = _checkout_state(w.clone_a)
    target = alice.workspace_root / "frontend.py"

    def mutate():
        target.write_text(LATE_EDIT, encoding="utf-8", newline="\n")  # controlled post-preview bytes

    # (1) capture, mutate the worktree AFTER the preview, then publish.
    _ticket1, first = _publish(w, alice.run_id, ALICE, ["frontend.py"], call_id=450,
                               mutate_after_preview=mutate)
    assert target.read_text(encoding="utf-8") == LATE_EDIT
    # The published artifact is EXACTLY the frozen pre-mutation capture.
    frozen = read_proposal(w.store_a, first["proposalId"]).files[0]
    assert frozen.after_bytes.decode("utf-8") == ALICE_FRONTEND_AFTER
    assert "LATE_EDIT" not in frozen.after_bytes.decode("utf-8")

    # (2) a SECOND capture really does read the live worktree, which proves (1)
    # was frozen rather than merely late.
    _ticket2, second = _publish(w, alice.run_id, ALICE, ["frontend.py"], call_id=460)
    live = read_proposal(w.store_a, second["proposalId"]).files[0]
    assert live.after_bytes.decode("utf-8") == LATE_EDIT
    assert first["proposalId"] != second["proposalId"]

    # (3) a ticket taken at revision C, then C moves to C+1.
    ticket = _preview_dto(_rpc(w, "collab.delivery.preview", {
        "runId": alice.run_id, "storePath": str(w.store_a_path), "hubPath": str(w.hub),
        "paths": ["frontend.py"]}, call_id=470))
    orphan_id = ticket["proposalId"]
    # No ref exists before the explicit publish step.
    assert w.store_a.remote_proposal_head(orphan_id) is None
    assert w.store_a.remote_proposal_head(first["proposalId"]) is not None

    head, _state = w.store_a.fetch_state()
    moved = w.coordinator.update_context(
        CREDENTIALS[ALICE],
        build_context(goal=SHARED_GOAL + " Follow-up: also log the rename.",
                      decisions=list(SHARED_DECISIONS), interfaces={}),
        expected_revision=head)
    assert moved != head
    assert alice.session.accepted_binding.revision != moved

    # A NEW preview against the moved context is refused...
    refused = _rpc(w, "collab.delivery.preview", {
        "runId": alice.run_id, "storePath": str(w.store_a_path), "hubPath": str(w.hub),
        "paths": ["frontend.py"]}, call_id=480)
    assert not refused["ok"] and refused["error"]["code"] == "delivery_stale", refused
    _no_secret(refused)

    # ...and so is the ticket taken BEFORE the move, with no orphan left.
    refused_confirm = _rpc(w, "collab.delivery.publish", {
        "runId": alice.run_id, "previewId": ticket["previewId"]}, call_id=481)
    assert not refused_confirm["ok"], refused_confirm
    assert refused_confirm["error"]["code"] == "delivery_stale", refused_confirm
    _no_secret(refused_confirm)
    assert w.store_a.remote_proposal_head(orphan_id) is None, "başarısız yayın orphan ref bıraktı"

    # Discarding is memory-only and idempotent; it publishes nothing.
    assert _rpc(w, "collab.delivery.discard",
                {"previewId": "delivery-preview-never-issued"}, call_id=490)["result"] == {}
    assert _rpc(w, "collab.delivery.discard",
                {"previewId": ticket["previewId"]}, call_id=491)["result"] == {}
    assert w.store_a.remote_proposal_head(orphan_id) is None

    assert _checkout_state(w.clone_a) == checkout_before
    _no_secret(w.events)


# ============================= 3. the real borrow lease gates every other writer


class _GatedDeliveryService:
    """A thin, explicit wrapper around the REAL ``SharedDeliveryService``.

    ``state.set_delivery_service`` is the documented injection hook, so the
    real ``webhost.api.delivery`` handlers still run their real async plumbing
    and their real ``borrow_delivery_context`` guard; only the moment between
    "borrow granted" and "first selected-file read" is made observable. No
    production object, consumer, lease or private attribute is touched.
    """

    def __init__(self, inner):
        self._inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()
        self.saw_borrow = False

    def _hold(self):
        self.saw_borrow = run_api._delivery_is_busy(
            run_api._active.get("collab_session"), run_api._active.get("run_id"))
        self.entered.set()
        assert self.release.wait(90), "teslimat işi bırakılmadı"

    def preview_publication(self, *args, **kwargs):
        self._hold()
        return self._inner.preview_publication(*args, **kwargs)

    def confirm_publication(self, *args, **kwargs):
        return self._inner.confirm_publication(*args, **kwargs)

    def discard_publication(self, *args, **kwargs):
        return self._inner.discard_publication(*args, **kwargs)

    def list_shared_proposals(self, *args, **kwargs):
        return self._inner.list_shared_proposals(*args, **kwargs)

    def assemble_shared_candidate(self, *args, **kwargs):
        return self._inner.assemble_shared_candidate(*args, **kwargs)


def test_a_background_capture_holds_the_real_borrow_and_blocks_every_other_writer(w):
    """One in-flight real capture lease gates apply / reject / follow-up / a new
    run, and shutdown keeps the retained resources until the job releases it."""
    alice = _member_run(w, member=ALICE, task_id="t-ui",
                        writes=[("frontend.py", ALICE_FRONTEND_AFTER)], call_id=600)
    checkout_before = _checkout_state(w.clone_a)
    worktree = alice.workspace_root
    session = alice.session

    gate = _GatedDeliveryService(SharedDeliveryService())
    state.set_delivery_service(gate)

    replies: list[dict] = []
    collector = lambda raw: replies.append(json.loads(raw))  # noqa: E731
    w.bridge.reply.connect(collector)
    try:
        w.bridge.call(json.dumps({"id": 700, "method": "collab.delivery.preview", "params": {
            "runId": alice.run_id, "storePath": str(w.store_a_path),
            "hubPath": str(w.hub), "paths": ["frontend.py"]}}))
        # The gate sits AFTER the borrow was granted and BEFORE any file read.
        assert _pump(gate.entered.is_set, 30), "arka plan yakalama hiç başlamadı"
        assert gate.saw_borrow is True, "model borrow OLMADAN dosya okumaya başladı"
        assert run_api._delivery_is_busy(session, alice.run_id) is True

        for method, params, call_id in (
            ("run.applyProposals", {"paths": ["frontend.py"]}, 701),
            ("run.rejectProposals", {}, 702),
            ("run.followUp", {"feedback": "devam et"}, 703),
            ("run.start", {"task": "ikinci koşu", "routing": ROUTING}, 704),
        ):
            reply = rpc(w.bridge, method, params, call_id=call_id)
            assert not reply["ok"], (method, reply)
            assert reply["error"]["code"] == "busy", (method, reply)

        # Nothing was written, no proposal was cleared, no new run started.
        assert run_api._active["proposals"] == alice.proposals
        assert _checkout_state(w.clone_a) == checkout_before
        assert run_api._active["run_id"] == alice.run_id
        assert worktree.is_dir()
        types = _event_types(w, alice.run_id)
        assert RunEventType.PROPOSAL_APPLIED not in types
        assert RunEventType.PROPOSAL_REJECTED not in types
        assert types.count(RunEventType.RUN_WAITING_USER) == 1

        # Shutdown while the job holds the lease: the resources are RETAINED.
        run_api.shutdown()
        assert run_api._active["workspace"] is not None
        assert worktree.is_dir()
        assert any(entry[1] is session for entry in run_api._draining_workers)
        assert (worktree / "frontend.py").read_text(encoding="utf-8") == ALICE_FRONTEND_AFTER

        # Releasing the gate ends the job. The lease drops BEFORE the drain, so
        # that same drain actually runs (it skips busy sessions).
        gate.release.set()
        assert _pump(lambda: any(r.get("id") == 700 for r in replies), 60), "delivery yanıtı yok"
        assert _pump(lambda: not run_api._draining_workers, 30), "kaynaklar serbest bırakılmadı"
        answer = next(r for r in replies if r.get("id") == 700)
        assert answer["ok"], answer
        assert set(answer["result"]) == PREVIEW_KEYS
        assert answer["result"]["paths"] == ["frontend.py"]
        _assert_clean(answer, PREVIEW_KEYS)

        # No zombie lease or closing marker is left behind, and nothing exists.
        assert run_api._delivery_leases == {}
        assert run_api._delivery_closing == set()
        assert run_api._delivery_is_busy() is False
        assert not worktree.exists()
        assert run_api._active["workspace"] is None
        assert run_api._active["collab_session"] is None
        # The getter still clones with proper value equality after the cleanup.
        assert session.accepted_binding is not None
        assert (session.accepted_binding is not session.accepted_binding and
                session.accepted_binding.to_dict() == session.accepted_binding.to_dict())
        assert _checkout_state(w.clone_a) == checkout_before
    finally:
        w.bridge.reply.disconnect(collector)
        gate.release.set()
    _no_secret(w.events)


# ============================= 4. a real three-way conflict, structured only


def test_two_published_proposals_on_the_same_file_conflict_with_paths_and_no_output(w):
    """Two tasks, two members, ONE file: the real three-way conflict is
    reported as structured paths and never materializes or verifies."""
    alice = _member_run(w, member=ALICE, task_id="t-note-a",
                        writes=[("conflict.txt", ALICE_NOTE_AFTER)], call_id=800)
    _ticket_a, receipt_a = _publish(w, alice.run_id, ALICE, ["conflict.txt"], call_id=820)

    # Free the canonical run WITHOUT applying her checkout work, so the next
    # member can bind its own run on its own checkout.
    released = rpc(w.bridge, "run.rejectProposals", {}, call_id=830)
    assert released["ok"] and released["result"] == {}, released
    assert RunEventType.PROPOSAL_REJECTED in _event_types(w, alice.run_id)
    assert (w.clone_a / "conflict.txt").read_text(encoding="utf-8") == BASE_NOTE

    state.set_project(str(w.clone_b))
    bob = _member_run(w, member=BOB, task_id="t-note-b",
                      writes=[("conflict.txt", BOB_NOTE_AFTER)], call_id=840)
    _ticket_b, receipt_b = _publish(w, bob.run_id, BOB, ["conflict.txt"], call_id=860)
    assert receipt_a["proposalId"] != receipt_b["proposalId"]

    out = w.tmp / "candidates"
    out.mkdir()
    target = out / "conflicted"
    conflicted = _candidate_dto(_rpc(w, "collab.delivery.candidate", {
        "runId": bob.run_id, "storePath": str(w.store_b_path), "hubPath": str(w.hub),
        "proposalIds": [receipt_a["proposalId"], receipt_b["proposalId"]],
        "outputPath": str(target), "verify": True}, call_id=880))
    # A conflict is a structured RESULT, not a transport error.
    assert conflicted["candidate"] is None
    assert conflicted["conflicts"] == ["conflict.txt"], conflicted
    assert not target.exists(), "çakışan birleşim hiçbir dizin oluşturdu"
    assert sorted(p.name for p in out.iterdir()) == []

    # Each single proposal alone is still usable, and still unverified here.
    single = _candidate_dto(_rpc(w, "collab.delivery.candidate", {
        "runId": bob.run_id, "storePath": str(w.store_b_path), "hubPath": str(w.hub),
        "proposalIds": [receipt_b["proposalId"]],
        "outputPath": str(out / "bob-only"), "verify": False}, call_id=890))
    assert single["conflicts"] == []
    assert (out / "bob-only" / "conflict.txt").read_text(encoding="utf-8") == BOB_NOTE_AFTER
    _no_secret(w.events)


# ============================================ 5. trust boundaries and shapes


@pytest.mark.parametrize("case", [
    "paths_not_a_list",
    "unexpected_key",
    "verify_string_before_any_exec",
    "duplicate_proposal_ids",
    "store_inside_checkout",
    "network_hub_url",
    "wrong_realm_hub",
    "missing_store_path",
    "out_of_scope_without_consent",
])
def test_delivery_refuses_forged_shapes_and_untrusted_placement_without_writing_anything(w, case):
    """Every refusal is a FIXED error code and the source checkout is untouched.

    The out-of-scope case additionally proves the ticket was refused, NOT
    consumed: an explicit ``allowOutOfScope`` consent is still required, and
    the unselected owned file never reaches the artifact.
    """
    # Alice changes her owned file AND an out-of-scope file, so the live active
    # proposal set really contains an out-of-scope selectable path.
    alice = _member_run(w, member=ALICE, task_id="t-ui",
                        writes=[("frontend.py", ALICE_FRONTEND_AFTER),
                                ("app.py", 'GREETING = "renamed"\n')], call_id=900)
    assert set(alice.active_paths) == {"frontend.py", "app.py"}, alice.active_paths
    checkout_before = _checkout_state(w.clone_a)
    out = w.tmp / "candidates"
    out.mkdir()

    base = {"runId": alice.run_id, "storePath": str(w.store_a_path),
            "hubPath": str(w.hub), "paths": ["frontend.py"]}
    method = "collab.delivery.preview"
    expected = "delivery_invalid"
    params = dict(base)

    if case == "paths_not_a_list":
        params["paths"] = "frontend.py"
    elif case == "unexpected_key":
        params["verify"] = False
        params["store"] = "x"
    elif case == "verify_string_before_any_exec":
        method = "collab.delivery.candidate"
        params = {"runId": alice.run_id, "storePath": str(w.store_a_path),
                  "hubPath": str(w.hub), "proposalIds": ["p"], "verify": "1",
                  "outputPath": str(out / "never")}
    elif case == "duplicate_proposal_ids":
        method = "collab.delivery.candidate"
        params = {"runId": alice.run_id, "storePath": str(w.store_a_path),
                  "hubPath": str(w.hub), "proposalIds": ["p", "p"],
                  "outputPath": str(out / "dupes")}
    elif case == "store_inside_checkout":
        nested = w.clone_a / "nested-store.git"
        GitStore.create_bare(nested, what="store")
        expected = "delivery_unavailable"
        params = dict(base, storePath=str(nested))
        # the test itself placed this directory inside the checkout
        checkout_before = _checkout_state(w.clone_a)
    elif case == "network_hub_url":
        expected = "delivery_unavailable"
        params = dict(base, hubPath="https://example.invalid/hub.git")
    elif case == "wrong_realm_hub":
        other_hub = GitStore.create_bare(w.tmp / "other-hub.git", what="hub")
        other_store_path = GitStore.create_bare(w.tmp / "other-store.git", what="store")
        other_store = GitStore(store=other_store_path, remote=str(other_hub))
        other_store.init_session(build_initial_state(
            session_id="other-session", target_version="other", base_commit=w.head))
        expected = "delivery_stale"
        params = dict(base, storePath=str(other_store_path), hubPath=str(other_hub))
    elif case == "missing_store_path":
        expected = "delivery_invalid"
        params = {k: v for k, v in base.items() if k != "storePath"}
    else:  # out_of_scope_without_consent
        ticket = _preview_dto(_rpc(w, "collab.delivery.preview",
                                   dict(base, paths=["app.py"]), call_id=920))
        assert ticket["outOfScopePaths"] == ["app.py"], ticket
        method = "collab.delivery.publish"
        params = {"runId": alice.run_id, "previewId": ticket["previewId"]}

    reply = _rpc(w, method, params, call_id=930)
    assert not reply["ok"], (case, reply)
    assert reply["error"]["code"] == expected, (case, reply)
    assert set(reply["error"]) == {"code", "message"}, reply
    _no_secret(reply)

    # Fixed messages only: no path, artifact, or git vocabulary leaks out.
    message = reply["error"]["message"]
    for leak in (str(w.tmp), str(w.hub), str(w.store_a_path), "Traceback", "conflict",
                 str(w.clone_a), "app.py", "frontend.py"):
        assert leak not in message, (case, leak)

    assert _checkout_state(w.clone_a) == checkout_before
    assert not (out / "never").exists() and not (out / "dupes").exists()
    assert sorted(p.name for p in out.iterdir()) == []

    if case == "out_of_scope_without_consent":
        consented = _publish_dto(_rpc(w, "collab.delivery.publish", dict(
            params, allowOutOfScope=True), call_id=931))
        assert consented["outOfScopeAuthorized"] is True
        assert consented["outOfScopePaths"] == ["app.py"]
        assert consented["paths"] == ["app.py"]
        # The unselected owned file was never serialized.
        artifact = read_proposal(w.store_a, consented["proposalId"])
        assert [f.path for f in artifact.files] == ["app.py"], artifact.files
        assert ALICE_WIP_LINE not in artifact.files[0].after_bytes.decode("utf-8")
    _no_secret(w.events)
