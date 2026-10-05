"""RPC boundary tests for own-task status preview and one-shot confirmation."""
import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication

from collab_runtime.host import ParticipantCommandError
from webhost import state
from webhost.api import collab as collab_api
from webhost.api import run as run_api
from webhost.bridge import HostBridge, handler


@pytest.fixture(scope="session")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def bridge_world(monkeypatch, tmp_path, qapp):
    # Other bridge tests may temporarily replace the process-global registry.
    # Restore only this module's participant handlers; don't clear/reset the
    # registry or depend on test/module import order.
    for method, function in (
        ("collab.taskStatus.preview", collab_api._participant_preview),
        ("collab.taskStatus.confirm", collab_api._participant_confirm),
        ("collab.taskStatus.discard", collab_api._participant_discard),
    ):
        handler(method)(function)
    app = qapp
    root = tmp_path.resolve()
    project = SimpleNamespace(root=str(root))
    generation = {"value": 41}
    monkeypatch.setattr(state, "get_project", lambda: project)
    monkeypatch.setattr(state, "project_generation", lambda: generation["value"])

    class Session:
        run_id = "participant-run"
        project_root = root
        calls = 0
        discarded = []
        mutate_preview_generation = False
        mutate_confirm_generation = False

        def preview_task_status(self, target):
            result = {"ticketId": "ticket-private", "runId": self.run_id,
                    "projectRoot": str(root), "sessionId": "session-1", "taskId": "task-1",
                    "memberId": "member-1", "fromStatus": "running", "targetStatus": target,
                    "expectedRevision": "revision-1", "contextHash": "hash-1",
                    "freshContextDiffersFromAccepted": None}
            if self.mutate_preview_generation:
                generation["value"] += 1
            return result

        def confirm_task_status(self, ticket):
            self.calls += 1
            if ticket == "uncertain":
                raise ParticipantCommandError("outcome_unknown", outcome_uncertain=True)
            if self.mutate_confirm_generation:
                generation["value"] += 1
            return {"revision": "revision-2", "taskId": "task-1", "status": "waiting"}

        def discard_task_status(self, ticket):
            self.discarded.append(ticket)

    session = Session()
    borrowed = {"session": session, "project_root": root, "generation": generation["value"]}

    @contextmanager
    def borrow(run_id):
        if run_id != session.run_id:
            raise RuntimeError("invalid")
        borrowed["generation"] = generation["value"]
        yield borrowed

    monkeypatch.setattr(run_api, "borrow_participant_context", borrow)
    monkeypatch.setattr(run_api, "_active", {"run_id": session.run_id, "collab_session": session})
    yield app, root, session


def _call(app, method, params):
    bridge = HostBridge()
    replies = []
    bridge.reply.connect(lambda raw: replies.append(json.loads(raw)))
    bridge.call(json.dumps({"id": 1, "method": method, "params": params}))
    deadline = time.monotonic() + 5
    while not replies and time.monotonic() < deadline:
        app.processEvents()
        threading.Event().wait(.005)
    assert replies
    return replies[0]


def test_preview_confirm_rpc_uses_only_pinned_run_and_returns_receipt(bridge_world):
    app, _, session = bridge_world
    preview = _call(app, "collab.taskStatus.preview", {
        "runId": session.run_id, "targetStatus": "waiting"})
    assert preview["ok"] and preview["result"]["taskId"] == "task-1"
    receipt = _call(app, "collab.taskStatus.confirm", {
        "runId": session.run_id, "ticketId": "ticket-private", "confirm": True})
    assert receipt["ok"] and receipt["result"] == {
        "revision": "revision-2", "taskId": "task-1", "status": "waiting"}
    assert session.calls == 1


@pytest.mark.parametrize("params", [
    {"runId": "participant-run", "targetStatus": "waiting", "root": "/elsewhere"},
    {"runId": "participant-run", "targetStatus": "done"},
])
def test_preview_rejects_retargeting_and_unknown_fields(bridge_world, params):
    app, _, _ = bridge_world
    result = _call(app, "collab.taskStatus.preview", params)
    assert not result["ok"] and result["error"]["code"] == "collab_invalid"


def test_confirmation_requires_true_and_preserves_uncertainty_contract(bridge_world):
    app, _, session = bridge_world
    malformed = _call(app, "collab.taskStatus.confirm", {
        "runId": session.run_id, "ticketId": "ticket-private", "confirm": 1})
    assert not malformed["ok"] and malformed["error"]["code"] == "collab_invalid"
    uncertain = _call(app, "collab.taskStatus.confirm", {
        "runId": session.run_id, "ticketId": "uncertain", "confirm": True})
    assert not uncertain["ok"]
    assert uncertain["error"]["code"] == "collab_task_outcome_unknown"
    assert "YAPILMIŞ OLABİLİR" in uncertain["error"]["message"]
    assert "yeniden deneme yapılmadı" in uncertain["error"]["message"]


def test_string_project_root_matches_leased_path_and_missing_project_does_not_leak_slots(bridge_world, monkeypatch):
    app, root, session = bridge_world
    monkeypatch.setattr(state, "get_project", lambda: None)
    for _ in range(2):
        missing = _call(app, "collab.taskStatus.preview", {
            "runId": session.run_id, "targetStatus": "waiting"})
        assert not missing["ok"] and missing["error"]["code"] == "no_project"

    monkeypatch.setattr(state, "get_project", lambda: SimpleNamespace(root=str(root)))
    result = _call(app, "collab.taskStatus.preview", {
        "runId": session.run_id, "targetStatus": "waiting"})
    assert result["ok"] and result["result"]["projectRoot"] == str(root)


def test_preview_drops_ticket_if_project_generation_changes_and_confirm_receipt_stays_truthful(bridge_world):
    app, _, session = bridge_world
    session.mutate_preview_generation = True
    stale = _call(app, "collab.taskStatus.preview", {
        "runId": session.run_id, "targetStatus": "waiting"})
    assert not stale["ok"] and stale["error"]["code"] == "collab_stale"
    assert session.discarded == ["ticket-private"]

    session.mutate_preview_generation = False
    session.mutate_confirm_generation = True
    receipt = _call(app, "collab.taskStatus.confirm", {
        "runId": session.run_id, "ticketId": "ticket-private", "confirm": True})
    assert receipt["ok"] and receipt["result"]["revision"] == "revision-2"
