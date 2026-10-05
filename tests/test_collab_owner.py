import json
import subprocess
from pathlib import Path

import pytest

from collab_runtime.client import LoopbackSnapshotClient
from collab_runtime.owner import OwnerError, OwnerSessionManager


def project(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "base"], check=True)
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return root, head


def request(root: Path) -> dict:
    return {"session_id": "demo", "target_version": "v1", "goal": "A shared goal",
            "owner_id": "alice", "member_ids": ["alice", "bob"],
            "tasks": [{"id": "task-a", "owner": "bob", "goal": "Implement safely",
                       "scopes": ["src/"]}]}


def test_preview_is_inert_and_create_publishes_schema1_metadata(tmp_path):
    root, head = project(tmp_path)
    private = tmp_path / "private"
    manager = OwnerSessionManager(private)
    preview = manager.preview_create(root, **request(root))
    assert preview["baseCommit"] == head
    assert preview["warnings"]
    assert not private.exists()
    assert manager.status()["state"] == "unconfigured"

    result = manager.create(preview["previewId"], root)
    assert result["state"] == "configured"
    store = manager._config["store"]
    revision, state = store.fetch_state()
    assert state.context.goal == "A shared goal"
    assert state.tasks["task-a"].context_revision == revision or store.is_ancestor(
        state.tasks["task-a"].context_revision, revision)
    assert state.tasks["task-a"].owner == "bob"
    assert subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip() == head
    assert sorted(path.name for path in private.iterdir())


def test_preview_validation_happens_before_private_files(tmp_path):
    root, _ = project(tmp_path)
    private = tmp_path / "private"
    manager = OwnerSessionManager(private)
    bad = request(root)
    bad["member_ids"] = ["alice", "alice"]
    with pytest.raises(OwnerError):
        manager.preview_create(root, **bad)
    bad = request(root)
    bad["tasks"] = [{"id": "x", "owner": "mallory", "goal": "g", "scopes": ["src/"]}]
    with pytest.raises(OwnerError):
        manager.preview_create(root, **bad)
    assert not private.exists()


def test_start_reveal_once_wrong_token_denied_and_stop_rotates_epoch(tmp_path):
    root, _ = project(tmp_path)
    manager = OwnerSessionManager(tmp_path / "private")
    preview = manager.preview_create(root, **request(root))
    manager.create(preview["previewId"], root)
    first = manager.start(root)
    revealed = manager.reveal_member_once("bob")
    assert first["epoch"] == revealed["epoch"]
    assert "credential" not in json.dumps(manager.status())
    with pytest.raises(OwnerError, match="already_shared"):
        manager.reveal_member_once("bob")
    with pytest.raises(Exception):
        LoopbackSnapshotClient(revealed["endpoint"], credential="x" * 32).snapshot()
    assert LoopbackSnapshotClient(revealed["endpoint"], credential=revealed["credential"]).snapshot().state.session_id == "demo"
    manager.stop()
    second = manager.start(root)
    assert second["epoch"] == first["epoch"] + 1
    with pytest.raises(Exception):
        LoopbackSnapshotClient(second["endpoint"], credential=revealed["credential"]).snapshot()
    manager.stop()


def test_source_head_change_invalidates_preview_without_creation(tmp_path):
    root, _ = project(tmp_path)
    manager = OwnerSessionManager(tmp_path / "private")
    preview = manager.preview_create(root, **request(root))
    subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "next"], check=True)
    with pytest.raises(OwnerError, match="preview_stale"):
        manager.create(preview["previewId"], root)
    assert not (tmp_path / "private").exists()


def test_status_remains_available_while_stop_is_draining(tmp_path, monkeypatch):
    root, _ = project(tmp_path)
    manager = OwnerSessionManager(tmp_path / "private")
    preview = manager.preview_create(root, **request(root))
    manager.create(preview["previewId"], root)
    manager.start(root)
    entered, release = __import__("threading").Event(), __import__("threading").Event()
    original = manager._server.close
    def blocked_close():
        entered.set()
        assert release.wait(3)
        original()
    monkeypatch.setattr(manager._server, "close", blocked_close)
    thread = __import__("threading").Thread(target=manager.stop)
    thread.start()
    assert entered.wait(2)
    assert manager.status()["state"] == "stopping"
    release.set()
    thread.join(4)
    assert not thread.is_alive()


def test_existing_metadata_selection_is_explicit_and_source_bound(tmp_path):
    root, _ = project(tmp_path)
    first = OwnerSessionManager(tmp_path / "private")
    preview = first.preview_create(root, **request(root))
    created = first.create(preview["previewId"], root)
    selected = OwnerSessionManager(tmp_path / "other-private")
    result = selected.select_existing(root, store_path=Path(created["storePath"]),
                                      hub_path=Path(created["hubPath"]), owner_id="alice",
                                      member_ids=["alice", "bob"])
    assert result["sessionId"] == "demo"
    assert result["state"] == "configured"


def test_failed_close_keeps_cleanup_state_and_retry_starts_fresh_epoch(tmp_path, monkeypatch):
    root, _ = project(tmp_path)
    manager = OwnerSessionManager(tmp_path / "private")
    preview = manager.preview_create(root, **request(root))
    manager.create(preview["previewId"], root)
    old = manager.start(root)
    original = manager._server.close
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("do not expose this detail")
        original()

    monkeypatch.setattr(manager._server, "close", fail_once)
    with pytest.raises(OwnerError, match="cleanup_failed"):
        manager.stop()
    assert manager.status()["state"] == "cleanup_failed"
    assert manager.status()["retryRequired"] is True
    with pytest.raises(OwnerError):
        manager.start(root)
    manager.stop()
    new = manager.start(root)
    assert new["epoch"] == old["epoch"] + 1
    manager.stop()
