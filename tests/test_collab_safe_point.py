"""Opt-in collaboration binding at the native Worker attempt boundary."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_runtime import ModelStopReason, ModelTurn, ModelUsage  # noqa: E402
from collab_runtime.context import ARTIFACT_RELPATH, SharedSnapshot, parse_snapshot_dict, render_snapshot_block  # noqa: E402
from collab_runtime.coordinator import Coordinator, Snapshot  # noqa: E402
from collab_runtime.consumer import RevisionConsumer  # noqa: E402
from collab_runtime.errors import ValidationError  # noqa: E402
from collab_runtime.models import (  # noqa: E402
    SessionState, SharedContext, Task, build_context, build_initial_state, build_task,
    canonical_json_bytes,
)
from collab_runtime.safe_point import NativeWorkerSafePoint, SafePointError  # noqa: E402
from collab_runtime.store import GitStore  # noqa: E402
from context_runtime import load_project_rules  # noqa: E402
from executor_runtime.errors import ExecutorAdapterInputError  # noqa: E402
from executor_runtime.native_worker import NativeWorkerAttemptAdapter  # noqa: E402
from fix_runtime.models import (  # noqa: E402
    FixTrigger, FixTriggerKind, FixWorkerRenderContext, FixWorkerRequest, InitialWorkerRequest,
)
from fix_runtime.prompt import render_fix_worker_input, render_initial_worker_input  # noqa: E402
from process_runtime.models import ProcessResult  # noqa: E402
from run_runtime import RunEventType, RunRuntime, RunStore  # noqa: E402
from verification_runtime.models import VerificationCheckResult, VerificationReport, VerificationStatus  # noqa: E402
from workspace.worktree import GitWorktreeWorkspace  # noqa: E402
from test_collab_transport import ALICE, SESSION_ID, hub, servers  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")


def _wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _worker_request() -> FixWorkerRequest:
    process = ProcessResult(
        argv=("true",), cwd=".", exit_code=1, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
        stdout_bytes=0, stderr_bytes=0,
    )
    result = VerificationCheckResult("check", "Check", VerificationStatus.FAIL, process)
    report = VerificationReport("verify-1", "plan-1", (result,), 1)
    trigger = FixTrigger(FixTriggerKind.VERIFICATION_FAIL, report)
    return FixWorkerRequest(
        "implement the approved task", trigger, 1,
        render_fix_worker_input(
            task="implement the approved task", plan="advisory plan", trigger=trigger,
            attempt_index=1, max_fix_attempts=2, pinned_paths=("src/approved.py",),
        ),
        plan="advisory plan",
        render_context=FixWorkerRenderContext(2, ("src/approved.py",)),
    )


def _bind_fix(snapshot: SharedSnapshot, request: FixWorkerRequest, workspace):
    rules = load_project_rules(workspace.root, shared_snapshot=snapshot)
    rendered = render_fix_worker_input(
        task=request.task, plan=request.plan, trigger=request.trigger,
        attempt_index=request.attempt_index, max_fix_attempts=2,
        rules=rules, pinned_paths=("src/approved.py",),
    )
    return replace(request, rendered_input=rendered)


def _new_workspace(tmp_path):
    source = tmp_path / "worker-source"
    source.mkdir()
    for args in (
        ("init", "-q"), ("config", "user.name", "SafePoint Test"),
        ("config", "user.email", "safe-point@example.invalid"),
    ):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    (source / "approved.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=source, check=True, capture_output=True)
    return GitWorktreeWorkspace.create(
        source_root=source, run_id="safe-point-test", base_dir=tmp_path / "worktrees",
    )


def _runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="safe-point test")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


class _ChangingBackend:
    """Fake provider publishes while the first model attempt is in flight."""

    def __init__(self, consumer, publish_twice, baseline):
        self.consumer = consumer
        self.publish_twice = publish_twice
        self.baseline = baseline
        self.inputs = []
        self.calls = 0

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        backend = self

        class Session:
            def respond(self, input_items):
                text = input_items[0].text
                backend.inputs.append(text)
                if backend.calls == 0:
                    before = text
                    backend.publish_twice()
                    assert _wait_for(lambda: len(backend.consumer.peek()) == 2)
                    assert backend.consumer.status()["consumed_revision"] == backend.baseline
                    assert backend.inputs[0] == before
                backend.calls += 1
                return ModelTurn("done", (), ModelStopReason.COMPLETED, ModelUsage())

        return Session()


def _seed_session(hub, tmp_path, name, base_commit):
    """A private temp store + hub session pinned at the REAL worktree head.

    The shared `wired` fixture is seeded from a synthetic 'a'*40 that no
    worktree here can match, so this test builds its own session instead of
    mutating that fixture's identity or the source repository HEAD.
    """
    store = GitStore(
        store=GitStore.create_bare(tmp_path / name, what="store"), remote=str(hub),
    )
    revision = store.init_session(
        build_initial_state(
            session_id=SESSION_ID, target_version="v0.1 safe point", base_commit=base_commit,
        )
    )
    return store, revision


def test_live_consumer_buffers_inflight_revisions_then_next_attempt_binds_and_acks(
    tmp_path, hub, servers,
):
    workspace = _new_workspace(tmp_path)
    consumer = None
    try:
        store, revision = _seed_session(
            hub, tmp_path, "safe-point.store.git", workspace.snapshot.source_head,
        )
        revision = store.upsert_task(
            build_task(
                task_id="safe-task", owner="alice", goal="implement approved task",
                scopes=["src/approved.py"], status="queued", context_revision=revision,
            ),
            expected_revision=revision,
        )
        coordinator = Coordinator(
            store, session_id=SESSION_ID, owner_id="alice",
            member_credentials={"alice": ALICE},
        )
        initial = coordinator.snapshot(ALICE)
        task = initial.state.tasks["safe-task"]
        listener = servers(coordinator).start()
        checkpoint_dir = tmp_path / "cursor"
        checkpoint_dir.mkdir(mode=0o700)
        consumer = RevisionConsumer(
            listener.base_url, credential=ALICE, member_id="alice",
            checkpoint_path=checkpoint_dir / "cursor.json", initial_snapshot=initial,
        )
        consumer.start()
        assert _wait_for(lambda: consumer.status()["state"] == "streaming")
        backend = _ChangingBackend(consumer, lambda: None, initial.revision)

        def publish_twice():
            nonlocal revision
            revision = coordinator.update_context(
                ALICE, build_context(goal="middle context", decisions=[], interfaces={}),
                expected_revision=revision,
            )
            revision = coordinator.update_context(
                ALICE, build_context(goal="latest context", decisions=["current"], interfaces={}),
                expected_revision=revision,
            )

        backend.publish_twice = publish_twice
        safe_point = NativeWorkerSafePoint(
            consumer, lambda: coordinator.snapshot(ALICE), initial_snapshot=initial,
            member_id="alice", approved_task=task,
        )
        runtime, run = _runtime(tmp_path)
        adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend, safe_point=safe_point)

        adapter.run(workspace, _worker_request(), execution_id="exec_safe_1")
        first_input = backend.inputs[0]
        assert "shared-context" in first_input or "SHARED COLLABORATION SNAPSHOT" in first_input
        assert consumer.status()["consumed_revision"] == initial.revision
        assert len(consumer.peek()) == 2

        adapter.run(workspace, _worker_request(), execution_id="exec_safe_2")
        second_input = backend.inputs[1]
        assert second_input.count("SHARED COLLABORATION SNAPSHOT") == 1
        assert "latest context" in second_input
        assert "middle context" not in second_input
        assert "- src/approved.py" in second_input
        assert consumer.status()["consumed_revision"] == revision
        assert consumer.peek() == ()
    finally:
        if consumer is not None:
            consumer.close()
        workspace.dispose()


def test_shared_snapshot_override_replaces_stale_artifact_and_keeps_file_rules(tmp_path):
    """A real, VALID but older on-disk artifact is replaced wholesale by the
    safe point's authoritative snapshot; repository rule files are unaffected."""
    root = tmp_path / "rules"
    root.mkdir()
    (root / "AGENTS.md").write_text("Keep the public API stable.\n", encoding="utf-8")
    initial, task = _snapshots()
    stale_state = replace(
        initial.state,
        context=build_context(goal="stale on-disk context", decisions=[], interfaces={}),
    )
    stale = _shared(Snapshot(initial.revision, stale_state), task.id)
    (root / ARTIFACT_RELPATH).parent.mkdir()
    (root / ARTIFACT_RELPATH).write_bytes(canonical_json_bytes(stale.to_dict()))

    fresh_state = replace(
        initial.state,
        context=build_context(goal="authoritative context", decisions=[], interfaces={}),
    )
    fresh = _shared(Snapshot(initial.revision, fresh_state), task.id)

    assert "stale on-disk context" in load_project_rules(root).text
    overridden = load_project_rules(root, shared_snapshot=fresh)
    assert "authoritative context" in overridden.text
    assert "stale on-disk context" not in overridden.text
    assert "Keep the public API stable." in overridden.text
    assert overridden.sha256 != load_project_rules(root).sha256


class _MemoryConsumer:
    def __init__(self, snapshot, member="alice", state="streaming", consumed=None, pending=()):
        self.session_identity = (
            snapshot.state.session_id, snapshot.state.base_commit, snapshot.state.target_version,
        )
        self.member_id = member
        self.current_state = state
        self.consumed = consumed or snapshot.revision
        self.pending = tuple(pending)
        self.acks = []
        self.reset_calls = []

    def status(self):
        return {"state": self.current_state, "consumed_revision": self.consumed}

    def peek(self):
        return self.pending

    def acknowledge_at_safe_point(self, revision):
        self.acks.append(revision)

    def reset_at_safe_point(self, snapshot):
        self.reset_calls.append(snapshot)
        raise ValidationError("fixed reset failure")


def _snapshots(base: str = "b" * 40):
    """One unit session pinned at `base`, the worktree head the caller owns."""
    revision = "a" * 40
    task = Task("task", "alice", "goal", ("src",), "queued", revision)
    state = SessionState("session", "v1", base, SharedContext("shared", (), ()), {task.id: task})
    return Snapshot(revision, state), task


def _unit_workspace(base_commit: str, root):
    """Trusted stand-in for a worktree: the safe point reads only the source
    head it is bound to and the rules root its callback renders from."""
    return SimpleNamespace(snapshot=SimpleNamespace(source_head=base_commit), root=root)


def _shared(snapshot: Snapshot, task_id: str) -> SharedSnapshot:
    return parse_snapshot_dict({
        "schema": 1, "revision": snapshot.revision,
        "context_hash": snapshot.state.context.content_hash,
        "state": snapshot.state.to_dict(), "task_id": task_id,
    })


def test_identity_member_terminal_ack_and_reset_fail_closed_without_consuming(tmp_path):
    initial, task = _snapshots()
    bound = lambda snapshot, request, workspace: replace(
        request, rendered_input=request.rendered_input + "\n" + render_snapshot_block(snapshot)
    )
    for kind in ("member", "identity"):
        mismatch = _MemoryConsumer(initial, member="bob" if kind == "member" else "alice")
        if kind == "identity":
            mismatch.session_identity = ("other-session", initial.state.base_commit, "v1")
        with pytest.raises(SafePointError):
            NativeWorkerSafePoint(
                mismatch, lambda: initial, initial_snapshot=initial, member_id="alice",
                approved_task=task, bind_input=bound,
            )

    consumer = _MemoryConsumer(initial)
    helper = NativeWorkerSafePoint(
        consumer, lambda: initial, initial_snapshot=initial, member_id="alice",
        approved_task=task, bind_input=bound,
    )
    workspace = _unit_workspace(initial.state.base_commit, tmp_path)
    prepared = helper.prepare(_worker_request(), workspace)
    consumer.current_state = "protocol_error"
    with pytest.raises(SafePointError):
        prepared.acknowledge()
    assert consumer.acks == []
    assert consumer.consumed == initial.revision

    pending = (SimpleNamespace(revision="c" * 40),)
    consumer.pending = pending
    with pytest.raises(SafePointError):
        helper.accept_reset(initial, _worker_request(), workspace)
    assert len(consumer.reset_calls) == 1
    assert consumer.consumed == initial.revision
    assert consumer.pending == pending


@pytest.mark.parametrize("failure", ["callback", "stale", "oversized", "corrupt"])
def test_invalid_binding_callback_is_rejected_before_ack(tmp_path, failure):
    initial, task = _snapshots()
    event = SimpleNamespace(revision="c" * 40)
    latest = Snapshot(event.revision, initial.state)
    consumer = _MemoryConsumer(initial, consumed=initial.revision, pending=(event,))

    def callback(snapshot, request, workspace):
        if failure == "callback":
            raise RuntimeError("private callback detail")
        if failure == "stale":
            # The request already carries the OLD canonical snapshot: one header
            # (so the header rule holds) but not the authoritative block.
            return replace(
                request,
                rendered_input=request.rendered_input + "\n"
                + render_snapshot_block(_shared(initial, task.id)),
            )
        canonical = render_snapshot_block(snapshot)
        text = request.rendered_input + "\n" + canonical
        if failure == "corrupt":
            text = request.rendered_input + "\nSHARED COLLABORATION SNAPSHOT corrupt"
        result = replace(request, rendered_input=text)
        if failure == "oversized":
            object.__setattr__(result, "rendered_input", "x" * 50_000 + "\n" + canonical)
        return result

    helper = NativeWorkerSafePoint(
        consumer, lambda: latest, initial_snapshot=initial, member_id="alice",
        approved_task=task, bind_input=callback,
    )
    with pytest.raises(SafePointError, match="worker attempt was not started"):
        helper.prepare(_worker_request(), _unit_workspace(initial.state.base_commit, tmp_path))
    assert consumer.acks == []
    assert consumer.pending == (event,)


def test_initial_and_fix_canonical_renderers_preserve_preview_and_pinned_paths(tmp_path):
    initial, task = _snapshots()
    consumer = _MemoryConsumer(initial)
    helper = NativeWorkerSafePoint(
        consumer, lambda: initial, initial_snapshot=initial, member_id="alice",
        approved_task=task,
        bind_input=lambda snapshot, request, workspace: replace(
            request,
            rendered_input=(
                render_initial_worker_input(
                    task=request.task, plan=request.plan,
                    verification_plan=SimpleNamespace(checks=(SimpleNamespace(
                        name="Approved check", check_id="approved", request=SimpleNamespace(argv=("pytest", "-q")),
                    ),)),
                    rules=load_project_rules(tmp_path, shared_snapshot=snapshot),
                    pinned_paths=("src/approved.py",),
                ) if isinstance(request, InitialWorkerRequest) else render_fix_worker_input(
                    task=request.task, plan=request.plan, trigger=request.trigger,
                    attempt_index=request.attempt_index, max_fix_attempts=2,
                    rules=load_project_rules(tmp_path, shared_snapshot=snapshot),
                    pinned_paths=("src/approved.py",),
                )
            ),
        ),
    )
    initial_request = InitialWorkerRequest("goal", "old bounded input", "plan")
    prepared_initial = helper.prepare(initial_request, _unit_workspace(initial.state.base_commit, tmp_path))
    assert "HOW THIS WILL BE JUDGED" in prepared_initial.request.rendered_input
    assert "pytest -q" in prepared_initial.request.rendered_input
    assert "src/approved.py" in prepared_initial.request.rendered_input
    assert render_snapshot_block(_shared(initial, task.id)) in prepared_initial.request.rendered_input

    fix_request = _worker_request()
    prepared_fix = helper.prepare(fix_request, _unit_workspace(initial.state.base_commit, tmp_path))
    assert "FIX FEEDBACK" in prepared_fix.request.rendered_input
    assert "src/approved.py" in prepared_fix.request.rendered_input
    assert prepared_fix.request.trigger == fix_request.trigger


def test_authoritative_snapshot_ahead_of_captured_inbox_is_deferred(tmp_path):
    initial, task = _snapshots()
    event = SimpleNamespace(revision="c" * 40)
    ahead = Snapshot("d" * 40, initial.state)
    consumer = _MemoryConsumer(initial, pending=(event,))
    helper = NativeWorkerSafePoint(
        consumer, lambda: ahead, initial_snapshot=initial, member_id="alice",
        approved_task=task,
        bind_input=lambda snapshot, request, workspace: replace(
            request, rendered_input=request.rendered_input + "\n" + render_snapshot_block(snapshot)
        ),
    )
    with pytest.raises(SafePointError):
        helper.prepare(_worker_request(), _unit_workspace(initial.state.base_commit, tmp_path))
    assert consumer.acks == []
    assert consumer.pending == (event,)


@pytest.mark.parametrize("has_pending", [False, True])
@pytest.mark.parametrize("state", [
    "access_denied", "resnapshot_required", "protocol_error", "server_error", "closed", "stopped",
])
def test_terminal_state_during_binding_blocks_ack_even_without_pending(tmp_path, has_pending, state):
    initial, task = _snapshots()
    pending = (SimpleNamespace(revision="c" * 40),) if has_pending else ()
    current = Snapshot(pending[-1].revision, initial.state) if pending else initial
    consumer = _MemoryConsumer(initial, pending=pending)

    def bind(snapshot, request, workspace):
        consumer.current_state = state
        return replace(request, rendered_input=request.rendered_input + "\n" + render_snapshot_block(snapshot))

    helper = NativeWorkerSafePoint(
        consumer, lambda: current, initial_snapshot=initial, member_id="alice",
        approved_task=task, bind_input=bind,
    )
    prepared = helper.prepare(_worker_request(), _unit_workspace(initial.state.base_commit, tmp_path))
    with pytest.raises(SafePointError):
        prepared.acknowledge()
    assert consumer.consumed == initial.revision
    assert consumer.pending == pending
    assert consumer.acks == []


def test_prepared_ack_keeps_captured_tail_and_rejects_external_cursor_change(tmp_path):
    initial, task = _snapshots()
    tail, later = SimpleNamespace(revision="c" * 40), SimpleNamespace(revision="d" * 40)
    consumer = _MemoryConsumer(initial, pending=(tail,))

    def bind(snapshot, request, workspace):
        consumer.pending = (tail, later)
        return replace(request, rendered_input=request.rendered_input + "\n" + render_snapshot_block(snapshot))

    helper = NativeWorkerSafePoint(
        consumer, lambda: Snapshot(tail.revision, initial.state), initial_snapshot=initial,
        member_id="alice", approved_task=task, bind_input=bind,
    )
    prepared = helper.prepare(_worker_request(), _unit_workspace(initial.state.base_commit, tmp_path))
    prepared.acknowledge()
    assert consumer.acks == [tail.revision]  # Never acknowledge the post-capture arrival.
    consumer.consumed = later.revision
    with pytest.raises(SafePointError):
        prepared.acknowledge()
    assert consumer.acks == [tail.revision]


def test_session_construction_failure_happens_before_ack_or_backend_open(tmp_path, monkeypatch):
    # The workspace exists FIRST so the session is pinned at the real worktree
    # head; otherwise prepare() would reject on the base commit and the
    # constructor would never be reached.
    workspace = _new_workspace(tmp_path)
    try:
        initial, task = _snapshots(base=workspace.snapshot.source_head)
        event = SimpleNamespace(revision="c" * 40)
        latest = Snapshot(event.revision, initial.state)
        consumer = _MemoryConsumer(initial, pending=(event,))
        helper = NativeWorkerSafePoint(
            consumer, lambda: latest, initial_snapshot=initial, member_id="alice",
            approved_task=task,
            bind_input=lambda snapshot, request, workspace: replace(
                request, rendered_input=request.rendered_input + "\n" + render_snapshot_block(snapshot)
            ),
        )
        runtime, run = _runtime(tmp_path)

        class Backend:
            def open_session(self, **kwargs):
                pytest.fail("model backend must not open after failed AgentSession construction")

        def fail_construction(**kwargs):
            raise RuntimeError("private constructor detail")

        monkeypatch.setattr("executor_runtime.native_worker.AgentSession", fail_construction)
        adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, Backend(), safe_point=helper)
        with pytest.raises(ExecutorAdapterInputError, match="worker attempt was not started"):
            adapter.run(workspace, _worker_request(), execution_id="exec_ctor_failure")
        assert consumer.acks == []
        assert consumer.pending == (event,)
    finally:
        workspace.dispose()


def test_workspace_source_head_mismatch_blocks_binding_ack_model_and_reset(tmp_path):
    """A session bound to a different base than the worktree head is refused at
    both the normal and the explicit-recovery path, and the reset refusal leaves
    the durable cursor and the inbox exactly as they were."""
    workspace = _new_workspace(tmp_path)
    try:
        initial, task = _snapshots(base="f" * 40)
        assert initial.state.base_commit != workspace.snapshot.source_head
        event = SimpleNamespace(revision="c" * 40)
        latest = Snapshot(event.revision, initial.state)
        consumer = _MemoryConsumer(initial, pending=(event,))
        bound = []
        helper = NativeWorkerSafePoint(
            consumer, lambda: latest, initial_snapshot=initial, member_id="alice",
            approved_task=task,
            bind_input=lambda snapshot, request, ws: bound.append(snapshot) or replace(
                request, rendered_input=request.rendered_input + "\n" + render_snapshot_block(snapshot)
            ),
        )
        runtime, run = _runtime(tmp_path)

        class Backend:
            def open_session(self, **kwargs):
                pytest.fail("a mismatched base commit must not reach the model")

        adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, Backend(), safe_point=helper)
        with pytest.raises(ExecutorAdapterInputError, match="worker attempt was not started"):
            adapter.run(workspace, _worker_request(), execution_id="exec_base_mismatch")
        assert bound == []
        assert consumer.acks == []

        with pytest.raises(SafePointError):
            helper.accept_reset(latest, _worker_request(), workspace)
        assert consumer.reset_calls == []
        assert consumer.consumed == initial.revision
        assert consumer.pending == (event,)
    finally:
        workspace.dispose()
