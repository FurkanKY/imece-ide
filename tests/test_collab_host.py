from __future__ import annotations

import pytest

from collab_runtime.coordinator import Snapshot
from collab_runtime.host import CollaborationHost, HostCollaborationError, _private_cursor_dir
from collab_runtime.models import SharedContext, Task, SessionState


HEAD = "a" * 40
REV = "b" * 40
SECRET = "secretcredential_not_for_output_123456"


def _snapshot():
    task = Task("task-1", "member-1", "Do the approved work", ("src/",), "running", REV)
    state = SessionState("session-1", "beta", HEAD, SharedContext("untrusted context", ("decision",), ()),
                         {task.id: task})
    return Snapshot(REV, state)


class FakeClient:
    def __init__(self, snapshot):
        self._credential = SECRET
        self.value = snapshot

    def snapshot(self):
        return self.value


def _host(tmp_path, snapshot=None):
    project = tmp_path / "project"
    project.mkdir()
    current = snapshot or _snapshot()
    client = FakeClient(current)
    host = CollaborationHost(tmp_path / "private" / "collab-cursors",
        head_reader=lambda root: HEAD,
        client_factory=lambda endpoint, *, credential: client)
    return host, project, client


def test_preview_is_not_approval_and_dtos_never_expose_credential(tmp_path):
    host, project, _ = _host(tmp_path)
    preview = host.preview(project, "http://127.0.0.1:1234", SECRET, "member-1", "task-1")
    assert set(preview) == {"previewId", "projectRoot", "endpoint", "memberId", "taskId",
                            "sessionId", "targetVersion", "baseCommit", "revision", "task", "context"}
    assert SECRET not in repr(preview)
    assert SECRET not in repr(host)
    with pytest.raises(HostCollaborationError):
        host.bind_run("forged", project, "run-1")

    approval = host.approve(preview["previewId"], project)
    assert approval["resetCursor"] is False
    session = host.bind_run(approval["approvalHandle"], project, "run-1")
    status = session.status()
    assert status["state"] == "inactive"
    assert {"state", "code", "consumedRevision", "receivedRevision", "pendingCount",
            "sessionId", "taskId", "memberId", "active"} == set(status)
    host.release(approval["approvalHandle"])
    # Release removes registry authority, but does not dispose the run's lease.
    assert session.status()["sessionId"] == "session-1"
    with pytest.raises(HostCollaborationError):
        host.bind_run(approval["approvalHandle"], project, "run-2")


@pytest.mark.parametrize("flag", [1, "true", None])
def test_approval_reset_requires_exact_boolean(tmp_path, flag):
    host, project, _ = _host(tmp_path)
    preview = host.preview(project, "http://127.0.0.1:1234", SECRET, "member-1", "task-1")
    with pytest.raises(HostCollaborationError):
        host.approve(preview["previewId"], project, reset_cursor=flag)


def test_approval_is_root_bound_and_rechecks_pinned_head(tmp_path):
    host, project, _ = _host(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    preview = host.preview(project, "http://127.0.0.1:1234", SECRET, "member-1", "task-1")
    with pytest.raises(HostCollaborationError):
        host.approve(preview["previewId"], other)


def test_changed_head_prevents_approval(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    current = [HEAD]
    client = FakeClient(_snapshot())
    host = CollaborationHost(tmp_path / "cursor", head_reader=lambda root: current[0],
        client_factory=lambda endpoint, *, credential: client)
    preview = host.preview(project, "http://127.0.0.1:1234", SECRET, "member-1", "task-1")
    current[0] = "c" * 40
    with pytest.raises(HostCollaborationError):
        host.approve(preview["previewId"], project)


def test_cursor_namespace_is_private_and_symlinks_are_rejected(tmp_path):
    root = tmp_path / "cursor"
    assert _private_cursor_dir(root) == root
    assert root.stat().st_mode & 0o777 == 0o700
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(HostCollaborationError):
        _private_cursor_dir(link)
