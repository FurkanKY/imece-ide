"""Private, one-shot participant task-status command boundary."""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from test_collab_host_lifecycle import env
from test_collab_transport import servers
from test_pipeline_integration import repo_workspace

from collab_runtime.commands import TaskCommandError
from collab_runtime.host import (
    CollaborationHost, ParticipantCommandError,
)
from test_collab_host_lifecycle import _approved


@pytest.fixture
def bound(env):
    host, _, session = _approved(env)
    return host, session


def test_preview_is_fresh_private_metadata_only_and_does_not_acknowledge(bound, env):
    _, session = bound
    before = session.status()
    accepted = session.accepted_binding
    ticket = session.preview_task_status("waiting")
    assert set(ticket) == {
        "ticketId", "runId", "projectRoot", "sessionId", "taskId", "memberId",
        "fromStatus", "targetStatus", "expectedRevision", "contextHash",
        "freshContextDiffersFromAccepted",
    }
    assert ticket["expectedRevision"] == env.revision
    assert ticket["fromStatus"] == "running"
    assert session.status() == before
    assert accepted is None and session.accepted_binding is None
    raw = json.dumps(ticket)
    assert env.credential not in raw and hashlib.sha256(env.credential.encode()).hexdigest() not in raw


def test_confirm_changes_only_own_status_and_returns_receipt_without_readback(bound, env):
    _, session = bound
    ticket = session.preview_task_status("waiting")
    response = session.confirm_task_status(ticket["ticketId"])
    assert set(response) == {"revision", "taskId", "status"}
    assert response["taskId"] == "factory-task"
    assert response["status"] == "waiting"
    state = env.coordinator.snapshot(env.credential).state
    assert state.tasks["factory-task"].status == "waiting"
    with pytest.raises(ParticipantCommandError):
        session.confirm_task_status(ticket["ticketId"])


def test_stale_cas_does_not_overwrite_newer_owner_status(bound, env):
    _, session = bound
    ticket = session.preview_task_status("waiting")
    env.coordinator.update_task_status(env.credential, task_id="factory-task", status="queued",
                                       expected_revision=ticket["expectedRevision"])
    with pytest.raises(ParticipantCommandError) as caught:
        session.confirm_task_status(ticket["ticketId"])
    assert caught.value.code == "stale_revision" and not caught.value.outcome_uncertain
    assert env.store.fetch_state()[1].tasks["factory-task"].status == "queued"


def test_ticket_consumed_before_single_uncertain_command(bound, env):
    calls = []

    class Uncertain:
        def __init__(self, endpoint, *, credential):
            assert endpoint == env.base_url and credential == env.credential

        def update_task_status(self, **kwargs):
            calls.append(kwargs)
            raise TaskCommandError("outcome_unknown", outcome_uncertain=True)

    session = bound[1]
    # Replace just the command factory; no real write occurs.
    session._command_factory = Uncertain
    ticket = session.preview_task_status("waiting")
    with pytest.raises(ParticipantCommandError) as caught:
        session.confirm_task_status(ticket["ticketId"])
    assert caught.value.code == "outcome_unknown" and caught.value.outcome_uncertain
    assert env.credential not in str(caught.value) and len(calls) == 1
    with pytest.raises(ParticipantCommandError):
        session.confirm_task_status(ticket["ticketId"])
    assert len(calls) == 1


@pytest.mark.parametrize("bad", ["done", "unknown", None])
def test_preview_rejects_non_slice_statuses(bound, bad):
    with pytest.raises(ParticipantCommandError):
        bound[1].preview_task_status(bad)


def test_ticket_registry_is_bounded_and_evicted_ticket_cannot_be_used(bound):
    session = bound[1]
    tickets = [session.preview_task_status("waiting") for _ in range(6)]
    assert len(session._participant_tickets) == 4
    with pytest.raises(ParticipantCommandError):
        session.confirm_task_status(tickets[0]["ticketId"])


def test_discard_is_idempotent_and_zero_io(bound):
    session = bound[1]
    ticket = session.preview_task_status("waiting")
    session.discard_task_status(ticket["ticketId"])
    session.discard_task_status(ticket["ticketId"])
    with pytest.raises(ParticipantCommandError):
        session.confirm_task_status(ticket["ticketId"])


def test_expired_ticket_cannot_be_confirmed(bound, monkeypatch):
    session = bound[1]
    ticket = session.preview_task_status("waiting")
    _, created, record, generation = session._participant_tickets[ticket["ticketId"]]
    session._participant_tickets[ticket["ticketId"]] = (
        session._participant_tickets[ticket["ticketId"]][0], created - 301, record, generation)
    with pytest.raises(ParticipantCommandError) as caught:
        session.confirm_task_status(ticket["ticketId"])
    assert caught.value.code == "expired"


def test_reapprove_invalidates_ticket_even_when_context_is_same(bound, env):
    host, session = bound
    ticket = session.preview_task_status("waiting")
    fresh = host.preview(env.source, env.base_url, env.credential, "alice", "factory-task")
    approval = host.approve(fresh["previewId"], env.source)
    session.reapprove(approval["approvalHandle"])
    with pytest.raises(ParticipantCommandError):
        session.confirm_task_status(ticket["ticketId"])


def test_activate_and_close_invalidate_tickets(bound, env):
    _, session = bound
    activation_ticket = session.preview_task_status("waiting")
    from test_collab_host_lifecycle import _stand_in
    session.activate(_stand_in(env))
    with pytest.raises(ParticipantCommandError):
        session.confirm_task_status(activation_ticket["ticketId"])
    session.deactivate()
    closing_ticket = session.preview_task_status("waiting")
    session.close()
    with pytest.raises(ParticipantCommandError):
        session.confirm_task_status(closing_ticket["ticketId"])


def test_busy_operations_are_serialized_but_status_is_responsive(bound, env):
    host, session = bound
    entered, release = threading.Event(), threading.Event()
    original = session._command_factory

    class Gated:
        def __init__(self, endpoint, *, credential):
            self.client = original(endpoint, credential=credential)

        def update_task_status(self, **kwargs):
            entered.set()
            assert release.wait(5)
            return self.client.update_task_status(**kwargs)

    session._command_factory = Gated
    ticket = session.preview_task_status("waiting")
    command = threading.Thread(target=session.confirm_task_status, args=(ticket["ticketId"],))
    command.start()
    assert entered.wait(5)
    assert session.status()["active"] is False
    done = threading.Event()
    close = threading.Thread(target=lambda: (session.close(), done.set()))
    close.start()
    time.sleep(0.05)
    assert not done.is_set()
    release.set()
    command.join(5)
    close.join(5)
    assert done.is_set()
