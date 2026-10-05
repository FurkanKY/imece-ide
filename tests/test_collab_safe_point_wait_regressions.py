"""Regressions for the bounded, opt-in replay wait at the Worker safe point.

Unit cases drive a fake clock installed on the safe point's OWN module aliases
(``collab_runtime.safe_point.monotonic``/``sleep`` -- global ``time`` is never
touched) plus a memory inbox that is observed only through the public
one-host-writer surface (``status``/``peek``/``acknowledge_at_safe_point``). No
test here starts, resets or closes a consumer or edits a private consumer
field. The two integration cases use a real Git worktree and a real loopback
socket with a scripted fake model backend, so nothing in this module calls an
external service.

Every bound the contract claims is asserted here: strict 0..5 second
configuration, a 0.05 second polling ceiling, the ONE deadline that a lag and
its refetch share, cooperative cancellation (never wall-clock preemption), the
rechecks of consumer state and cursor before coverage and before the binder,
and the exact set of things the wait must never do (bind, acknowledge,
persist, reset, start, close or call a model).
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import engine_factory  # noqa: E402
from agent_runtime import ModelStopReason, ModelTurn, ModelUsage, UserInput  # noqa: E402
from agent_runtime.cancellation import CancellationToken, OperationCancelledError  # noqa: E402
from collab_runtime import safe_point as safe_point_module  # noqa: E402
from collab_runtime.context import render_snapshot_block  # noqa: E402
from collab_runtime.coordinator import Coordinator, Snapshot  # noqa: E402
from collab_runtime.models import Task, build_context  # noqa: E402
from collab_runtime.safe_point import (  # noqa: E402
    SAFE_POINT_ERROR, NativeWorkerSafePoint, SafePointError,
)
from executor_runtime.errors import (  # noqa: E402
    ExecutorAdapterCancelledError, ExecutorAdapterInputError,
)
from executor_runtime.native_worker import NativeWorkerAttemptAdapter  # noqa: E402
from pipeline_runtime.models import PipelineStatus  # noqa: E402
from pipeline_runtime.runner import PipelineRunner  # noqa: E402
from test_collab_factory import (  # noqa: E402  -- real Git + real loopback fixture
    PINNED_PATHS, PLAN_JSON, TASK_TEXT, collab_session,
)
from test_collab_safe_point import (  # noqa: E402
    _MemoryConsumer, _new_workspace, _runtime, _snapshots, _unit_workspace,
    _wait_for, _worker_request,
)
from test_collab_transport import ALICE, servers  # noqa: E402  (real listener factory)
from test_pipeline_integration import ScriptedBackend, _completed_turn, repo_workspace  # noqa: E402

# Every non-runnable consumer state the host may observe; none of them may be
# bound or acknowledged, with or without a pending inbox.
_TERMINAL_STATES = (
    "access_denied", "resnapshot_required", "protocol_error", "server_error",
    "closed", "stopped",
)
_RUNNABLE_STATES = {"connecting", "streaming", "retrying", "inbox_full"}


def _never(*_args, **_kwargs):  # pragma: no cover - only reached on a defect
    pytest.fail("the safe point must not reach this step")


class _Clock:
    """Deterministic ``monotonic``/``sleep`` pair for the safe-point aliases."""

    def __init__(self) -> None:
        self.start = 1000.0
        self.now = self.start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def install(self, monkeypatch) -> "_Clock":
        # raising=False keeps this test honest against either the pre- or the
        # post-correction module layout; the assertions below are the contract.
        monkeypatch.setattr(safe_point_module, "monotonic", self.monotonic, raising=False)
        monkeypatch.setattr(safe_point_module, "sleep", self.sleep, raising=False)
        return self

    def assert_polling_ceiling(self) -> None:
        assert self.sleeps, "the wait never polled at all"
        assert all(0.0 < seconds <= 0.05 for seconds in self.sleeps), self.sleeps


class _Inbox(_MemoryConsumer):
    """Memory inbox scripted through the public surface only.

    ``deliver`` maps a ``peek()`` index to the events visible at that poll,
    ``states``/``consumed`` map a ``status()`` index to an observed state or to
    a cursor another host writer moved, and ``on_peek`` runs caller code at a
    chosen poll (used to set a real ``threading.Event`` mid-wait).
    """

    def __init__(self, snapshot, *, deliver=None, states=None, consumed=None, on_peek=None, **kwargs):
        super().__init__(snapshot, **kwargs)
        self._deliver = dict(deliver or {})
        self._states = dict(states or {})
        self._consumed = dict(consumed or {})
        self._on_peek = dict(on_peek or {})
        self.status_calls = 0
        self.peek_calls = 0

    def status(self):
        self.status_calls += 1
        if self.status_calls in self._states:
            self.current_state = self._states[self.status_calls]
        if self.status_calls in self._consumed:
            self.consumed = self._consumed[self.status_calls]
        return super().status()

    def peek(self):
        self.peek_calls += 1
        if self.peek_calls in self._deliver:
            self.pending = tuple(self._deliver[self.peek_calls])
        if self.peek_calls in self._on_peek:
            self._on_peek[self.peek_calls]()
        return super().peek()

    # The wait owns no consumer lifecycle: these are spies, not mutators.
    def start(self):  # pragma: no cover
        pytest.fail("the safe point wait must never start a consumer")

    def close(self):  # pragma: no cover
        pytest.fail("the safe point wait must never close a consumer")


def _bind(snapshot, request, workspace):
    return replace(
        request,
        rendered_input=request.rendered_input + "\n" + render_snapshot_block(snapshot),
    )


def _point(initial, task, consumer, provider, *, timeout=0.0, bind=_bind):
    return NativeWorkerSafePoint(
        consumer, provider, initial_snapshot=initial, member_id="alice",
        approved_task=task, replay_wait_timeout=timeout, bind_input=bind,
    )


def _unit(initial, tmp_path):
    """The trusted stand-in worktree pinned at the unit session's base."""
    return _unit_workspace(initial.state.base_commit, tmp_path)


# --------------------------------------------------------------- configuration


@pytest.mark.parametrize("invalid", [
    10 ** 1000, float("nan"), float("inf"), float("-inf"), -0.001, 5.0001,
    True, "1", None,
])
def test_wait_settings_outside_the_strict_bound_are_plain_value_errors(invalid):
    """A hostile timeout is a ValueError -- never an OverflowError from a
    range comparison, and never a silently clamped wait."""
    initial, task = _snapshots()
    consumer = _MemoryConsumer(initial)
    with pytest.raises(ValueError, match="finite number from 0 to 5") as caught:
        _point(initial, task, consumer, lambda: initial, timeout=invalid)
    assert not isinstance(caught.value, OverflowError)
    assert consumer.acks == []


# ----------------------------------------------------------------- default mode


def test_default_zero_timeout_takes_the_exact_captured_tail_with_one_fetch(tmp_path, monkeypatch):
    clock = _Clock().install(monkeypatch)
    initial, task = _snapshots()
    tail = SimpleNamespace(revision="c" * 40)
    consumer = _Inbox(initial, pending=(tail,))
    calls = []

    def provider():
        calls.append(1)
        return Snapshot(tail.revision, initial.state)

    prepared = _point(initial, task, consumer, provider).prepare(
        _worker_request(), _unit(initial, tmp_path),
    )
    assert len(calls) == 1                 # exactly one authenticated fetch, no refetch
    assert clock.sleeps == []           # the default never waits
    assert f"revision={tail.revision}" in prepared.request.rendered_input
    prepared.acknowledge()
    assert consumer.acks == [tail.revision]


# ----------------------------------------------------------------- the lag wait


def test_an_unknown_head_waits_at_twenty_hertz_then_binds_the_delivered_head(tmp_path, monkeypatch):
    clock = _Clock().install(monkeypatch)
    initial, task = _snapshots()
    first, head = SimpleNamespace(revision="c" * 40), SimpleNamespace(revision="d" * 40)
    consumer = _Inbox(initial, deliver={4: (first, head)})
    calls = []

    def provider():
        calls.append(1)
        return Snapshot(head.revision, initial.state)

    prepared = _point(initial, task, consumer, provider, timeout=2.0).prepare(
        _worker_request(), _unit(initial, tmp_path),
    )
    assert len(calls) == 2                 # the initial fetch plus one refetch, at arrival only
    assert clock.sleeps == [0.05, 0.05]  # two bounded polls, not a busy 20 Hz loop
    clock.assert_polling_ceiling()
    assert clock.now - clock.start <= 2.0
    assert f"revision={head.revision}" in prepared.request.rendered_input
    assert f"revision={first.revision}" not in prepared.request.rendered_input
    prepared.acknowledge()
    # The whole covered prefix, including the head that was unknown at capture.
    assert consumer.acks == [head.revision]


def test_an_unreconciled_head_fails_on_the_same_deadline_with_one_fetch(tmp_path, monkeypatch):
    clock = _Clock().install(monkeypatch)
    initial, task = _snapshots()
    consumer = _Inbox(initial)          # the unknown head never reaches the inbox
    calls = []

    def provider():
        calls.append(1)
        return Snapshot("d" * 40, initial.state)

    with pytest.raises(SafePointError) as caught:
        _point(initial, task, consumer, provider, timeout=0.3, bind=_never).prepare(
            _worker_request(), _unit(initial, tmp_path),
        )
    assert str(caught.value) == SAFE_POINT_ERROR
    assert len(calls) == 1                 # idle waiting never refetches the snapshot
    clock.assert_polling_ceiling()
    assert sum(clock.sleeps) <= 0.3 + 0.05   # never past the ONE deadline
    assert consumer.acks == []
    assert consumer.consumed == initial.revision
    assert consumer.pending == ()


def test_each_new_head_shares_the_original_deadline(tmp_path, monkeypatch):
    clock = _Clock().install(monkeypatch)
    initial, task = _snapshots()
    first, second, third = (SimpleNamespace(revision=value * 40) for value in "cde")
    consumer = _Inbox(initial, deliver={2: (first,), 4: (first, second)})
    calls = []
    snapshots = iter((first, second, third))

    def provider():
        calls.append(clock.now)
        return Snapshot(next(snapshots).revision, initial.state)

    with pytest.raises(SafePointError):
        _point(initial, task, consumer, provider, timeout=0.12, bind=_never).prepare(
            _worker_request(), _unit(initial, tmp_path),
        )
    assert len(calls) == 3  # Refetch only when each preceding target arrives.
    assert clock.now - clock.start == pytest.approx(0.12)
    clock.assert_polling_ceiling()
    assert consumer.pending == (first, second)
    assert consumer.consumed == initial.revision
    assert consumer.acks == []


def test_a_full_inbox_with_an_unknown_head_is_refused_immediately(tmp_path, monkeypatch):
    clock = _Clock().install(monkeypatch)
    initial, task = _snapshots()
    events = tuple(SimpleNamespace(revision=f"{0xC0FFEE + index:040x}") for index in range(32))
    consumer = _Inbox(initial, pending=events)
    calls = []

    def provider():
        calls.append(1)
        return Snapshot("f" * 40, initial.state)

    with pytest.raises(SafePointError):
        _point(initial, task, consumer, provider, timeout=5.0, bind=_never).prepare(
            _worker_request(), _unit(initial, tmp_path),
        )
    assert len(calls) == 1
    assert clock.sleeps == []           # no room to grow, so no polling at all
    assert consumer.acks == []
    assert consumer.pending == events

    # The COVERED prefix of a full inbox is still accepted, tail included.
    consumer = _Inbox(initial, pending=events)
    prepared = _point(initial, task, consumer, lambda: Snapshot(events[-1].revision, initial.state)).prepare(
        _worker_request(), _unit(initial, tmp_path),
    )
    prepared.acknowledge()
    assert consumer.acks == [events[-1].revision]


# --------------------------------------------------------------- cancellation


def test_cancellation_during_the_lag_is_cooperative_and_keeps_the_inbox(tmp_path):
    initial, task = _snapshots()
    token = CancellationToken()
    # A real Event set from inside the wait loop: the token's wait() returns at
    # once, so this case sleeps for no wall-clock time at all.
    consumer = _Inbox(initial, on_peek={2: token.event.set})
    calls = []

    def provider():
        calls.append(1)
        return Snapshot("d" * 40, initial.state)

    with pytest.raises(OperationCancelledError):
        _point(initial, task, consumer, provider, timeout=5.0).prepare(
            _worker_request(), _unit(initial, tmp_path), cancel_token=token,
        )
    assert len(calls) == 1                 # nothing is refetched after cancellation
    assert consumer.acks == []
    assert consumer.consumed == initial.revision
    assert consumer.pending == ()


def test_cancellation_before_prepare_before_the_binder_and_at_the_ack(tmp_path):
    initial, task = _snapshots()

    # (a) already cancelled: the authenticated fetch never runs.
    token = CancellationToken()
    token.event.set()
    consumer = _Inbox(initial)
    with pytest.raises(OperationCancelledError):
        _point(
            initial, task, consumer, lambda: pytest.fail("no fetch before cancellation"),
            timeout=1.0, bind=_never,
        ).prepare(_worker_request(), _unit(initial, tmp_path), cancel_token=token)
    assert consumer.acks == []

    # (b) cancelled by the fetch itself: still nothing bound, nothing acked.
    token = CancellationToken()

    def cancelling_provider():
        token.event.set()
        return initial

    consumer = _Inbox(initial)
    with pytest.raises(OperationCancelledError):
        _point(initial, task, consumer, cancelling_provider, timeout=1.0, bind=_never).prepare(
            _worker_request(), _unit(initial, tmp_path), cancel_token=token,
        )
    assert consumer.acks == []
    assert consumer.consumed == initial.revision

    # (c) cancelled after binding: the ack is not a generic input failure, and
    #     nothing is persisted on the way out.
    tail = SimpleNamespace(revision="c" * 40)
    consumer = _Inbox(initial, pending=(tail,))
    prepared = _point(initial, task, consumer, lambda: Snapshot(tail.revision, initial.state)).prepare(
        _worker_request(), _unit(initial, tmp_path),
    )
    token = CancellationToken()
    token.event.set()
    with pytest.raises(OperationCancelledError):
        prepared.acknowledge(cancel_token=token)
    assert consumer.acks == []
    assert consumer.consumed == initial.revision

    # Accepted normally, then cancelled again: a later cancellation cannot roll
    # back the cursor that was already persisted for this attempt.
    prepared.acknowledge()
    assert consumer.acks == [tail.revision]
    with pytest.raises(OperationCancelledError):
        prepared.acknowledge(cancel_token=token)
    assert consumer.acks == [tail.revision]     # accepted once, never rolled back


def test_cancellation_from_status_is_rechecked_immediately_before_persistence(tmp_path, monkeypatch):
    initial, task = _snapshots()
    tail = SimpleNamespace(revision="c" * 40)
    consumer = _Inbox(initial, pending=(tail,))
    prepared = _point(initial, task, consumer, lambda: Snapshot(tail.revision, initial.state)).prepare(
        _worker_request(), _unit(initial, tmp_path),
    )
    token = CancellationToken()
    original_status = consumer.status

    def cancelling_status():
        status = original_status()
        token.event.set()
        return status

    monkeypatch.setattr(consumer, "status", cancelling_status)
    with pytest.raises(OperationCancelledError):
        prepared.acknowledge(cancel_token=token)
    assert consumer.acks == []
    assert consumer.consumed == initial.revision
    assert consumer.pending == (tail,)


def test_cancellation_during_binding_keeps_the_cursor_and_inbox(tmp_path):
    initial, task = _snapshots()
    tail = SimpleNamespace(revision="c" * 40)
    consumer = _Inbox(initial, pending=(tail,))
    token = CancellationToken()

    def cancelling_bind(snapshot, request, workspace):
        bound = _bind(snapshot, request, workspace)
        token.event.set()
        return bound

    with pytest.raises(OperationCancelledError):
        _point(
            initial, task, consumer, lambda: Snapshot(tail.revision, initial.state), bind=cancelling_bind,
        ).prepare(_worker_request(), _unit(initial, tmp_path), cancel_token=token)
    assert consumer.acks == []
    assert consumer.consumed == initial.revision
    assert consumer.pending == (tail,)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not found")
def test_native_cancellation_after_session_construction_is_not_wrapped_as_input_failure(tmp_path, monkeypatch):
    workspace = _new_workspace(tmp_path)
    try:
        initial, task = _snapshots(base=workspace.snapshot.source_head)
        tail = SimpleNamespace(revision="c" * 40)
        consumer = _Inbox(initial, pending=(tail,))
        token = CancellationToken()
        runtime, run = _runtime(tmp_path)

        class Session:
            def __init__(self, **kwargs):
                token.event.set()

            def start(self, _input):
                pytest.fail("cancelled acknowledgement must not start the model")

        class Backend:
            def open_session(self, **kwargs):
                pytest.fail("the model must not open after cancellation")

        monkeypatch.setattr("executor_runtime.native_worker.AgentSession", Session)
        adapter = NativeWorkerAttemptAdapter(
            runtime, run.run_id, Backend(), safe_point=_point(
                initial, task, consumer, lambda: Snapshot(tail.revision, initial.state),
            ),
        )
        with pytest.raises(ExecutorAdapterCancelledError) as caught:
            adapter.run(workspace, _worker_request(), execution_id="exec_cancel_before_ack", cancel_token=token)
        assert isinstance(caught.value, OperationCancelledError)
        assert not isinstance(caught.value, ExecutorAdapterInputError)
        assert consumer.acks == []
        assert consumer.pending == (tail,)
        assert consumer.consumed == initial.revision
    finally:
        workspace.dispose()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not found")
def test_the_native_adapter_maps_a_lag_cancellation_to_cancelled_not_failed(tmp_path):
    workspace = _new_workspace(tmp_path)
    try:
        initial, task = _snapshots(base=workspace.snapshot.source_head)
        token = CancellationToken()
        consumer = _Inbox(initial, on_peek={2: token.event.set})
        runtime, run = _runtime(tmp_path)

        class Backend:  # pragma: no cover
            def open_session(self, **kwargs):
                pytest.fail("a cancelled wait must never open a model session")

        adapter = NativeWorkerAttemptAdapter(
            runtime, run.run_id, Backend(),
            safe_point=_point(
                initial, task, consumer, lambda: Snapshot("d" * 40, initial.state), timeout=5.0,
            ),
        )
        with pytest.raises(ExecutorAdapterCancelledError) as caught:
            adapter.run(workspace, _worker_request(), execution_id="exec_lag_cancel", cancel_token=token)
        # The cancelled family the pipeline reports as CANCELLED, never the
        # fixed input failure it would report as FAILED.
        assert isinstance(caught.value, OperationCancelledError)
        assert not isinstance(caught.value, ExecutorAdapterInputError)
        assert str(caught.value).startswith("Worker preparation cancelled")
        assert consumer.acks == []
        assert consumer.consumed == initial.revision
    finally:
        workspace.dispose()


# ------------------------------------------------------- rechecked while waiting


@pytest.mark.parametrize("state", _TERMINAL_STATES)
def test_a_terminal_state_seen_while_waiting_blocks_binding_and_ack(tmp_path, monkeypatch, state):
    clock = _Clock().install(monkeypatch)
    initial, task = _snapshots()
    consumer = _Inbox(initial, states={4: state})   # observed inside the wait loop
    calls = []

    def provider():
        calls.append(1)
        return Snapshot("d" * 40, initial.state)

    with pytest.raises(SafePointError):
        _point(initial, task, consumer, provider, timeout=1.0, bind=_never).prepare(
            _worker_request(), _unit(initial, tmp_path),
        )
    assert len(calls) == 1
    assert len(clock.sleeps) == 1
    assert consumer.acks == []
    assert consumer.consumed == initial.revision
    assert consumer.pending == ()


def test_a_state_change_inside_the_refetched_fetch_blocks_the_binder(tmp_path, monkeypatch):
    clock = _Clock().install(monkeypatch)
    initial, task = _snapshots()
    event = SimpleNamespace(revision="c" * 40)
    consumer = _Inbox(initial, deliver={2: (event,)})
    calls = []

    def provider():
        calls.append(1)
        if len(calls) == 2:
            # The host lost the stream while the authenticated refetch ran.
            consumer.current_state = "resnapshot_required"
        return Snapshot(event.revision, initial.state)

    with pytest.raises(SafePointError):
        _point(initial, task, consumer, provider, timeout=1.0, bind=_never).prepare(
            _worker_request(), _unit(initial, tmp_path),
        )
    assert len(calls) == 2
    assert consumer.acks == []
    assert consumer.pending == (event,)


def test_a_cursor_another_host_writer_advanced_blocks_binding(tmp_path, monkeypatch):
    clock = _Clock().install(monkeypatch)
    initial, task = _snapshots()
    consumer = _Inbox(initial, consumed={4: "e" * 40})
    calls = []

    def provider():
        calls.append(1)
        return Snapshot("d" * 40, initial.state)

    with pytest.raises(SafePointError):
        _point(initial, task, consumer, provider, timeout=1.0, bind=_never).prepare(
            _worker_request(), _unit(initial, tmp_path),
        )
    assert len(calls) == 1
    assert len(clock.sleeps) == 1
    assert consumer.acks == []
    assert consumer.consumed == "e" * 40


# ------------------------------------------------------------- fail-closed fetch


def test_a_failed_refetch_and_a_reassigned_task_fail_closed_without_echo(tmp_path, monkeypatch):
    clock = _Clock().install(monkeypatch)
    initial, task = _snapshots()
    event = SimpleNamespace(revision="c" * 40)

    # (a) the authenticated refetch fails: no binder, no ack, no provider text.
    consumer = _Inbox(initial, deliver={2: (event,)})
    calls = []

    def failing_provider():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("secret credential store detail")
        return Snapshot(event.revision, initial.state)

    with pytest.raises(SafePointError) as caught:
        _point(initial, task, consumer, failing_provider, timeout=1.0, bind=_never).prepare(
            _worker_request(), _unit(initial, tmp_path),
        )
    assert str(caught.value) == SAFE_POINT_ERROR
    assert "secret" not in str(caught.value) and "credential" not in str(caught.value)
    assert consumer.acks == []
    assert consumer.pending == (event,)

    # (b) the refetched head reassigns the approved task: retargeting needs a
    #     new approved binding, never a silent one.
    reassigned = replace(
        initial.state,
        tasks={task.id: Task(task.id, "bob", task.goal, task.scopes, task.status, task.context_revision)},
    )
    consumer = _Inbox(initial, deliver={2: (event,)})
    seen = []

    def reassigning_provider():
        seen.append(1)
        return Snapshot(event.revision, reassigned if len(seen) == 2 else initial.state)

    with pytest.raises(SafePointError):
        _point(initial, task, consumer, reassigning_provider, timeout=1.0, bind=_never).prepare(
            _worker_request(), _unit(initial, tmp_path),
        )
    assert len(seen) == 2
    assert consumer.acks == []
    assert consumer.pending == (event,)


def test_a_blocking_fetch_is_never_preempted_but_its_late_result_is_refused(tmp_path, monkeypatch):
    clock = _Clock().install(monkeypatch)
    initial, task = _snapshots()
    consumer = _Inbox(initial)

    def blocking_provider():
        # The deadline is cooperative: a fetch already in flight is not
        # interrupted, but nothing it returns late may be bound.
        clock.now += 30.0
        return Snapshot("d" * 40, initial.state)

    with pytest.raises(SafePointError):
        _point(initial, task, consumer, blocking_provider, timeout=1.0, bind=_never).prepare(
            _worker_request(), _unit(initial, tmp_path),
        )
    assert clock.sleeps == []
    assert consumer.acks == []
    assert consumer.consumed == initial.revision


# ------------------------------------------------- real Git + real loopback wire


@pytest.mark.skipif(shutil.which("git") is None, reason="git not found")
def test_a_real_delayed_wire_binds_and_acknowledges_before_the_first_model_call(
    collab_session, repo_workspace, tmp_path, monkeypatch,
):
    """A real Git worktree, a real socket and a real consumer: the revision is
    published while the wire is held, so the wait is a genuine lag and not a
    stubbed one. Nothing is bound, acknowledged or persisted until the wire
    releases, and the cursor has moved before the first model call."""
    session = collab_session
    coordinator, consumer, initial = session.coordinator, session.consumer, session.initial
    checkpoint = tmp_path / "checkpoint" / "cursor.json"
    baseline = consumer.status()["consumed_revision"]
    before = checkpoint.read_bytes() if checkpoint.exists() else None
    assert baseline == initial.revision

    reached_wire = threading.Event()
    entered_wait = threading.Event()
    release = threading.Event()
    original_replay = Coordinator.replay
    original_sleep = safe_point_module.sleep

    def observed_sleep(seconds):
        entered_wait.set()
        original_sleep(seconds)

    def gated_replay(self, credential, **kwargs):
        page = original_replay(self, credential, **kwargs)
        if page.events:
            # The page is already read (no core lock is held), so holding it
            # here is a slow wire, not a stalled coordinator.
            reached_wire.set()
            assert release.wait(10.0), "test did not release the wire gate"
        return page

    monkeypatch.setattr(Coordinator, "replay", gated_replay)
    monkeypatch.setattr(safe_point_module, "sleep", observed_sleep)
    head = coordinator.update_context(
        ALICE, build_context(goal="delayed head", decisions=["one"], interfaces={}),
        expected_revision=initial.revision,
    )
    assert head != initial.revision

    seen: list[str] = []
    cursors: list[str] = []

    class Backend:
        def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
            class Session:
                def respond(self, input_items):
                    text = next(
                        (item.text for item in input_items if isinstance(item, UserInput)),
                        input_items[0].text,
                    )
                    seen.append(text)
                    # The authoritative provider is coordinator.snapshot(ALICE).
                    cursors.append(consumer.status()["consumed_revision"])
                    return ModelTurn("done", (), ModelStopReason.COMPLETED, ModelUsage())

            return Session()

    adapter = NativeWorkerAttemptAdapter(
        session.runtime, session.run.run_id, Backend(),
        safe_point=NativeWorkerSafePoint(
            consumer, lambda: coordinator.snapshot(ALICE), initial_snapshot=initial,
            member_id="alice", approved_task=session.task, replay_wait_timeout=5.0,
        ),
    )
    failures: list[BaseException] = []

    def attempt():
        try:
            adapter.run(repo_workspace, _worker_request(), execution_id="exec_real_lag")
        except BaseException as error:  # noqa: BLE001 - reported to the main thread
            failures.append(error)

    worker = threading.Thread(target=attempt, name="safe-point-attempt", daemon=True)
    try:
        worker.start()
        assert _wait_for(reached_wire.is_set), "the wire never carried the new revision"
        assert _wait_for(entered_wait.is_set), "the attempt did not enter replay-lag waiting"
        assert seen == []                       # no model work during the lag
        assert consumer.status()["consumed_revision"] == baseline
        assert consumer.peek() == ()
        after_lag = checkpoint.read_bytes() if checkpoint.exists() else None
        assert after_lag == before              # the cursor is not persisted yet
        release.set()
        worker.join(10.0)
        assert not worker.is_alive(), "the worker attempt did not finish"
        assert failures == []
    finally:
        # Released BEFORE the fixture closes the consumer, so no server or
        # consumer thread is ever left blocked in this wait.
        release.set()
        if worker.ident is not None:
            worker.join(10.0)
            assert not worker.is_alive(), "test attempt must drain before consumer teardown"

    assert len(seen) == 1
    assert f"revision={head}" in seen[0]
    assert f"revision={initial.revision}" not in seen[0]
    assert cursors == [head]                   # acknowledged BEFORE the model call
    assert consumer.status()["consumed_revision"] == head
    persisted = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert persisted["consumed_revision"] == head
    assert consumer.status()["state"] in _RUNNABLE_STATES   # still host-owned


@pytest.mark.skipif(shutil.which("git") is None, reason="git not found")
def test_cancel_after_real_checkpoint_commit_keeps_cursor_and_does_not_open_model(
    collab_session, repo_workspace, tmp_path, monkeypatch,
):
    session = collab_session
    consumer = session.consumer
    head = session.coordinator.update_context(
        ALICE, build_context(goal="accepted before cancellation", decisions=[], interfaces={}),
        expected_revision=session.initial.revision,
    )
    assert _wait_for(lambda: len(consumer.peek()) == 1)
    token = CancellationToken()
    original_acknowledge = consumer.acknowledge_at_safe_point

    def commit_then_cancel(revision):
        original_acknowledge(revision)
        token.event.set()

    monkeypatch.setattr(consumer, "acknowledge_at_safe_point", commit_then_cancel)

    class Backend:
        def open_session(self, **kwargs):
            pytest.fail("an observed post-commit cancellation must not open the model")

    adapter = NativeWorkerAttemptAdapter(
        session.runtime, session.run.run_id, Backend(), safe_point=NativeWorkerSafePoint(
            consumer, lambda: session.coordinator.snapshot(ALICE), initial_snapshot=session.initial,
            member_id="alice", approved_task=session.task,
        ),
    )
    with pytest.raises(ExecutorAdapterCancelledError):
        adapter.run(repo_workspace, _worker_request(), execution_id="exec_post_commit_cancel", cancel_token=token)
    assert consumer.status()["consumed_revision"] == head
    assert consumer.peek() == ()
    checkpoint = json.loads((tmp_path / "checkpoint" / "cursor.json").read_text(encoding="utf-8"))
    assert checkpoint["consumed_revision"] == head


@pytest.mark.skipif(shutil.which("git") is None, reason="git not found")
def test_the_pipeline_reports_cancelled_when_the_safe_point_wait_is_cancelled(
    collab_session, repo_workspace, monkeypatch,
):
    """Cancel at an actual wait while the new revision is held off the wire.

    Delivery is gated, so a fast subscription cannot bypass the wait and make
    this test depend on whether the initial snapshot was already caught up.
    """
    session = collab_session
    cancel = threading.Event()
    calls: list[int] = []
    waits = []
    release = threading.Event()
    original_replay = Coordinator.replay
    original_wait = CancellationToken.wait

    def gated_replay(self, credential, **kwargs):
        page = original_replay(self, credential, **kwargs)
        if page.events:
            assert release.wait(10.0), "test did not release the wire gate"
        return page

    def cancelling_wait(token, timeout=None):
        waits.append(timeout)
        assert session.consumer.status()["consumed_revision"] == session.initial.revision
        assert session.consumer.peek() == ()
        cancel.set()
        return original_wait(token, timeout)

    monkeypatch.setattr(Coordinator, "replay", gated_replay)
    monkeypatch.setattr(CancellationToken, "wait", cancelling_wait)
    session.coordinator.update_context(
        ALICE, build_context(goal="ahead of the inbox", decisions=[], interfaces={}),
        expected_revision=session.initial.revision,
    )

    def provider():
        calls.append(1)
        return session.coordinator.snapshot(ALICE)

    class NeverCalled:  # pragma: no cover
        def open_session(self, **kwargs):
            pytest.fail("a cancelled worker attempt must never call a model")

    backends = {"openai": iter((
        ScriptedBackend([_completed_turn(PLAN_JSON)]), NeverCalled(), NeverCalled(),
    ))}
    try:
        ports = engine_factory.build_pipeline_ports(
            session.runtime, session.run.run_id,
            {"planner": "openai", "coder": "openai", "reviewer": "openai"},
            backend_factory=lambda name: next(backends[name]),
            worker_safe_point=NativeWorkerSafePoint(
                session.consumer, provider, initial_snapshot=session.initial,
                member_id="alice", approved_task=session.task, replay_wait_timeout=5.0,
            ),
        )
        report = PipelineRunner(
            session.runtime, planner=ports.planner, worker=ports.worker,
            verification=ports.verification, reviewer=ports.reviewer,
            change_provider=ports.change_provider, max_fix_attempts=1,
        ).run(
            session.run.run_id, repo_workspace, TASK_TEXT,
            pinned_paths=PINNED_PATHS, cancel_event=cancel,
        )
    finally:
        release.set()  # Always release the listener before consumer teardown.
    assert report.status is PipelineStatus.CANCELLED
    assert len(calls) == 1
    assert len(waits) == 1
    assert session.consumer.status()["consumed_revision"] == session.initial.revision
