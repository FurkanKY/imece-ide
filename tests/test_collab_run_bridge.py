import json
import subprocess
import threading
from pathlib import Path

import pytest
pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication

import engine_factory
import ui_prefs
import webhost.api.run as run_api
from collab_runtime.coordinator import Snapshot
from collab_runtime.host import CollaborationHost, HostCollaborationError
from collab_runtime.models import SessionState, SharedContext, Task
from run_runtime import RunRuntime, RunStore
from webhost import state
from webhost.bridge import HostBridge


@pytest.fixture(scope="session")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def setup_run(tmp_path, monkeypatch, qapp):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "file.txt").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    monkeypatch.setattr(engine_factory, "workspaces_dir", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(ui_prefs, "load", lambda: {**ui_prefs.DEFAULTS, "ai_engine": "auto"})
    state.set_project(str(repo))
    state.set_run_runtime(RunRuntime(RunStore(tmp_path / "runs.sqlite3")))
    run_api._active.update({"worker": None, "coordinator": None, "run_id": None,
        "proposals": [], "engine": "legacy", "workspace": None,
        "cancel_event": None, "collab_session": None})
    yield repo
    state._active = None
    state.set_collaboration_host(None)
    state.set_collaboration_status_cache(None)
    state.set_run_runtime(None)


def rpc(bridge, method, params):
    replies = []
    bridge.reply.connect(lambda raw: replies.append(json.loads(raw)))
    bridge.call(json.dumps({"id": 1, "method": method, "params": params}))
    assert replies
    return replies[0]


def test_collab_run_handle_shape_and_legacy_preflight(setup_run, monkeypatch):
    bridge = HostBridge()
    bad = rpc(bridge, "run.start", {"task": "test", "collabApprovalHandle": ""})
    assert not bad["ok"] and bad["error"]["code"] == "collab_invalid"

    monkeypatch.setattr(engine_factory, "select_engine",
                        lambda *a, **kw: engine_factory.EngineSelection("legacy"))
    unsupported = rpc(bridge, "run.start", {
        "task": "test", "collabApprovalHandle": "approval-token"})
    assert not unsupported["ok"]
    assert unsupported["error"]["code"] == "collab_unsupported"
    assert run_api._active["worker"] is None
    assert run_api._active["run_id"] is None


def test_approved_run_binds_native_safe_point_without_legacy_fallback(setup_run, monkeypatch):
    class Session:
        safe_point = object()

        def __init__(self):
            self.root = setup_run

    session = Session()

    class Host:
        def bind_run(self, handle, root, run_id):
            assert handle == "approved-handle"
            assert Path(root) == setup_run
            assert run_id
            return session

    class Worker:
        def isRunning(self):
            return False

    seen = {}
    state.set_collaboration_host(Host())
    monkeypatch.setattr(engine_factory, "select_engine",
        lambda *a, **kw: engine_factory.EngineSelection("pipeline"))
    monkeypatch.setattr(engine_factory, "create_pipeline_workspace", lambda *_a: object())
    def ports(*args, **kwargs):
        seen["safe_point"] = kwargs.get("worker_safe_point")
        return object()
    monkeypatch.setattr(engine_factory, "build_pipeline_ports", ports)
    monkeypatch.setattr(engine_factory, "build_verification_failure_gate", lambda *_a, **_kw: None)
    def start_pipeline(*_args, **kwargs):
        seen["session"] = kwargs.get("collab_session")
        return Worker()
    monkeypatch.setattr(run_api, "_start_pipeline_run", start_pipeline)
    reply = rpc(HostBridge(), "run.start", {
        "task": "approved task", "routing": {"coder": "deepseek"},
        "collabApprovalHandle": "approved-handle"})
    assert reply["ok"]
    assert seen["safe_point"] is session.safe_point
    assert seen["session"] is session
    assert run_api._active["collab_session"] is session


class _Signal:
    def __init__(self):
        self.values = []

    def emit(self, value):
        self.values.append(value)


class _Session:
    def __init__(self, log, *, fail_close=False):
        self.log, self.fail_close = log, fail_close

    def activate(self, workspace, *, cancel_token=None):
        self.log.append("activate")

    def deactivate(self):
        self.log.append("deactivate")
        if self.fail_close:
            raise RuntimeError("secret should not leak")


class _Worker:
    run_id = "run-1"
    workspace = object()
    cancel_event = threading.Event()

    def __init__(self, session):
        self.collab_session = session
        self.finished_ok = _Signal()
        self.failed = _Signal()


def test_worker_execution_brackets_pipeline_with_collaboration_activation(qapp):
    log = []
    worker = _Worker(_Session(log))
    run_api._execute_with_collaboration(worker, lambda: (log.append("model"), "report")[1])
    assert log == ["activate", "model", "deactivate"]
    assert worker.finished_ok.values == ["report"]
    assert not worker.failed.values


def test_worker_cleanup_error_never_emits_success_or_secret(qapp):
    log = []
    worker = _Worker(_Session(log, fail_close=True))
    run_api._execute_with_collaboration(worker, lambda: "report")
    assert log == ["activate", "deactivate"]
    assert not worker.finished_ok.values
    assert worker.failed.values == ["collab_cleanup_failed"]


def test_idle_reapproval_updates_only_same_approved_task(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    head = "a" * 40

    class Client:
        def __init__(self, snapshot):
            self.value = snapshot

        def snapshot(self):
            return self.value

    def snapshot(revision, goal="same goal"):
        task = Task("task-1", "member-1", goal, ("src/",), "running", revision)
        state_value = SessionState("session-1", "v1", head,
            SharedContext("context", (), ()), {task.id: task})
        return Snapshot(revision, state_value)

    latest = [snapshot("b" * 40)]
    host = CollaborationHost(tmp_path / "cursor", head_reader=lambda _root: head,
        client_factory=lambda *_a, **_kw: Client(latest[0]))
    first = host.preview(root, "http://127.0.0.1:1234", "secret-value", "member-1", "task-1")
    initial_approval = host.approve(first["previewId"], root)
    session = host.bind_run(initial_approval["approvalHandle"], root, "run-1")
    latest[0] = snapshot("c" * 40)
    replacement = host.preview(root, "http://127.0.0.1:1234", "new-secret", "member-1", "task-1")
    approval = host.approve(replacement["previewId"], root, reset_cursor=True)
    session.reapprove(approval["approvalHandle"])
    assert session.status()["consumedRevision"] == "b" * 40
    assert session.status()["receivedRevision"] == "b" * 40
    assert session.status()["taskId"] == "task-1"

    latest[0] = snapshot("d" * 40, goal="retargeted goal")
    different = host.preview(root, "http://127.0.0.1:1234", "third-secret", "member-1", "task-1")
    different_approval = host.approve(different["previewId"], root)
    with pytest.raises(HostCollaborationError):
        session.reapprove(different_approval["approvalHandle"])
    assert session.status()["consumedRevision"] == "b" * 40


@pytest.mark.parametrize("handler_name", ["_apply", "_reject"])
def test_apply_and_reject_busy_guard_precedes_proposal_or_canonical_mutation(
        setup_run, monkeypatch, handler_name):
    class Session:
        project_root = setup_run.resolve()

    class Worker:
        def isRunning(self):
            return True

    proposals = [{"path": "file.txt", "new": "changed\n"}]
    run_api._active.update({"collab_session": Session(), "worker": Worker(),
        "engine": "pipeline", "proposals": proposals})
    calls = []
    monkeypatch.setattr(run_api, "_stale_apply_conflicts", lambda *_a: calls.append("filesystem"))
    with pytest.raises(Exception) as caught:
        params = {"paths": ["file.txt"]} if handler_name == "_apply" else {}
        getattr(run_api, handler_name)(params, object())
    assert getattr(caught.value, "code", None) == "busy"
    assert run_api._active["proposals"] == proposals
    assert calls == []


def test_terminal_collaboration_status_survives_session_release(setup_run):
    class Session:
        project_root = setup_run.resolve()

        def status(self):
            return {"state": "inactive", "code": None, "consumedRevision": "b" * 40,
                "receivedRevision": "c" * 40, "pendingCount": 1, "sessionId": "s",
                "taskId": "t", "memberId": "m", "active": False}

    session = Session()
    run_api._active.update({"run_id": "run-status", "collab_session": session})
    run_api._cache_collaboration_status(session, "run-status", "closed", "run_cancelled")
    run_api._active["collab_session"] = None
    status = run_api.get_collaboration_status("run-status")
    assert status["state"] == "closed" and status["code"] == "run_cancelled"
    assert status["consumedRevision"] == "b" * 40
