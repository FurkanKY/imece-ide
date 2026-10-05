"""Focused owner-only task creation coverage using temporary bare Git stores."""
import shutil
import subprocess
from pathlib import Path

import pytest

from collab_runtime.coordinator import Coordinator
from collab_runtime.errors import AccessDeniedError, StaleRevisionError, ValidationError
from collab_runtime.models import build_initial_state
from collab_runtime.store import GitStore
from collab_runtime.owner import OwnerError, OwnerSessionManager
from webhost.bridge import BridgeError
from webhost.api import owner as owner_api

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
OWNER = "A" * 40
MEMBER = "B" * 40


@pytest.fixture
def session(tmp_path):
    hub = GitStore.create_bare(tmp_path / "hub.git", what="hub")
    store_path = GitStore.create_bare(tmp_path / "store.git", what="store")
    store = GitStore(store=store_path, remote=str(hub))
    revision = store.init_session(build_initial_state(session_id="create-test", target_version="v1", base_commit="a" * 40))
    coordinator = Coordinator(store, session_id="create-test", owner_id="alice",
                              member_credentials={"alice": OWNER, "bob": MEMBER})
    return store, coordinator, revision


def test_owner_creates_queued_task_with_observed_context_revision(session):
    store, coordinator, revision = session
    result = coordinator.create_task(OWNER, task_id="new-task", owner="bob", goal="Build it", scopes=["src/"], expected_revision=revision)
    new_revision, state = store.fetch_state()
    task = state.tasks["new-task"]
    assert result == new_revision != revision
    assert (task.owner, task.status, task.context_revision) == ("bob", "queued", revision)
    assert task.scopes == ("src/",)


def test_assignee_and_unknown_credentials_are_denied_before_state_fetch(session, monkeypatch):
    _store, coordinator, revision = session
    def forbidden(): raise AssertionError("state fetch must not precede owner authentication")
    monkeypatch.setattr(coordinator, "_fetch_state", forbidden)
    for credential in (MEMBER, "Z" * 40):
        with pytest.raises(AccessDeniedError):
            coordinator.create_task(credential, task_id="new", owner="bob", goal="goal", scopes=[], expected_revision=revision)


@pytest.mark.parametrize("kwargs", [
    {"task_id": "bad/id", "owner": "bob", "goal": "goal", "scopes": []},
    {"task_id": "new", "owner": "unknown", "goal": "goal", "scopes": []},
    {"task_id": "new", "owner": "bob", "goal": "", "scopes": []},
    {"task_id": "new", "owner": "bob", "goal": "goal", "scopes": ["x" * 513]},
])
def test_invalid_new_task_is_rejected(session, kwargs):
    _store, coordinator, revision = session
    with pytest.raises(ValidationError):
        coordinator.create_task(OWNER, **kwargs, expected_revision=revision)


def test_create_never_replaces_existing_task_and_stale_revision_fails(session):
    store, coordinator, revision = session
    first = coordinator.create_task(OWNER, task_id="new", owner="bob", goal="goal", scopes=[], expected_revision=revision)
    with pytest.raises(ValidationError):
        coordinator.create_task(OWNER, task_id="new", owner="alice", goal="changed", scopes=[], expected_revision=first)
    with pytest.raises(StaleRevisionError):
        coordinator.create_task(OWNER, task_id="other", owner="bob", goal="goal", scopes=[], expected_revision=revision)
    assert list(store.fetch_state()[1].tasks) == ["new"]


def _project(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "base"], check=True)
    return path


def _manager(root: Path, private: Path) -> OwnerSessionManager:
    manager = OwnerSessionManager(private)
    preview = manager.preview_create(root, session_id="manager-test", target_version="v1", goal="goal",
        owner_id="alice", member_ids=["alice", "bob"], tasks=[{"id": "initial", "owner": "bob", "goal": "work", "scopes": ["src/"]}])
    manager.create(preview["previewId"], root)
    return manager


def test_owner_manager_returns_committed_receipt_and_snapshot_contains_task(tmp_path):
    root = _project(tmp_path / "source"); manager = _manager(root, tmp_path / "private")
    try:
        running = manager.start(root); board = manager.product_snapshot(root)
        receipt = manager.create_product_task(root, task_id="follow-up", owner="bob", goal="Continue",
            scopes=["src/next/"], expected_revision=board["revision"], expected_epoch=board["epoch"],
            expected_session_id=board["sessionId"])
        assert receipt == {"revision": receipt["revision"], "sessionId": "manager-test", "epoch": running["epoch"],
            "action": "createTask", "taskId": "follow-up", "status": "queued", "owner": "bob",
            "contextRevision": board["revision"]}
        task = next(item for item in manager.product_snapshot(root)["tasks"] if item["id"] == "follow-up")
        assert (task["status"], task["contextRevision"]) == ("queued", board["revision"])
    finally:
        manager.stop()


def test_stopped_owner_manager_cannot_create_task(tmp_path):
    root = _project(tmp_path / "source"); manager = _manager(root, tmp_path / "private")
    board = manager.product_snapshot(root)
    with pytest.raises(OwnerError, match="not_running"):
        manager.create_product_task(root, task_id="new", owner="bob", goal="goal", scopes=[],
            expected_revision=board["revision"], expected_epoch=board["epoch"], expected_session_id=board["sessionId"])


def test_create_task_bridge_rejects_confirmation_and_overrides_before_project_io(monkeypatch):
    monkeypatch.setattr(owner_api, "_project", lambda: (_ for _ in ()).throw(AssertionError("project I/O")))
    identity = {"expectedRevision": "a" * 40, "expectedEpoch": 1, "expectedSessionId": "session"}
    valid_task = {"id": "new", "owner": "bob", "goal": "goal", "scopes": []}
    with pytest.raises(BridgeError) as missing_confirmation:
        owner_api._product_create_task({**identity, "confirm": 1, "task": valid_task}, None)
    assert missing_confirmation.value.code == "owner_confirmation_required"
    with pytest.raises(BridgeError) as override:
        owner_api._product_create_task({**identity, "confirm": True, "task": {**valid_task, "status": "done"}}, None)
    assert override.value.code == "owner_invalid"
