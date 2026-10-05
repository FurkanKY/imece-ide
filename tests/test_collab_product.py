import subprocess
from pathlib import Path

import pytest

from collab_runtime.owner import OwnerError, OwnerSessionManager


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"; root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "base"], check=True)
    return root


def _configured(root, tmp_path):
    manager = OwnerSessionManager(tmp_path / "private")
    preview = manager.preview_create(root, session_id="demo", target_version="v1", goal="goal",
        owner_id="alice", member_ids=["alice", "bob", "carol"], tasks=[{"id":"task-a", "owner":"bob", "goal":"work", "scopes":["src/"]}])
    manager.create(preview["previewId"], root)
    return manager


def test_product_snapshot_fresh_detached_and_cas_updates(tmp_path):
    root = _project(tmp_path); manager = _configured(root, tmp_path); manager.start(root)
    board = manager.product_snapshot(root)
    assert board["revision"] == manager._config["store"].fetch_state()[0]
    assert "credential" not in str(board) and "contextHash" in board
    board["context"]["decisions"].append("mutation")
    assert manager.product_snapshot(root)["context"]["decisions"] == []
    board_revision = manager._config["revision"]
    changed = manager.update_product_context(root, context={"goal":"new", "decisions":["decision"], "interfaces":{"api":"v1"}},
        expected_revision=board_revision, expected_epoch=manager.status()["epoch"], expected_session_id="demo")
    assert changed["revision"] != board_revision
    snap = manager.product_snapshot(root)
    assert snap["context"]["goal"] == "new"
    task = manager.update_product_task(root, task_id="task-a", status="done", expected_revision=snap["revision"],
        expected_epoch=snap["epoch"], expected_session_id="demo")
    assert task["status"] == "done"
    assert manager.product_snapshot(root)["tasks"][0]["status"] == "done"
    manager.stop()


def test_product_identity_and_assignee_permissions_fail_closed(tmp_path):
    root = _project(tmp_path); manager = _configured(root, tmp_path); started = manager.start(root)
    snap = manager.product_snapshot(root)
    with pytest.raises(OwnerError, match="product_access_denied"):
        manager.update_product_task(root, task_id="task-a", status="done", expected_revision=snap["revision"],
            expected_epoch=snap["epoch"], expected_session_id="demo", member_id="carol")
    for kwargs in ({"expected_epoch": started["epoch"] + 1}, {"expected_session_id":"other"}):
        args = {"expected_epoch": snap["epoch"], "expected_session_id":"demo"}; args.update(kwargs)
        with pytest.raises(OwnerError):
            manager.update_product_task(root, task_id="task-a", status="done", expected_revision=snap["revision"], **args)
    (tmp_path / "elsewhere").mkdir()
    other = _project(tmp_path / "elsewhere")
    with pytest.raises(OwnerError, match="wrong_project"):
        manager.product_snapshot(other)
    manager.stop()
