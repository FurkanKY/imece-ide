"""Acceptance for the memory-carried collaboration binding.

Two halves of ONE claim: a worker safe point hands the already ACCEPTED
``SharedSnapshot`` to the capture/candidate layer in memory, so a real
``GitWorktreeWorkspace`` (whose synthetic snapshot commit is NOT the session
base) can capture and assemble without ever reading -- or writing -- a
``.imece/shared-context.json`` artifact, and the host records a receipt only
after a REAL safe-point acknowledgement.

Real on both sides: temporary git source checkouts, a real ``GitWorktreeWorkspace``
over the real frontend clone, real Git-backed stores/hubs and a real loopback
HTTP server with the real ``LoopbackSnapshotClient``/``RevisionConsumer``. Only
the model provider is scripted and two per-test seams inject failures a live
server cannot produce (a failing checkpoint persist and a failing session ctor).

Proven here:

* capture from a worktree whose HEAD is a synthetic commit (BASE is only an
  ancestor) succeeds with a typed binding, with NO artifact anywhere, and leaves
  the source text, WIP, HEAD, index, refs and config byte-identical;
* a stale worktree artifact is IGNORED (the typed binding wins) and never
  rewritten; the same artifact without a typed binding is still refused;
* the recorded provenance is the context the safe point accepted, never the old
  approval revision;
* forged whole-state bindings (same integrity checksum, wrong owner/goal/scopes/
  target version), raw dicts and filesystem paths are refused, and revision, CAS,
  context and task drift always fail with NO proposal ref on the hub;
* candidate assembly from two proposals with a typed binding materializes a NEW
  directory outside the checkout, runs NO verification, and still gates conflicts
  and the exact-bool ``verify`` flag;
* ``accepted_binding`` is None after preview/approval/activation/prepare, exists
  after a real ack (including the no-pending baseline case), tracks the LATEST
  acknowledged binding, survives deactivate/close, stays read-only, and is absent
  after a failed session ctor or a cancelled prepare/ack.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_runtime.cancellation import CancellationToken, OperationCancelledError  # noqa: E402
from collab_runtime.candidates import CandidateConflictError, assemble_candidate  # noqa: E402
from collab_runtime.context import SharedSnapshot, parse_snapshot_bytes  # noqa: E402
from collab_runtime.consumer import RevisionConsumer  # noqa: E402
from collab_runtime.coordinator import Coordinator  # noqa: E402
from collab_runtime.errors import StaleRevisionError, ValidationError  # noqa: E402
from collab_runtime.host import CollaborationHost  # noqa: E402
from collab_runtime.models import (  # noqa: E402
    SessionState, Task, build_context, build_initial_state, build_task, canonical_json_bytes,
)
from collab_runtime.proposals import capture_proposal, list_proposals  # noqa: E402
from collab_runtime.safe_point import SafePointError  # noqa: E402
from collab_runtime.store import GitStore  # noqa: E402
import executor_runtime.native_worker as native_worker  # noqa: E402
from executor_runtime.errors import ExecutorAdapterInputError  # noqa: E402
from executor_runtime.native_worker import NativeWorkerAttemptAdapter  # noqa: E402
from fix_runtime.models import InitialWorkerRenderContext, InitialWorkerRequest  # noqa: E402
from workspace.worktree import GitWorktreeWorkspace  # noqa: E402
from test_collab_candidates import (  # noqa: E402
    _FE_EDITED, _be_edit, _fe_edit, _head as _candidate_head,
)
from test_collab_candidates import _world as _candidate_world  # noqa: E402
from test_collab_candidates import _capture_publish  # noqa: E402
from test_collab_host_lifecycle import _initial_request  # noqa: E402
from test_collab_proposals import _git, _write  # noqa: E402
from test_collab_proposals import _mutate_frontend as _dirty_frontend  # noqa: E402
from test_collab_proposals import _world as _source_world  # noqa: E402
from test_collab_safe_point import _wait_for  # noqa: E402
from test_collab_transport import ALICE, SESSION_ID, servers  # noqa: E402,F811
from test_pipeline_integration import (  # noqa: E402
    ScriptedBackend, _completed_turn, repo_workspace, setup_runtime,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadi")

FRONT_TASK = "t-fe"
UI_TASK = "t-ui"
BACK_TASK = "t-be"
MEMBER = "alice"
PROPOSAL_REF = "refs/heads/imece-proposals/"


# --------------------------------------------------------------- helpers ----


def _typed_binding(store: GitStore, task_id: str) -> SharedSnapshot:
    """The memory-carried binding the safe point accepted: a typed
    ``SharedSnapshot`` over the LIVE published hub state for one task."""
    revision, state = store.fetch_state()
    return SharedSnapshot(
        revision=revision, context_hash=state.context_hash, state=state, task_id=task_id,
    )


def _forged_binding(store: GitStore, task_id: str, *, target_version=None, **task_overrides):
    """A hand-built envelope whose integrity checksum MATCHES the live context
    but whose whole state is not the published one."""
    revision, state = store.fetch_state()
    live = state.tasks[task_id]
    fields = {"owner": live.owner, "goal": live.goal, "scopes": live.scopes}
    fields.update(task_overrides)
    forged_task = Task(
        live.id, fields["owner"], fields["goal"], tuple(fields["scopes"]),
        live.status, live.context_revision,
    )
    forged_state = SessionState(
        state.session_id, target_version or state.target_version, state.base_commit,
        state.context, {forged_task.id: forged_task},
    )
    return SharedSnapshot(
        revision=revision, context_hash=state.context_hash, state=forged_state, task_id=task_id,
    )


def _source_state(root: Path) -> dict:
    """Everything a read-only capture must not touch: working-tree text, the
    WIP/index view, HEAD, every ref and the local git config."""
    tracked = [rel for rel in _git(["ls-files"], root).split("\n") if rel]
    return {
        "status": _git(["status", "--porcelain"], root),
        "head": _git(["rev-parse", "HEAD"], root).strip(),
        "refs": _git(["for-each-ref", "--format=%(refname) %(objectname)"], root),
        "config": _git(["config", "--local", "--list"], root),
        "files": {
            rel: hashlib.sha256((root / rel).read_bytes()).hexdigest()
            for rel in tracked if (root / rel).is_file()
        },
    }


def _artifact(root: Path) -> Path:
    return root / ".imece" / "shared-context.json"


def _write_artifact(root: Path, snapshot: SharedSnapshot) -> bytes:
    raw = canonical_json_bytes(snapshot.to_dict())
    parse_snapshot_bytes(raw)  # sanity: a VALID envelope, merely a stale one
    _artifact(root).parent.mkdir(parents=True, exist_ok=True)
    _artifact(root).write_bytes(raw)
    return raw


def _proposal_ref(hub: Path, proposal_id: str) -> str | None:
    out = _git(["ls-remote", str(hub), PROPOSAL_REF + proposal_id], hub).strip()
    return out.split("\t", 1)[0] if out else None


@contextmanager
def _owned_worktree(source: Path, tmp_path: Path, run_id: str):
    """A REAL shadow worktree over the user's checkout; always disposed."""
    workspace = GitWorktreeWorkspace.create(
        source_root=source, run_id=run_id, base_dir=tmp_path / "worktrees",
    )
    try:
        yield workspace
    finally:
        workspace.dispose()


# ------------------------------------------- 1. worktree + memory binding ----


def test_worktree_capture_uses_the_typed_binding_with_a_synthetic_head_and_no_artifact(tmp_path):
    w = _source_world(tmp_path)
    _dirty_frontend(w)
    _artifact(w.frontend).unlink()
    binding = _typed_binding(w.store_a, FRONT_TASK)
    source_before = _source_state(w.frontend)

    with _owned_worktree(w.frontend, tmp_path, "run-wtb-capture") as workspace:
        head, _state = w.store_a.fetch_state()
        # The worktree HEAD is a synthetic snapshot commit: neither the session
        # base nor the source head, but BASE is a real ancestor of it.
        worktree_head = _git(["rev-parse", "HEAD"], workspace.root).strip()
        assert worktree_head == workspace.snapshot.snapshot_commit
        assert worktree_head not in (w.base, workspace.snapshot.source_head)
        assert w.base not in _git(["rev-parse", "HEAD"], w.frontend).strip()
        assert not _artifact(workspace.root).exists()

        _write(workspace.root, "lib.py", "L from the worktree\n")
        assert _source_state(w.frontend) == source_before  # worktree creation is invisible to the user repo

        proposal = capture_proposal(
            w.store_a, workspace.root, "wtb-capture", FRONT_TASK, ["lib.py"], head,
            binding=binding,
        )
        assert proposal.context_revision == binding.revision
        assert proposal.context_hash == binding.context_hash
        assert [entry.path for entry in proposal.files] == ["lib.py"]
        assert proposal.out_of_scope_paths == ()

        # The agent's edit lives only in the worktree; the checkout is untouched.
        assert (workspace.root / "lib.py").read_text(encoding="utf-8") == "L from the worktree\n"
        assert _source_state(w.frontend) == source_before


def test_a_stale_worktree_artifact_is_ignored_and_never_rewritten(tmp_path):
    w = _source_world(tmp_path)
    _dirty_frontend(w)
    _artifact(w.frontend).unlink()
    binding = _typed_binding(w.store_a, FRONT_TASK)

    with _owned_worktree(w.frontend, tmp_path, "run-wtb-stale") as workspace:
        _write(workspace.root, "lib.py", "L from the agent\n")
        # A VALID envelope pinned to an older published revision: it parses and
        # is an ancestor, but it is not the state that revision published.
        stale = SharedSnapshot(
            revision=w.rev1, context_hash=binding.context_hash,
            state=binding.state, task_id=FRONT_TASK,
        )
        raw = _write_artifact(workspace.root, stale)
        head, _state = w.store_a.fetch_state()

        proposal = capture_proposal(
            w.store_a, workspace.root, "wtb-stale", FRONT_TASK, ["lib.py"], head,
            binding=binding,
        )
        assert proposal.context_revision == binding.revision != w.rev1
        assert _artifact(workspace.root).read_bytes() == raw  # never overwritten
        # The same artifact on its own is still refused: memory won, not luck.
        with pytest.raises(ValidationError):
            capture_proposal(w.store_a, workspace.root, "wtb-stale-none", FRONT_TASK, ["lib.py"], head)


def test_the_owner_published_context_is_recorded_from_the_accepted_binding_not_the_old_approval(tmp_path):
    w = _source_world(tmp_path)
    _dirty_frontend(w)
    _artifact(w.frontend).unlink()
    accepted = _typed_binding(w.store_a, FRONT_TASK)

    # The owner publishes NEW authoritative context data on the server; the safe
    # point later accepts a typed snapshot of it.
    published = w.store_a.update_context(
        build_context(goal="the new owner goal", decisions=["d2"], interfaces={"I": "y"}),
        expected_revision=accepted.revision,
    )
    fresh = _typed_binding(w.store_a, FRONT_TASK)
    assert fresh.revision == published and fresh.context_hash != accepted.context_hash

    with _owned_worktree(w.frontend, tmp_path, "run-wtb-context") as workspace:
        # The approval-time artifact for the OLD revision is still sitting here.
        raw = _write_artifact(workspace.root, accepted)
        _write(workspace.root, "lib.py", "L after the context update\n")

        proposal = capture_proposal(
            w.store_a, workspace.root, "wtb-context", FRONT_TASK, ["lib.py"], published,
            binding=fresh,
        )
        assert proposal.context_revision == published != accepted.revision
        assert proposal.context_hash == fresh.context_hash
        assert _artifact(workspace.root).read_bytes() == raw
        # The superseded binding is refused as stale context, never silently used.
        with pytest.raises(StaleRevisionError):
            capture_proposal(
                w.store_a, workspace.root, "wtb-old", FRONT_TASK, ["lib.py"], published,
                binding=accepted,
            )


def test_capture_without_a_typed_binding_still_refuses_an_absent_artifact(tmp_path):
    w = _source_world(tmp_path)
    _dirty_frontend(w)
    _artifact(w.frontend).unlink()
    head, _state = w.store_a.fetch_state()

    with _owned_worktree(w.frontend, tmp_path, "run-wtb-none") as workspace:
        _write(workspace.root, "lib.py", "L unbound\n")
        for root in (workspace.root, w.frontend):
            with pytest.raises(ValidationError):
                capture_proposal(w.store_a, root, "wtb-none", FRONT_TASK, ["lib.py"], head)
        # A refusal creates nothing: no artifact, no namespace.
        assert not (workspace.root / ".imece").exists()


# --------------------------------------------- 2. forged/drifted bindings ----


@pytest.mark.parametrize("variant", ["owner", "goal", "scopes", "target_version"])
def test_forged_whole_state_bindings_are_refused_despite_a_matching_context_hash(tmp_path, variant):
    w = _source_world(tmp_path)
    _dirty_frontend(w)
    _artifact(w.frontend).unlink()
    forged_tasks = {
        "owner": {"owner": "mallory"},
        "goal": {"goal": "someone else's goal"},
        "scopes": {"scopes": ("app.py",)},
        "target_version": {},
    }[variant]
    forged = _forged_binding(
        w.store_a, FRONT_TASK,
        target_version="hijacked target version" if variant == "target_version" else None,
        **forged_tasks,
    )
    head, _state = w.store_a.fetch_state()
    assert forged.context_hash == _typed_binding(w.store_a, FRONT_TASK).context_hash

    with _owned_worktree(w.frontend, tmp_path, f"run-wtb-forged-{variant}") as workspace:
        _write(workspace.root, "lib.py", "L forged\n")
        with pytest.raises(ValidationError):
            capture_proposal(
                w.store_a, workspace.root, "wtb-forged", FRONT_TASK, ["lib.py"], head,
                binding=forged,
            )
        # Only typed SharedSnapshots are accepted; dicts/paths/None/garbage are
        # refused before any hub work happens.
        for bad in (forged.to_dict(), str(_artifact(workspace.root)), None, 42):
            with pytest.raises(ValidationError):
                capture_proposal(
                    w.store_a, workspace.root, "wtb-typed", FRONT_TASK, ["lib.py"], head,
                    binding=bad,
                )
        assert _proposal_ref(w.hub, "wtb-forged") is None
        assert _proposal_ref(w.hub, "wtb-typed") is None
        assert list_proposals(w.store_a) == []


def test_revision_cas_context_and_task_drift_always_fail_and_publish_nothing(tmp_path):
    w = _source_world(tmp_path)
    _dirty_frontend(w)
    _artifact(w.frontend).unlink()
    stale_revision, stale_state = w.store_a.fetch_state()
    stale = SharedSnapshot(stale_revision, stale_state.context_hash, stale_state, FRONT_TASK)
    # A binding pinned to a revision that published a DIFFERENT whole state is
    # never accepted, even though it is a real ancestor of the live head.
    too_old = SharedSnapshot(w.rev1, stale_state.context_hash, stale_state, FRONT_TASK)

    # A revision that exists in ANOTHER session's history is foreign here.
    foreign_hub = GitStore.create_bare(tmp_path / "foreign-hub.git", what="hub")
    foreign_store = GitStore(
        store=GitStore.create_bare(tmp_path / "foreign-store.git"), remote=str(foreign_hub))
    foreign_revision = foreign_store.init_session(build_initial_state(
        session_id="foreign-1", target_version="foreign", base_commit=w.base))
    foreign = SharedSnapshot(foreign_revision, stale_state.context_hash, stale_state, FRONT_TASK)

    # A task-only move keeps the binding usable, so drift cases need a fresh one.
    moved = w.store_b.upsert_task(build_task(
        task_id="t-extra", owner="bob", goal="extra work", scopes=["api.py"],
        status="queued", context_revision=stale_revision), expected_revision=stale_revision)

    with _owned_worktree(w.frontend, tmp_path, "run-wtb-drift") as workspace:
        _write(workspace.root, "lib.py", "L drift\n")

        def refused(proposal_id, expected, binding, error):
            with pytest.raises(error):
                capture_proposal(
                    w.store_a, workspace.root, proposal_id, FRONT_TASK, ["lib.py"],
                    expected, binding=binding,
                )
            assert _proposal_ref(w.hub, proposal_id) is None

        refused("wtb-cas", stale_revision, stale, StaleRevisionError)   # stale CAS token
        refused("wtb-revision", moved, too_old, ValidationError)       # not the published state
        refused("wtb-foreign", moved, foreign, ValidationError)        # not this session's history
        assert list_proposals(w.store_a) == []

        # Shared-context drift: the accepted binding is one publication behind.
        refreshed = w.store_a.update_context(
            build_context(goal="moved on", decisions=[], interfaces={}),
            expected_revision=moved)
        refused("wtb-context", refreshed, stale, StaleRevisionError)

        # Task drift: owner/goal/scopes changed after the binding was accepted.
        superseded = _typed_binding(w.store_a, FRONT_TASK)
        head = w.store_a.upsert_task(build_task(
            task_id=FRONT_TASK, owner=MEMBER, goal="a rewritten goal",
            scopes=["app.py", "lib.py"], status="running", context_revision=superseded.revision),
            expected_revision=superseded.revision)
        refused("wtb-task", head, superseded, StaleRevisionError)
        assert _proposal_ref(w.hub, "wtb-task") is None
        assert list_proposals(w.store_a) == []


# --------------------------------------------------- 3. candidate assembly ----


def test_candidate_assembles_two_proposals_with_a_typed_binding_and_runs_no_verification(tmp_path):
    w = _candidate_world(tmp_path)
    _fe_edit(w)
    _be_edit(w)
    _capture_publish(w.store_a, w.frontend, "wtb-fe", UI_TASK, ["ui.py"])
    _capture_publish(w.store_b, w.backend, "wtb-be", BACK_TASK, ["key.py"])
    # The memory route must not need any artifact in the source checkout.
    _artifact(w.frontend).unlink()
    _artifact(w.backend).unlink()
    binding = _typed_binding(w.store_a, UI_TASK)
    out_root = tmp_path / "candidates"
    out_root.mkdir()

    receipt = assemble_candidate(
        w.store_a, w.frontend, ["wtb-fe", "wtb-be"], out_root / "typed-binding",
        expected_revision=_candidate_head(w.store_a), verify=False, binding=binding,
    )
    assert receipt["binding_revision"] == binding.revision
    assert receipt["context_hash"] == binding.context_hash
    assert receipt["proposal_ids"] == ["wtb-be", "wtb-fe"]
    assert receipt["conflicts"] == []
    assert receipt["verification"] == {
        "status": "not_run", "plan_id": None, "checks": [], "changed_content": False}
    candidate = out_root / "typed-binding"
    assert candidate.is_dir() and str(candidate).startswith(str(out_root))
    assert (candidate / "ui.py").read_text(encoding="utf-8") == _FE_EDITED
    assert (candidate / "key.py").read_text(encoding="utf-8") == 'KEY = "new-key"\n'
    # The user's checkout keeps its dirty state; nothing was materialized in it.
    assert (w.frontend / "ui.py").read_text(encoding="utf-8") == _FE_EDITED
    assert not _artifact(w.frontend).exists()


def test_conflicts_and_the_verify_flag_still_gate_a_typed_binding(tmp_path):
    w = _candidate_world(tmp_path)
    _fe_edit(w)
    _write(w.backend, "ui.py", 'from key import KEY\nLABEL = "backend:" + KEY\n\nFOOTER = "b"\n')
    _capture_publish(w.store_a, w.frontend, "wtb-fe", UI_TASK, ["ui.py"])
    _capture_publish(w.store_b, w.backend, "wtb-be", BACK_TASK, ["key.py", "ui.py"])
    binding = _typed_binding(w.store_a, UI_TASK)
    head = _candidate_head(w.store_a)
    out_root = tmp_path / "candidates"
    out_root.mkdir()

    with pytest.raises(CandidateConflictError) as conflict:
        assemble_candidate(
            w.store_a, w.frontend, ["wtb-fe", "wtb-be"], out_root / "conflicting",
            expected_revision=head, binding=binding,
        )
    assert conflict.value.conflict_paths == ["ui.py"]
    assert not (out_root / "conflicting").exists()

    # A typed binding never authorizes execution: verify must be exactly a bool.
    for flag in ("true", 1, None):
        with pytest.raises(ValidationError):
            assemble_candidate(
                w.store_a, w.frontend, ["wtb-fe"], out_root / f"verify-{flag!r}",
                expected_revision=head, verify=flag, binding=binding,
            )
        assert not (out_root / f"verify-{flag!r}").exists()


# ------------------------------------------------ 4. host accepted receipt ----

HOST_TASK = "wt-binding-task"
TASK_TEXT = "Fix the bug in a.txt"


@pytest.fixture
def host_env(tmp_path, repo_workspace, servers):
    """A live loopback session over a real Git store, pinned at the real
    worktree head, with a real private cursor namespace."""
    source = Path(repo_workspace.snapshot.source_root)
    head = repo_workspace.snapshot.source_head
    hub = GitStore.create_bare(tmp_path / "binding-hub.git", what="hub")
    store = GitStore(store=GitStore.create_bare(tmp_path / "binding-store.git"), remote=str(hub))
    revision = store.init_session(build_initial_state(
        session_id=SESSION_ID, target_version="worktree binding", base_commit=head))
    revision = store.upsert_task(build_task(
        task_id=HOST_TASK, owner=MEMBER, goal=TASK_TEXT, scopes=["a.txt"], status="running",
        context_revision=revision), expected_revision=revision)
    coordinator = Coordinator(store, session_id=SESSION_ID, owner_id=MEMBER,
                              member_credentials={MEMBER: ALICE})
    listener = servers(coordinator).start()
    trust = tmp_path / "binding-private"
    trust.mkdir(mode=0o700)
    state = SimpleNamespace(
        tmp=tmp_path, store=store, coordinator=coordinator, workspace=repo_workspace,
        source=source, head=head, credential=ALICE, base_url=listener.base_url,
        cursor_root=trust / "collab-cursors", baseline=revision, sessions=[],
    )
    try:
        yield state
    finally:
        for session in reversed(state.sessions):
            try:
                session.close()
            except Exception:  # noqa: BLE001 - teardown must not mask a failure
                pass


def _approved(env, *, run_id="run-1"):
    """Real preview -> approve -> bind_run over the actual root and source head."""
    host = CollaborationHost(env.cursor_root)
    preview = host.preview(env.source, env.base_url, env.credential, MEMBER, HOST_TASK)
    approval = host.approve(preview["previewId"], env.source)
    session = host.bind_run(approval["approvalHandle"], env.source, run_id)
    env.sessions.append(session)
    return host, preview, session


def _consumed(env) -> str:
    files = sorted(env.cursor_root.glob("*.json"))
    assert len(files) == 1, f"expected exactly one private cursor file, found {files}"
    return json.loads(files[0].read_text(encoding="utf-8"))["consumed_revision"]


def _activate(env, session):
    session.activate(env.workspace)
    assert _wait_for(lambda: session.status()["state"] == "streaming"), session.status()


def _publish_context(env, goal: str) -> str:
    """Owner-side context publication over the REAL coordinator."""
    revision, _state = env.store.fetch_state()
    return env.coordinator.update_context(
        ALICE, build_context(goal=goal, decisions=[], interfaces={}), expected_revision=revision)


def test_only_a_real_acknowledged_prepare_produces_a_receipt_at_the_baseline(host_env):
    env = host_env
    host, preview, session = _approved(env)
    assert session.accepted_binding is None  # not from the approval
    _activate(env, session)
    assert session.accepted_binding is None  # not from activation/streaming

    prepared = session.prepare(_initial_request(), env.workspace)
    assert isinstance(prepared.binding, SharedSnapshot)
    assert prepared.binding.revision == preview["revision"] == env.baseline
    assert prepared.binding.task_id == HOST_TASK
    assert session.accepted_binding is None  # not from prepare alone

    prepared.acknowledge()
    accepted = session.accepted_binding
    assert accepted is not None and accepted.revision == env.baseline
    assert accepted.state.tasks[HOST_TASK].owner == MEMBER
    # The getter hands out a fresh, read-only clone of the accepted state.
    assert session.accepted_binding is not accepted
    assert isinstance(accepted.state.tasks, MappingProxyType)
    with pytest.raises(TypeError):
        accepted.state.tasks[HOST_TASK] = accepted.state.tasks[HOST_TASK]

    session.deactivate()
    assert session.accepted_binding.revision == env.baseline
    session.close()
    assert session.accepted_binding.revision == env.baseline
    # The receipt never materialized an artifact anywhere.
    assert not (env.source / ".imece").exists()
    assert not (env.workspace.root / ".imece").exists()


def test_the_receipt_is_the_latest_acknowledged_binding_not_the_approved_one(host_env, monkeypatch):
    env = host_env
    host, preview, session = _approved(env)
    _activate(env, session)

    first = session.prepare(_initial_request(), env.workspace)
    first.acknowledge()
    assert session.accepted_binding.revision == preview["revision"]
    assert _consumed(env) == preview["revision"]

    # Two NEW context updates land in the inbox before the next attempt.
    one = _publish_context(env, "goal one")
    two = _publish_context(env, "goal two")
    assert _wait_for(lambda: session.status()["pendingCount"] == 2), session.status()

    prepared = session.prepare(_initial_request(), env.workspace)
    assert prepared.binding.revision == two != one
    assert session.accepted_binding.revision == preview["revision"]  # NOT the new one yet
    assert _consumed(env) == preview["revision"]

    def refuse_persist(self, revision):
        raise ValidationError("the revision checkpoint could not be persisted.")

    with monkeypatch.context() as patch:
        patch.setattr(RevisionConsumer, "_persist_checkpoint", refuse_persist)
        with pytest.raises(SafePointError):
            prepared.acknowledge()
    assert session.accepted_binding.revision == preview["revision"]
    assert _consumed(env) == preview["revision"]  # durable cursor untouched
    stuck = session.status()
    assert stuck["pendingCount"] == 2 and stuck["consumedRevision"] == preview["revision"]

    prepared.acknowledge()
    assert session.accepted_binding.revision == two
    assert _consumed(env) == two
    assert session.status()["pendingCount"] == 0

    session.deactivate()
    latest = session.accepted_binding
    assert latest.revision == two
    assert session._record.snapshot.revision == preview["revision"]  # approval-time snapshot
    with pytest.raises(TypeError):
        latest.state.tasks[HOST_TASK] = latest.state.tasks[HOST_TASK]


def test_a_failed_session_ctor_records_no_receipt(host_env, monkeypatch):
    env = host_env
    host, preview, session = _approved(env)
    _activate(env, session)
    _publish_context(env, "goal for a failing session")
    assert _wait_for(lambda: session.status()["pendingCount"] == 1), session.status()

    backend = ScriptedBackend([_completed_turn("Done.")])
    runs = env.tmp / "runs-ctor"
    runs.mkdir(exist_ok=True)
    runtime, run = setup_runtime(runs)
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend, safe_point=session.safe_point)

    def refuse_session(self, *args, **kwargs):
        raise RuntimeError("the model session could not be constructed")

    monkeypatch.setattr(native_worker.AgentSession, "__init__", refuse_session)
    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(env.workspace, _initial_request(), execution_id="exec-ctor-fail")

    assert session.accepted_binding is None
    assert _consumed(env) == preview["revision"]
    assert session.status()["pendingCount"] == 1
    assert backend.session.received_inputs == []  # the model was never called


def test_cancelled_prepare_and_acknowledgement_record_no_receipt(host_env):
    env = host_env
    host, preview, session = _approved(env)
    _activate(env, session)
    _publish_context(env, "goal for a cancelled attempt")
    assert _wait_for(lambda: session.status()["pendingCount"] == 1), session.status()

    cancelled = CancellationToken()
    cancelled.cancel()
    with pytest.raises(OperationCancelledError):
        session.prepare(_initial_request(), env.workspace, cancel_token=cancelled)
    assert session.accepted_binding is None
    assert _consumed(env) == preview["revision"]
    assert session.status()["pendingCount"] == 1

    prepared = session.prepare(_initial_request(), env.workspace)
    assert prepared.binding.revision != preview["revision"]
    assert session.accepted_binding is None

    late = CancellationToken()
    late.cancel()
    with pytest.raises(OperationCancelledError):
        prepared.acknowledge(cancel_token=late)
    assert session.accepted_binding is None
    assert _consumed(env) == preview["revision"]
    assert session.status()["pendingCount"] == 1
