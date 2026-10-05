from pathlib import Path
from types import SimpleNamespace

import pytest

import webhost.api.run as run_api
from webhost import state
from collab_runtime.host import HostCollaborationError, _CheckpointLease


@pytest.fixture
def project_state(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "file.txt").write_text("before\n")
    state.set_project(str(root))
    run_api._draining_workers.clear()
    run_api._active.update({
        "worker": None, "coordinator": None, "run_id": None, "proposals": [],
        "engine": "pipeline", "workspace": None, "collab_session": None,
    })
    state.set_collaboration_status_cache(None)
    yield root
    run_api._draining_workers.clear()
    state.set_collaboration_status_cache(None)
    state._active = None


class _Session:
    def __init__(self, root, status=None, failures=0):
        self.project_root = Path(root).resolve()
        self._status = status or {"state": "active", "code": "ok", "consumed": 4,
                                  "received": 7, "pending": 2}
        self.failures = failures
        self.close_count = 0

    def status(self):
        return dict(self._status)

    def close(self):
        self.close_count += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("private cleanup detail")


def test_terminal_session_state_survives_normal_close(project_state):
    for terminal in ("access_denied", "resnapshot_required", "protocol_error", "server_error"):
        session = _Session(project_state, {"state": terminal, "code": terminal})
        run_api._active.update(run_id=terminal, collab_session=session)
        run_api._cache_collaboration_status(session, terminal, "closed", "run_finished")
        cached = state.get_collaboration_status_cache(terminal)
        assert (cached["state"], cached["code"]) == (terminal, terminal)


def test_terminal_reason_survives_cleanup_retry(project_state):
    session = _Session(project_state, {"state": "access_denied", "code": "access_denied"}, failures=1)
    run_id = "terminal-retry"
    run_api._active.update(run_id=run_id, collab_session=session)
    run_api._retain_collaboration(session, None, run_id)
    run_api._drain_collaboration_resources()
    cached = state.get_collaboration_status_cache(run_id)
    assert cached["state"] == "cleanup_failed"
    assert cached["recoveryState"] == "access_denied"
    run_api._drain_collaboration_resources()
    cached = state.get_collaboration_status_cache(run_id)
    assert (cached["state"], cached["code"]) == ("access_denied", "access_denied")


@pytest.mark.parametrize("terminal_code", ["run_cancelled", "run_shutdown", "run_failed"])
def test_drain_preserves_retirement_terminal_code(project_state, terminal_code):
    session = _Session(project_state)
    run_id = "retire-" + terminal_code
    run_api._active.update(run_id=run_id, collab_session=session)
    run_api._retire_collaboration_after_worker(None, session, None, run_id, terminal_code)
    cached = state.get_collaboration_status_cache(run_id)
    assert cached["code"] == terminal_code


@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX flock lease")
def test_real_checkpoint_lease_remains_busy_until_cleanup_retry(project_state):
    lease_path = project_state / ".private" / "checkpoint.lock"
    lease_path.parent.mkdir(mode=0o700)
    lease = _CheckpointLease(lease_path)

    class LeaseSession(_Session):
        def __init__(self):
            super().__init__(project_state)
            self.failures = 1

        def close(self):
            self.close_count += 1
            if self.failures:
                self.failures -= 1
                raise RuntimeError("private cleanup detail")
            lease.close()

    session = LeaseSession()
    run_id = "lease-retry"
    run_api._active.update(run_id=run_id, collab_session=session)
    run_api._retain_collaboration(session, None, run_id)
    run_api._drain_collaboration_resources()
    with pytest.raises(HostCollaborationError) as busy:
        _CheckpointLease(lease_path)
    assert busy.value.code == "busy"
    run_api._drain_collaboration_resources()
    replacement = _CheckpointLease(lease_path)
    replacement.close()
    assert not run_api._draining_workers


def test_cache_is_detached_and_matches_symlinked_project_root(project_state, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(project_state, target_is_directory=True)
    run_api._active.update(run_id="r", collab_session=object())
    state.set_collaboration_status_cache({"runId": "r", "projectRoot": str(project_state),
                                          "status": {"state": "closed", "pending": 3}})
    result = state.get_collaboration_status_cache("r")
    result["pending"] = 99
    assert state.get_collaboration_status_cache("r")["pending"] == 3
    state._active.root = str(alias)
    assert state.get_collaboration_status_cache("r")["pending"] == 3
    state._active.root = str(project_state.parent)
    assert state.get_collaboration_status_cache("r") is None


@pytest.mark.parametrize("decision", ["apply", "reject"])
def test_committed_decision_is_not_failed_by_session_close(project_state, decision):
    (project_state / "file.txt").write_text("before\n")
    session = _Session(project_state, failures=1)
    run_id = "decision-run"
    canonical = SimpleNamespace(
        record_proposal_applied=lambda **_kw: None,
        record_proposal_rejected=lambda **_kw: None,
    )
    run_api._active.update(
        run_id=run_id, collab_session=session, coordinator=canonical,
        proposals=[{"path": "file.txt", "new": "after\n", "is_new": False}],
        workspace=None, engine="pipeline",
    )
    events = []
    ctx = SimpleNamespace(_bridge=SimpleNamespace(emit_event=lambda *args: events.append(args)))
    if decision == "apply":
        result = run_api._apply({"paths": ["file.txt"]}, ctx)
        assert result["applied"] == ["file.txt"] and result["checkpointId"]
        assert (project_state / "file.txt").read_text() == "after\n"
        assert events == [("fs.changed", {"kind": "modified", "paths": ["file.txt"]})]
    else:
        assert run_api._reject({}, ctx) == {}
        assert (project_state / "file.txt").read_text() == "before\n"
    assert state.get_collaboration_status_cache(run_id)["state"] == "cleanup_failed"
    assert run_api._active["collab_session"] is session
    assert len(run_api._draining_workers) == 1
    run_api._drain_collaboration_resources()
    expected_code = "run_applied" if decision == "apply" else "run_rejected"
    assert state.get_collaboration_status_cache(run_id)["code"] == expected_code


def test_drain_retries_failure_and_never_closes_running_worker(project_state):
    class Worker:
        running = True
        def isFinished(self):
            return not self.running

    worker = Worker()
    session = _Session(project_state, failures=1)
    run_api._active.update(run_id="drain", collab_session=session)
    run_api._retain_collaboration(session, None, "drain", worker)
    run_api._drain_collaboration_resources()
    assert session.close_count == 0 and run_api._draining_workers
    worker.running = False
    run_api._drain_collaboration_resources()
    assert session.close_count == 1 and run_api._draining_workers
    run_api._drain_collaboration_resources()
    assert session.close_count == 2 and not run_api._draining_workers
    assert run_api._active["collab_session"] is None


def test_drain_keeps_workspace_when_dispose_fails(project_state):
    class Workspace:
        def __init__(self): self.calls = 0
        def dispose(self):
            self.calls += 1
            if self.calls == 1: raise RuntimeError("private")

    workspace = Workspace()
    session = _Session(project_state)
    run_api._active.update(run_id="dispose", collab_session=session, workspace=workspace)
    run_api._retain_collaboration(session, workspace, "dispose")
    run_api._drain_collaboration_resources()
    assert run_api._draining_workers and run_api._active["workspace"] is workspace
    run_api._drain_collaboration_resources()
    assert not run_api._draining_workers and workspace.calls == 2


def test_unavailable_observation_keeps_a_known_terminal_recovery_gate(project_state):
    status = run_api._unavailable_collaboration_status({
        "state": "resnapshot_required", "code": "resnapshot_required",
        "consumedRevision": "a" * 40, "receivedRevision": "b" * 40,
        "pendingCount": 3, "sessionId": "session", "taskId": "task",
        "memberId": "alice", "active": False,
    })
    assert status["state"] == "unavailable"
    assert status["recoveryState"] == "resnapshot_required"
    assert status["recoveryCode"] == "resnapshot_required"
    assert status["consumedRevision"] == "a" * 40
    assert status["pendingCount"] == 3
