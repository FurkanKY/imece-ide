"""Bounded, opt-in replay reconciliation at the Worker safe point."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_runtime.cancellation import CancellationToken, OperationCancelledError
from collab_runtime.coordinator import Snapshot
from collab_runtime.safe_point import NativeWorkerSafePoint, SafePointError
from test_collab_safe_point import (
    _MemoryConsumer, _shared, _snapshots, _unit_workspace, _worker_request,
)
from collab_runtime.context import render_snapshot_block


def _helper(initial, task, consumer, provider, tmp_path, *, timeout=0.0, bind=None):
    return NativeWorkerSafePoint(
        consumer, provider, initial_snapshot=initial, member_id="alice",
        approved_task=task, replay_wait_timeout=timeout,
        bind_input=bind or (lambda snapshot, request, workspace: replace(
            request, rendered_input=request.rendered_input + "\n" + render_snapshot_block(snapshot)
        )),
    )


@pytest.mark.parametrize("invalid", [True, False, "1", None, float("nan"), float("inf"), -1, 5.01])
def test_replay_wait_configuration_is_strict_and_fixed(invalid):
    initial, task = _snapshots()
    consumer = _MemoryConsumer(initial)
    with pytest.raises(ValueError, match="finite number from 0 to 5"):
        _helper(initial, task, consumer, lambda: initial, None, timeout=invalid)


def test_positive_mode_accepts_authoritative_known_prefix_only(tmp_path):
    initial, task = _snapshots()
    first, last, later = (SimpleNamespace(revision=value * 40) for value in "cde")
    consumer = _MemoryConsumer(initial, pending=(first, last))
    helper = _helper(
        initial, task, consumer, lambda: Snapshot(first.revision, initial.state), tmp_path, timeout=0.2,
    )
    prepared = helper.prepare(_worker_request(), _unit_workspace(initial.state.base_commit, tmp_path))
    consumer.pending = (first, last, later)
    prepared.acknowledge()
    assert consumer.acks == [first.revision]
    assert consumer.pending == (first, last, later)


def test_default_mode_does_not_wait_or_refetch(tmp_path):
    initial, task = _snapshots()
    event = SimpleNamespace(revision="c" * 40)
    consumer = _MemoryConsumer(initial, pending=(event,))
    calls = []
    helper = _helper(initial, task, consumer, lambda: calls.append(1) or initial, tmp_path)
    with pytest.raises(SafePointError):
        helper.prepare(_worker_request(), _unit_workspace(initial.state.base_commit, tmp_path))
    assert calls == [1]
    assert consumer.acks == []


def test_wait_refetches_after_unknown_snapshot_revision_reaches_inbox(tmp_path):
    initial, task = _snapshots()
    event = SimpleNamespace(revision="c" * 40)

    class ArrivingConsumer(_MemoryConsumer):
        peeks = 0

        def peek(self):
            self.peeks += 1
            if self.peeks == 2:
                self.pending = (event,)
            return self.pending

    consumer = ArrivingConsumer(initial)
    calls = []

    def provider():
        calls.append(1)
        return Snapshot(event.revision, initial.state)

    helper = _helper(initial, task, consumer, provider, tmp_path, timeout=0.5)
    prepared = helper.prepare(_worker_request(), _unit_workspace(initial.state.base_commit, tmp_path))
    assert len(calls) == 2
    assert prepared.request is not None
    assert consumer.acks == []
    prepared.acknowledge()
    assert consumer.acks == [event.revision]


def test_prepare_cancellation_is_not_safe_point_failure(tmp_path):
    initial, task = _snapshots()
    consumer = _MemoryConsumer(initial)
    token = CancellationToken()
    token.cancel()
    helper = _helper(initial, task, consumer, lambda: pytest.fail("provider must not run"), tmp_path, timeout=1)
    with pytest.raises(OperationCancelledError):
        helper.prepare(_worker_request(), _unit_workspace(initial.state.base_commit, tmp_path), cancel_token=token)
    assert consumer.acks == []
