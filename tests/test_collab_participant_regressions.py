"""Regressions for the newly wired participant task-status boundary.

TEST-ONLY. Nothing here changes production behaviour and nothing fakes the
host boundary: the ``env`` world is the real one from
``test_collab_host_lifecycle`` -- a temporary Git source checkout, a real
loopback HTTP server over a real Git-backed store, the real
``LoopbackSnapshotClient``/``LoopbackTaskClient``, the real ``RevisionConsumer``,
the real POSIX checkpoint lease and the real private cursor namespace. Only two
narrow seams inject what a live hub cannot produce deterministically (a scripted
``HEAD`` reader and a scripted command client / ticket session); both are
labelled where they are used.

Lockdown covered here (it complements, and does not duplicate,
``test_collab_participant_commands`` / ``test_collab_participant_bridge``):

* ``freshContextDiffersFromAccepted`` is ADVISORY: ``None`` without an accepted
  binding, it tracks the accepted context hash, and it never blocks a write;
* a fresh ``HEAD`` drift is refused for preview AND confirm, refuses BEFORE any
  write, registers no ticket, and a refusal does not spend the ticket;
* participant commands are refused while a run is active or closed, and a
  refused re-approval neither consumes its handle nor swaps the record;
* an uncertain confirmation is issued exactly once, spends the ticket and moves
  no native cursor / accepted binding / session status;
* a forged receipt is ``protocol_error`` with ``outcome_uncertain=True`` and is
  never retried; the 4-slot registry and its 300 s TTL stay bounded;
* ``borrow_participant_context`` (real code, mocked worker/coordinator/
  workspace) requires an idle WAITING_USER pipeline run with a real accepted
  binding, and a borrowed generation/root drift is refused BEFORE any command;
* the delivery lease blocks apply / reject / followUp / run.start and defers the
  resource drain until the lease is released;
* the three participant RPCs refuse malformed shapes, unknown runs, drifted
  leases and saturated slots before any I/O, never leak a semaphore slot and
  never surface a secret; uncertainty is only ever reported for a started
  confirmation.

RUN:
  env -u PYTHONPATH -u IMECE_RUN_LIVE_API_TESTS PYTHONDONTWRITEBYTECODE=1 \\
      QT_QPA_PLATFORM=offscreen PATH="$PWD/.venv/bin:$PATH" \\
      .venv/bin/python -m pytest -q -p no:cacheprovider \\
      tests/test_collab_participant_regressions.py
"""

from __future__ import annotations

import json
import errno
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication

from test_collab_host_lifecycle import env
from test_collab_transport import servers
from test_pipeline_integration import repo_workspace

from collab_runtime.commands import TaskCommandError, TaskStatusReceipt
from collab_runtime.host import HostCollaborationError, ParticipantCommandError
from run_runtime.models import RunStatus
from webhost import state
from webhost.api import collab as collab_api
from webhost.api import run as run_api
from webhost.bridge import BridgeError, HostBridge
from test_collab_host_lifecycle import (  # noqa: E402
    MEMBER, TASK_ID, _activate_and_stream, _approved, _commit_in_source, _initial_request,
    _publish_context,
)

RUN_ID = "participant-run"


# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def bound(env):
    """preview -> approve -> bind_run with no activation of any kind."""
    host, _, session = _approved(env, run_id=RUN_ID)
    return host, session


@pytest.fixture
def accepted(bound, env):
    """A really activated, acknowledged, then deactivated session: idle + binding."""
    host, session = bound
    _activate_and_stream(env, session)
    prepared = session.prepare(_initial_request(), env.workspace)
    prepared.acknowledge()
    session.deactivate()
    assert session.active is False and session.accepted_binding is not None
    return host, session


@pytest.fixture(autouse=True)
def _isolated_run_state():
    """Never leak run/delivery/project-cache state into or out of a test."""
    active = dict(run_api._active)
    draining = list(run_api._draining_workers)
    codes = dict(run_api._collaboration_cleanup_codes)
    with run_api._delivery_lock:
        leases, closing = dict(run_api._delivery_leases), set(run_api._delivery_closing)
    yield
    run_api._active.clear()
    run_api._active.update(active)
    run_api._draining_workers[:] = draining
    run_api._collaboration_cleanup_codes.clear()
    run_api._collaboration_cleanup_codes.update(codes)
    with run_api._delivery_lock:
        run_api._delivery_leases.clear()
        run_api._delivery_leases.update(leases)
        run_api._delivery_closing.clear()
        run_api._delivery_closing.update(closing)
    # A drained run writes the global status cache; never leave it for another test.
    state.set_collaboration_status_cache(None)


def _idle_worker(*, finished=True):
    return SimpleNamespace(isRunning=lambda: False, isFinished=lambda: finished)


def _coordinator(status=RunStatus.WAITING_USER):
    return SimpleNamespace(get_run=lambda: SimpleNamespace(status=status))


# ------------------------------------------------------- 1. advisory hash only


def test_context_hash_advisory_is_none_then_tracks_the_accepted_binding(accepted, env):
    """``freshContextDiffersFromAccepted`` informs; it never gates a write."""
    _, session = accepted
    accepted_hash = session.accepted_binding.state.context.content_hash

    # No delivered delta since the acknowledgement: the fresh hash is identical.
    same = session.preview_task_status("waiting")
    assert same["contextHash"] == accepted_hash
    assert same["freshContextDiffersFromAccepted"] is False

    # An owner-side context publication moves the fresh hash only.
    _publish_context(env, "an owner goal the coder has not accepted")
    differs = session.preview_task_status("running")
    assert differs["contextHash"] != accepted_hash
    assert differs["freshContextDiffersFromAccepted"] is True

    # Advisory only: the differing hash does not refuse, block or alter the write.
    receipt = session.confirm_task_status(differs["ticketId"])
    assert receipt["taskId"] == TASK_ID and receipt["status"] == "running"
    # The receipt carries the committed revision, not the reviewed one.
    assert receipt["revision"] != differs["expectedRevision"]
    assert env.store.fetch_state()[1].tasks[TASK_ID].status == "running"


def test_context_hash_advisory_is_null_without_an_accepted_binding(bound, env):
    """A pre-activation session must not invent an accepted-context claim."""
    _, session = bound
    assert session.accepted_binding is None
    ticket = session.preview_task_status("waiting")
    assert ticket["freshContextDiffersFromAccepted"] is None
    assert session.accepted_binding is None
    assert not env.cursor_root.exists(), "a status command creates no native namespace"


# --------------------------------------------------------- 2. fresh HEAD drift


def test_preview_refuses_a_moved_real_head_and_registers_no_ticket(bound, env):
    """A real ``HEAD`` commit made after the approval fails closed, pre-write."""
    _, session = bound
    moved = _commit_in_source(env, "participant-drift")
    assert moved != env.head

    with pytest.raises(ParticipantCommandError) as caught:
        session.preview_task_status("waiting")
    assert caught.value.code == "stale" and not caught.value.outcome_uncertain
    assert session._participant_tickets == {}
    assert session.accepted_binding is None
    assert env.store.fetch_state()[1].tasks[TASK_ID].status == "running"


def test_head_drift_inside_the_preview_http_read_is_refused(bound, monkeypatch, env):
    """``HEAD`` may move while the snapshot request is in flight; still refuse."""
    host, session = bound
    calls = {"n": 0}

    def drifting(root):
        calls["n"] += 1
        # The pre-HTTP read matches the approval; the post-HTTP re-read does not.
        return env.head if calls["n"] == 1 else "b" * 40

    monkeypatch.setattr(host, "_head_reader", drifting)
    with pytest.raises(ParticipantCommandError) as caught:
        session.preview_task_status("waiting")
    assert caught.value.code == "stale" and not caught.value.outcome_uncertain
    assert calls["n"] >= 2, "the post-HTTP re-read must be the refusing one"
    assert session._participant_tickets == {}


def test_head_drift_refusal_does_not_spend_the_ticket_and_writes_nothing(
        bound, monkeypatch, env):
    """A refused confirm issues NO command and leaves the ticket re-usable."""
    host, session = bound
    real = host._head_reader
    ticket = session.preview_task_status("waiting")
    monkeypatch.setattr(host, "_head_reader", lambda root: "b" * 40)

    with pytest.raises(ParticipantCommandError) as caught:
        session.confirm_task_status(ticket["ticketId"])
    assert caught.value.code == "stale" and not caught.value.outcome_uncertain
    # The refusal came BEFORE the ticket was spent and before any command.
    assert list(session._participant_tickets) == [ticket["ticketId"]]
    assert env.store.fetch_state()[1].tasks[TASK_ID].status == "running"

    # Restoring the checked-out head lets the very same reviewed ticket commit:
    # the drift refusal consumed neither the ticket nor the reviewed revision.
    monkeypatch.setattr(host, "_head_reader", real)
    assert real(env.source) == env.head
    assert session.confirm_task_status(ticket["ticketId"])["status"] == "waiting"
    assert session._participant_tickets == {}
    assert env.store.fetch_state()[1].tasks[TASK_ID].status == "waiting"


# --------------------------------------------- 3. active / closed refusals


def test_participant_commands_are_refused_while_the_run_is_active(bound, env):
    """An active session issues no participant command and registers no ticket."""
    _, session = bound
    _activate_and_stream(env, session)
    for call in (lambda: session.preview_task_status("waiting"),
                 lambda: session.confirm_task_status("never-issued")):
        with pytest.raises(ParticipantCommandError) as caught:
            call()
        assert caught.value.code == "busy" and not caught.value.outcome_uncertain
    assert session._participant_tickets == {}
    assert env.store.fetch_state()[1].tasks[TASK_ID].status == "running"


def test_a_refused_reapproval_keeps_its_handle_and_the_original_record(bound, env):
    """A busy re-approval is a no-op: the handle and the credential survive."""
    host, session = bound
    _activate_and_stream(env, session)
    fresh = host.preview(env.source, env.base_url, env.credential, MEMBER, TASK_ID)
    approval = host.approve(fresh["previewId"], env.source)
    handle = approval["approvalHandle"]
    original_record = session._record

    with pytest.raises(HostCollaborationError) as caught:
        session.reapprove(handle)
    assert caught.value.code == "busy"
    assert host._approved.get(handle) is not None, "a refused re-approval consumes nothing"
    assert session._record is original_record

    session.deactivate()
    session.reapprove(handle)
    # The successful retry swapped the record and consumed its handle.
    assert session._record is not original_record
    assert handle not in host._approved
    assert session.status()["taskId"] == TASK_ID and session.status()["memberId"] == MEMBER


def test_a_closed_session_refuses_every_participant_command(bound, env):
    _, session = bound
    ticket = session.preview_task_status("waiting")
    session.close()
    for call in (lambda: session.preview_task_status("waiting"),
                 lambda: session.confirm_task_status(ticket["ticketId"])):
        with pytest.raises(ParticipantCommandError) as caught:
            call()
        assert caught.value.code == "busy" and not caught.value.outcome_uncertain
    assert session._participant_tickets == {}
    assert env.store.fetch_state()[1].tasks[TASK_ID].status == "running"


# ------------------------------------- 4. exactly-once uncertain confirmation


def test_uncertain_confirmation_changes_no_native_cursor_binding_or_status(bound, env):
    """One uncertain command: ticket spent, cursor/binding/status untouched."""
    _, session = bound
    calls = []

    class Uncertain:
        def __init__(self, endpoint, *, credential):
            assert endpoint == env.base_url and credential == env.credential

        def update_task_status(self, **kwargs):
            calls.append(kwargs)
            raise TaskCommandError("outcome_unknown", outcome_uncertain=True)

    session._command_factory = Uncertain
    before = session.status()
    ticket = session.preview_task_status("waiting")
    with pytest.raises(ParticipantCommandError) as caught:
        session.confirm_task_status(ticket["ticketId"])
    assert caught.value.code == "outcome_unknown" and caught.value.outcome_uncertain
    assert len(calls) == 1, "the command client already sent the write exactly once"

    # No native state moved: no private namespace, no binding, no status delta.
    assert not env.cursor_root.exists()
    assert session.accepted_binding is None
    assert session.active is False
    assert session.status() == before
    # No retry: the reviewed ticket is spent even though the outcome is unknown.
    with pytest.raises(ParticipantCommandError) as again:
        session.confirm_task_status(ticket["ticketId"])
    assert again.value.code == "expired" and not again.value.outcome_uncertain
    assert len(calls) == 1
    assert env.store.fetch_state()[1].tasks[TASK_ID].status == "running"


@pytest.mark.parametrize("receipt", [
    TaskStatusReceipt("a" * 40, "other-task", "waiting"),
    TaskStatusReceipt("a" * 40, TASK_ID, "queued"),
    TaskStatusReceipt("not-a-sha", TASK_ID, "waiting"),
    TaskStatusReceipt("A" * 40, TASK_ID, "waiting"),
])
def test_forged_receipt_is_unknown_and_never_retried(bound, env, receipt):
    """A receipt that does not match the reviewed ticket is ``protocol_error``."""
    _, session = bound
    calls = []

    class Forged:
        def __init__(self, endpoint, *, credential):
            pass

        def update_task_status(self, **kwargs):
            calls.append(kwargs)
            return receipt

    session._command_factory = Forged
    ticket = session.preview_task_status("waiting")
    with pytest.raises(ParticipantCommandError) as caught:
        session.confirm_task_status(ticket["ticketId"])
    assert caught.value.code == "protocol_error" and caught.value.outcome_uncertain
    assert len(calls) == 1
    with pytest.raises(ParticipantCommandError):
        session.confirm_task_status(ticket["ticketId"])
    assert len(calls) == 1
    assert env.store.fetch_state()[1].tasks[TASK_ID].status == "running"


def test_ticket_registry_evicts_the_oldest_and_keeps_the_newest(bound):
    """The 4-slot registry bounds memory and always drops the oldest review."""
    _, session = bound
    tickets = [session.preview_task_status("waiting") for _ in range(4)]
    assert list(session._participant_tickets) == [t["ticketId"] for t in tickets]
    newest = session.preview_task_status("running")
    assert list(session._participant_tickets) == (
        [t["ticketId"] for t in tickets[1:]] + [newest["ticketId"]])
    with pytest.raises(ParticipantCommandError) as caught:
        session.confirm_task_status(tickets[0]["ticketId"])
    assert caught.value.code == "expired"


def test_ttl_prune_bounds_the_registry_without_a_new_review(bound):
    """A 300 s TTL prunes every aged ticket; a fresh one survives the prune."""
    _, session = bound
    old = session.preview_task_status("waiting")
    for key, (dto, created, record, generation) in tuple(session._participant_tickets.items()):
        session._participant_tickets[key] = (dto, created - 400, record, generation)
    fresh = session.preview_task_status("running")
    assert old["ticketId"] not in session._participant_tickets
    assert list(session._participant_tickets) == [fresh["ticketId"]]
    with pytest.raises(ParticipantCommandError) as caught:
        session.confirm_task_status(old["ticketId"])
    assert caught.value.code == "expired"
    assert session.confirm_task_status(fresh["ticketId"])["status"] == "running"


# ----------------------------------------------------- 5. the real run lease


def _install_idle_run(session, workspace, env, monkeypatch, *, worker=None, coordinator=None,
                      engine="pipeline", proposals=()):
    """A waiting-user pipeline run around a REAL collaboration session."""
    monkeypatch.setitem(run_api._active, "run_id", RUN_ID)
    monkeypatch.setitem(run_api._active, "engine", engine)
    monkeypatch.setitem(run_api._active, "collab_session", session)
    monkeypatch.setitem(run_api._active, "workspace", workspace)
    monkeypatch.setitem(run_api._active, "worker", _idle_worker() if worker is None else worker)
    monkeypatch.setitem(run_api._active, "coordinator",
                        _coordinator() if coordinator is None else coordinator)
    monkeypatch.setitem(run_api._active, "proposals", list(proposals))
    monkeypatch.setattr(state, "get_project", lambda: SimpleNamespace(root=str(env.source)))
    monkeypatch.setattr(state, "project_generation", lambda: 7)


def test_real_participant_lease_needs_an_idle_accepted_waiting_user_run(
        accepted, env, monkeypatch):
    """The real ``borrow_participant_context`` gate, not a stubbed one."""
    _, session = accepted
    _install_idle_run(session, env.workspace, env, monkeypatch, proposals=[{"path": "a.txt"}])

    with run_api.borrow_participant_context(RUN_ID) as borrowed:
        assert borrowed["session"] is session
        assert borrowed["workspace"] is env.workspace
        assert borrowed["project_root"] == env.source.resolve()
        assert borrowed["generation"] == 7 and borrowed["run_id"] == RUN_ID
        assert borrowed["available_paths"] == ("a.txt",)
        assert borrowed["binding"].to_dict() == borrowed["binding_data"]
        assert borrowed["binding"].to_dict() == session.accepted_binding.to_dict()
        assert run_api._delivery_is_busy(session, RUN_ID)
        assert run_api._delivery_leases[(id(session), RUN_ID)] == 1
    assert not run_api._delivery_is_busy(session, RUN_ID)
    assert run_api._delivery_leases == {}

    # A live worker, a non-WAITING_USER run and a non-pipeline engine are refused.
    monkeypatch.setitem(run_api._active, "worker", _idle_worker(finished=False))
    with pytest.raises(RuntimeError, match="busy"):
        with run_api.borrow_participant_context(RUN_ID):
            pass
    monkeypatch.setitem(run_api._active, "worker", _idle_worker())
    monkeypatch.setitem(run_api._active, "coordinator", _coordinator(RunStatus.RUNNING))
    with pytest.raises(RuntimeError, match="busy"):
        with run_api.borrow_participant_context(RUN_ID):
            pass
    monkeypatch.setitem(run_api._active, "coordinator", _coordinator())
    monkeypatch.setitem(run_api._active, "engine", "legacy")
    with pytest.raises(RuntimeError, match="invalid"):
        with run_api.borrow_participant_context(RUN_ID):
            pass
    monkeypatch.setitem(run_api._active, "engine", "pipeline")
    with pytest.raises(RuntimeError, match="invalid"):
        with run_api.borrow_participant_context("some-other-run"):
            pass
    # A session without a real accepted binding cannot be leased either.
    monkeypatch.setattr(session, "_accepted_binding", None)
    with pytest.raises(RuntimeError, match="invalid"):
        with run_api.borrow_participant_context(RUN_ID):
            pass
    assert run_api._delivery_leases == {} and not run_api._delivery_closing
    assert session._participant_tickets == {}


def test_borrowed_generation_or_root_drift_is_stale_before_any_io(
        accepted, env, monkeypatch, tmp_path):
    """A lease captured against an older generation is refused before yielding."""
    _, session = accepted
    _install_idle_run(session, env.workspace, env, monkeypatch)
    counter = {"n": 0}

    def drifting_generation():
        counter["n"] += 1
        return 7 if counter["n"] == 1 else 8

    monkeypatch.setattr(state, "project_generation", drifting_generation)
    with pytest.raises(RuntimeError, match="stale"):
        with run_api.borrow_participant_context(RUN_ID):
            pytest.fail("a stale generation must never yield")
    assert session._participant_tickets == {}

    # A different selected project is refused the same way, before any yield.
    other = tmp_path / "other-project"
    other.mkdir()
    monkeypatch.setattr(state, "get_project", lambda: SimpleNamespace(root=str(other)))
    with pytest.raises(RuntimeError, match="stale"):
        with run_api.borrow_participant_context(RUN_ID):
            pytest.fail("a foreign project must never yield")
    # A missing project is refused at capture time, and every lease comes back.
    monkeypatch.setattr(state, "get_project", lambda: None)
    with pytest.raises(RuntimeError, match="invalid"):
        with run_api.borrow_participant_context(RUN_ID):
            pytest.fail("a missing project must never yield")
    assert run_api._delivery_leases == {} and not run_api._delivery_closing
    assert env.store.fetch_state()[1].tasks[TASK_ID].status == "running"


def test_participant_lease_accepts_a_symlinked_project_root(accepted, env, monkeypatch, tmp_path):
    """A ``str``/``Path`` root and a symlink alias are the same canonical root."""
    _, session = accepted
    _install_idle_run(session, env.workspace, env, monkeypatch)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(env.source, target_is_directory=True)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM):
            raise
        pytest.skip("symlink creation requires platform permission")
    monkeypatch.setattr(state, "get_project", lambda: SimpleNamespace(root=alias))
    with run_api.borrow_participant_context(RUN_ID) as borrowed:
        assert borrowed["project_root"] == env.source.resolve()


# ------------------------------------------ 6. delivery lease vs run mutation


def test_delivery_lease_blocks_apply_reject_follow_up_and_start(accepted, env, monkeypatch):
    """No run mutation, retirement or restart while a delivery lease is held."""
    _, session = accepted
    workspace = env.workspace
    _install_idle_run(session, workspace, env, monkeypatch, proposals=[{"path": "a.txt"}])
    ctx = SimpleNamespace(_bridge=SimpleNamespace(emit_event=lambda *a, **k: None))
    hits = []
    monkeypatch.setattr(run_api, "_stale_apply_conflicts", lambda *a: hits.append("filesystem"))

    with run_api.borrow_delivery_context(RUN_ID) as borrowed:
        assert borrowed["session"] is session
        for handler in (run_api._apply, run_api._reject):
            with pytest.raises(BridgeError) as caught:
                params = {"paths": ["a.txt"]} if handler is run_api._apply else {}
                handler(params, ctx)
            assert caught.value.code == "busy"
        assert hits == [], "a refused apply must not touch the filesystem"
        assert run_api._active["proposals"] == [{"path": "a.txt"}]

        with pytest.raises(BridgeError) as follow_up:
            run_api._follow_up({"feedback": "devam edelim"}, SimpleNamespace())
        assert follow_up.value.code == "busy"

        with pytest.raises(BridgeError) as start:
            run_api._start({"task": "yeni görev"}, SimpleNamespace())
        assert start.value.code == "busy"
        assert (env.source / "a.txt").read_text(encoding="utf-8") == "buggy\n"
        assert session.active is False and session.accepted_binding is not None

    # Once released, nothing was lost: the accepted binding still reads back.
    assert not run_api._delivery_is_busy(session, RUN_ID)
    assert session.accepted_binding.to_dict() == borrowed["binding_data"]


def test_the_drain_waits_for_the_lease_and_retires_on_release(accepted, env, monkeypatch):
    """A retained session/workspace is not disposed under a live delivery lease."""
    _, session = accepted
    workspace = env.workspace
    _install_idle_run(session, workspace, env, monkeypatch)
    disposals = []
    real_dispose = workspace.dispose
    monkeypatch.setattr(workspace, "dispose",
                        lambda: (disposals.append("dispose"), real_dispose())[1])
    run_api._retain_collaboration(session, workspace, RUN_ID)

    with run_api.borrow_delivery_context(RUN_ID):
        run_api._drain_collaboration_resources()
        assert run_api._draining_workers, "the retained resources are still owned"
        assert disposals == [] and session.active is False
        assert run_api._active["collab_session"] is session

    # Releasing the lease is the last trigger the drain needed.
    assert run_api._draining_workers == []
    assert disposals == ["dispose"]
    assert run_api._active["collab_session"] is None
    assert run_api._active["workspace"] is None
    cached = state.get_collaboration_status_cache(RUN_ID)
    assert cached["state"] == "closed" and cached["taskId"] == TASK_ID
    with pytest.raises(ParticipantCommandError):
        session.preview_task_status("waiting")  # the session really was closed


# ------------------------------------------------------ 7. the three RPCs


class _TicketSession:
    """A minimal stand-in that RECORDS every participant call.

    Deliberately not a ticket ledger: spend-once is a host contract proven with
    the real session above. What matters here is that the RPC layer refuses
    before it calls anything, and that it never retries or reports uncertainty
    it does not have.
    """

    run_id = RUN_ID

    def __init__(self, root):
        self.project_root = root
        self.calls = []
        self.discarded = []
        self.confirm_error = None

    def preview_task_status(self, target):
        self.calls.append(("preview", target))
        return {"ticketId": "ticket-private", "runId": self.run_id,
                "projectRoot": str(self.project_root), "sessionId": "session-1",
                "taskId": "task-1", "memberId": "member-1", "fromStatus": "running",
                "targetStatus": target, "expectedRevision": "revision-1",
                "contextHash": "hash-1", "freshContextDiffersFromAccepted": None}

    def confirm_task_status(self, ticket):
        self.calls.append(("confirm", ticket))
        if self.confirm_error is not None:
            raise self.confirm_error
        return {"revision": "revision-2", "taskId": "task-1", "status": "waiting"}

    def discard_task_status(self, ticket):
        self.calls.append(("discard", ticket))
        self.discarded.append(ticket)


@pytest.fixture
def rpc_world(tmp_path, monkeypatch, qapp):
    """A leased participant RPC world with a scripted run lease and project.

    The lease yields a SNAPSHOT (as the real code does) and never validates the
    session identity itself, so the bridge's own identity checks are exercised.
    ``world.drift`` models a project generation that moves between the RPC's
    capture and the lease's capture.
    """
    root = tmp_path.resolve()
    session = _TicketSession(root)
    generation = {"value": 41}
    drift = {"n": 0}

    @contextmanager
    def borrow(run_id):
        if run_id != RUN_ID:
            raise RuntimeError("invalid")
        yield {"session": session, "workspace": SimpleNamespace(root=str(root)),
               "project_root": root, "generation": generation["value"] + drift["n"],
               "run_id": run_id, "binding": object(), "binding_data": {},
               "available_paths": ()}

    monkeypatch.setattr(state, "get_project", lambda: SimpleNamespace(root=str(root)))
    monkeypatch.setattr(state, "project_generation", lambda: generation["value"])
    monkeypatch.setattr(run_api, "borrow_participant_context", borrow)
    monkeypatch.setattr(run_api, "_active", {"run_id": session.run_id,
                                             "collab_session": session})
    return SimpleNamespace(app=qapp, root=root, session=session, generation=generation,
                           drift=drift)


def _pump(app):
    app.processEvents()
    threading.Event().wait(.005)


def _call(app, method, params):
    bridge = HostBridge()
    replies = []
    bridge.reply.connect(lambda raw: replies.append(json.loads(raw)))
    bridge.call(json.dumps({"id": 1, "method": method, "params": params}))
    deadline = time.monotonic() + 5
    while not replies and time.monotonic() < deadline:
        _pump(app)
    assert replies, f"no reply for {method}"
    return replies[0]


@pytest.mark.parametrize("method,params", [
    ("collab.taskStatus.preview", {"runId": RUN_ID, "targetStatus": "done"}),
    ("collab.taskStatus.preview", {"runId": RUN_ID}),
    ("collab.taskStatus.preview", {"runId": "", "targetStatus": "waiting"}),
    ("collab.taskStatus.preview", {"runId": RUN_ID, "targetStatus": "waiting", "root": "/x"}),
    ("collab.taskStatus.confirm", {"runId": RUN_ID, "ticketId": "t", "confirm": False}),
    ("collab.taskStatus.confirm", {"runId": RUN_ID, "ticketId": "t", "confirm": "true"}),
    ("collab.taskStatus.confirm", {"runId": RUN_ID, "ticketId": "", "confirm": True}),
    ("collab.taskStatus.confirm", {"runId": RUN_ID, "ticketId": "t"}),
    ("collab.taskStatus.discard", {"runId": RUN_ID}),
    ("collab.taskStatus.discard", {"runId": RUN_ID, "ticketId": 7}),
    ("collab.taskStatus.discard", {"runId": RUN_ID, "ticketId": "t", "extra": True}),
])
def test_all_three_rpcs_reject_malformed_shapes_before_touching_the_lease(
        rpc_world, method, params):
    reply = _call(rpc_world.app, method, params)
    assert not reply["ok"] and reply["error"]["code"] == "collab_invalid"
    assert rpc_world.session.calls == []


def test_drifted_lease_generation_is_refused_before_any_session_call(rpc_world):
    """The bridge re-checks the captured generation before running the command."""
    app, session = rpc_world.app, rpc_world.session
    rpc_world.drift["n"] = 1  # the project moved after the RPC captured it
    preview = _call(app, "collab.taskStatus.preview",
                    {"runId": RUN_ID, "targetStatus": "waiting"})
    assert not preview["ok"] and preview["error"]["code"] == "collab_stale"
    assert session.calls == [] and session.discarded == []

    confirm = _call(app, "collab.taskStatus.confirm",
                    {"runId": RUN_ID, "ticketId": "t", "confirm": True})
    assert not confirm["ok"] and confirm["error"]["code"] == "collab_stale"
    assert session.calls == []
    assert collab_api._SLOTS._value == 2


def test_session_identity_mismatch_is_refused_before_any_session_call(rpc_world, monkeypatch):
    """A lease whose session is not this run may never issue a command."""
    app, session = rpc_world.app, rpc_world.session
    monkeypatch.setattr(session, "run_id", "another-run")
    reply = _call(app, "collab.taskStatus.preview",
                  {"runId": RUN_ID, "targetStatus": "waiting"})
    assert not reply["ok"] and reply["error"]["code"] == "collab_stale"
    assert session.calls == []

    monkeypatch.setattr(session, "run_id", RUN_ID)
    monkeypatch.setattr(session, "project_root", rpc_world.root.parent)
    reply = _call(app, "collab.taskStatus.preview",
                  {"runId": RUN_ID, "targetStatus": "waiting"})
    assert not reply["ok"] and reply["error"]["code"] == "collab_stale"
    assert session.calls == []


def test_unknown_caller_denied_before_io_and_slots_never_leak(rpc_world, monkeypatch):
    """A foreign run id is refused, and refusals return every semaphore slot."""
    app, session = rpc_world.app, rpc_world.session

    @contextmanager
    def foreign(run_id):
        raise RuntimeError("invalid")
        yield  # pragma: no cover - generator shape only

    monkeypatch.setattr(run_api, "borrow_participant_context", foreign)
    denied = _call(app, "collab.taskStatus.preview",
                   {"runId": "another-run", "targetStatus": "waiting"})
    assert not denied["ok"] and denied["error"]["code"] == "collab_invalid"
    assert session.calls == []

    # A missing project is refused synchronously, before any slot is taken.
    monkeypatch.setattr(state, "get_project", lambda: None)
    for _ in range(25):
        missing = _call(app, "collab.taskStatus.preview",
                        {"runId": RUN_ID, "targetStatus": "waiting"})
        assert not missing["ok"] and missing["error"]["code"] == "no_project"
    assert collab_api._SLOTS._value == 2, "a refused request must return its slot"

    # An unusable project root is refused before the slot is taken as well.
    monkeypatch.setattr(state, "get_project",
                        lambda: SimpleNamespace(root=str(rpc_world.root / "gone")))
    unusable = _call(app, "collab.taskStatus.preview",
                     {"runId": RUN_ID, "targetStatus": "waiting"})
    assert not unusable["ok"] and unusable["error"]["code"] == "collab_invalid"
    assert collab_api._SLOTS._value == 2
    assert session.calls == []


def test_saturated_participant_slots_are_bounded_and_released(rpc_world, monkeypatch):
    """At most two participant commands are in flight; both slots come back."""
    app, session = rpc_world.app, rpc_world.session
    entered = threading.Semaphore(0)
    release = threading.Event()
    original = session.preview_task_status

    def gated(target):
        entered.release()
        assert release.wait(5)
        return original(target)

    monkeypatch.setattr(session, "preview_task_status", gated)
    replies, bridges = [], []
    for _ in range(2):
        bridge = HostBridge()
        got = []
        # A default argument binds this bridge's own sink: a closure over the
        # loop variable would send every reply to the last bridge.
        bridge.reply.connect(lambda raw, sink=got: sink.append(json.loads(raw)))
        bridge.call(json.dumps({"id": 1, "method": "collab.taskStatus.preview",
                                "params": {"runId": RUN_ID, "targetStatus": "waiting"}}))
        bridges.append(bridge)
        replies.append(got)
    assert entered.acquire(timeout=5) and entered.acquire(timeout=5)
    # The third request is refused by the bounded semaphore, not queued.
    saturated = _call(app, "collab.taskStatus.preview",
                      {"runId": RUN_ID, "targetStatus": "waiting"})
    assert not saturated["ok"] and saturated["error"]["code"] == "collab_busy"
    release.set()
    deadline = time.monotonic() + 5
    while not all(replies) and time.monotonic() < deadline:
        _pump(app)
    assert all(reply[0]["ok"] for reply in replies)
    assert bridges  # the deferred replies were delivered, not collected away
    assert collab_api._SLOTS._value == 2


def test_discard_rpc_only_touches_the_matching_active_run(rpc_world, monkeypatch):
    """Discard is zero-I/O, idempotent and scoped to the live run."""
    app, session = rpc_world.app, rpc_world.session

    first = _call(app, "collab.taskStatus.discard", {"runId": RUN_ID, "ticketId": "t1"})
    assert first["ok"] and first["result"] == {}
    assert session.discarded == ["t1"]
    assert _call(app, "collab.taskStatus.discard",
                 {"runId": RUN_ID, "ticketId": "t1"})["result"] == {}
    assert session.discarded == ["t1", "t1"]

    # A run that is no longer active keeps its tickets owned by the closed host.
    monkeypatch.setitem(run_api._active, "run_id", "other-run")
    stale = _call(app, "collab.taskStatus.discard", {"runId": RUN_ID, "ticketId": "t2"})
    assert stale["ok"] and stale["result"] == {}
    assert session.discarded == ["t1", "t1"]

    # A lease failure is swallowed: a discard never becomes an error surface.
    monkeypatch.setitem(run_api._active, "run_id", RUN_ID)

    @contextmanager
    def failing(run_id):
        raise RuntimeError("busy")
        yield  # pragma: no cover - generator shape only

    monkeypatch.setattr(run_api, "borrow_participant_context", failing)
    assert _call(app, "collab.taskStatus.discard",
                 {"runId": RUN_ID, "ticketId": "t3"})["result"] == {}
    assert session.discarded == ["t1", "t1"]


@pytest.mark.parametrize("code,uncertain", [
    ("invalid", False), ("stale", False), ("busy", False), ("expired", False),
    ("access_denied", False), ("stale_revision", False), ("invalid_request", False),
    ("session_unavailable", False), ("connection_error", False),
    ("server_error", True), ("protocol_error", True), ("outcome_unknown", True),
])
def test_participant_error_surface_is_safe_and_uncertainty_preserving(
        rpc_world, code, uncertain):
    """Every failure is a sanitized local message; uncertainty is never hidden."""
    app, session = rpc_world.app, rpc_world.session
    session.confirm_error = ParticipantCommandError(code, outcome_uncertain=uncertain)
    reply = _call(app, "collab.taskStatus.confirm",
                  {"runId": RUN_ID, "ticketId": "t", "confirm": True})
    assert not reply["ok"]
    error = reply["error"]
    assert error["message"].strip()
    if uncertain:
        assert error["code"] == "collab_task_outcome_unknown"
    else:
        assert error["code"] in {"collab_busy", "collab_stale", "collab_invalid",
                                 "collab_unavailable"}
    for exact in (("busy", "collab_busy"), ("stale", "collab_stale"), ("invalid", "collab_invalid")):
        if code == exact[0] and not uncertain:
            assert error["code"] == exact[1]
    # Nothing private, local or infrastructural leaks through the local UI error.
    blob = json.dumps(error, ensure_ascii=False)
    for secret in (session.run_id, str(rpc_world.root), "ticket-private", "Traceback",
                   "session-1", "task-1"):
        assert secret not in blob
    assert session.calls == [("confirm", "t")]


def test_only_a_started_confirmation_may_be_reported_as_unknown(rpc_world, monkeypatch):
    """A preview failure is never 'unknown'; a confirm failure always is."""
    app, session = rpc_world.app, rpc_world.session

    def boom(target):
        raise RuntimeError("internal detail that must not leak")

    monkeypatch.setattr(session, "preview_task_status", boom)
    preview = _call(app, "collab.taskStatus.preview",
                    {"runId": RUN_ID, "targetStatus": "waiting"})
    assert not preview["ok"] and preview["error"]["code"] == "collab_unavailable"
    assert "internal detail" not in preview["error"]["message"]

    session.confirm_error = RuntimeError("internal detail")
    confirm = _call(app, "collab.taskStatus.confirm",
                    {"runId": RUN_ID, "ticketId": "t", "confirm": True})
    assert not confirm["ok"] and confirm["error"]["code"] == "collab_task_outcome_unknown"
    assert "internal detail" not in confirm["error"]["message"]
    # Exactly one attempt: the RPC layer never retries a command by itself.
    assert session.calls == [("confirm", "t")]
