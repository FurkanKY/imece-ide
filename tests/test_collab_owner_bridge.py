"""Owner lifecycle bridge contract: explicit setup, private metadata, and one-shot sharing."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from collab_runtime.owner import OwnerSessionManager
from webhost import state
from webhost.api import owner
from webhost.bridge import BridgeError, HostBridge


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@local",
                               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@local"}).stdout.decode().strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@local"], root)
    (root / "source.txt").write_text("untouched\n")
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "initial"], root)
    manager = OwnerSessionManager(tmp_path / "private")
    monkeypatch.setattr(state, "get_project", lambda: SimpleNamespace(root=str(root)))
    monkeypatch.setattr(state, "project_generation", lambda: 1)
    monkeypatch.setattr(state, "get_owner_manager", lambda: manager)
    monkeypatch.setattr(state, "peek_owner_manager", lambda: manager)
    monkeypatch.setattr(state, "get_collaboration_host", lambda: manager._host)
    try:
        yield SimpleNamespace(root=root, private=tmp_path / "private", manager=manager)
    finally:
        manager.stop()


def _rpc(method, params, timeout=20):
    app = QCoreApplication.instance() or QCoreApplication([])
    bridge, replies = HostBridge(), []
    bridge.reply.connect(lambda raw: replies.append(json.loads(raw)))
    bridge.call(json.dumps({"id": 41, "method": method, "params": params}))
    until = time.monotonic() + timeout
    while not replies and time.monotonic() < until:
        app.processEvents()
        threading.Event().wait(.003)
    assert replies, f"no reply for {method}"
    return replies[0]


def _create_args():
    return {"sessionId": "session-1", "targetVersion": "v1", "goal": "Coordinate safely",
            "ownerId": "alice", "memberIds": ["alice", "bob"],
            "tasks": [{"id": "task-1", "owner": "bob", "goal": "Implement task",
                       "scopes": ["src/"]}]}


def test_status_and_invalid_preview_are_inert(world):
    assert _rpc("collab.owner.status", {})["result"]["state"] == "unconfigured"
    bad = _create_args() | {"tasks": [{"id": "x", "owner": "mallory", "goal": "g", "scopes": ["src/"]}]}
    assert _rpc("collab.owner.previewCreate", bad)["ok"] is False
    assert not world.private.exists()
    assert _rpc("collab.owner.start", {"port": True})["ok"] is False
    assert not world.private.exists()


def test_create_preview_then_create_touches_only_private_metadata(world):
    before = _git(["status", "--porcelain=v1"], world.root)
    preview_reply = _rpc("collab.owner.previewCreate", _create_args())
    assert preview_reply["ok"] is True, preview_reply
    preview = preview_reply["result"]
    assert preview["mode"] == "create" and preview["projectRoot"] == str(world.root)
    assert not world.private.exists()
    created = _rpc("collab.owner.create", {"previewId": preview["previewId"]})
    assert created["ok"] is True, created
    status = created["result"]
    assert status["state"] == "configured" and status["endpoint"] is None
    assert _git(["status", "--porcelain=v1"], world.root) == before
    assert _git(["rev-parse", "HEAD"], world.root) == preview["baseCommit"]
    assert (world.root / ".imece").exists() is False


def test_start_status_share_once_and_stop_rotate_credentials(world):
    preview = _rpc("collab.owner.previewCreate", _create_args())["result"]
    assert _rpc("collab.owner.create", {"previewId": preview["previewId"]})["ok"]
    started = _rpc("collab.owner.start", {})
    assert started["ok"] is True, started
    status = _rpc("collab.owner.status", {})["result"]
    assert status["state"] == "running" and status["endpoint"].startswith("http://127.0.0.1:")
    assert "credential" not in json.dumps(status)
    assert _rpc("collab.owner.shareOnce", {"memberId": "bob", "confirmSecret": False})["ok"] is False
    secret = _rpc("collab.owner.shareOnce", {"memberId": "bob", "confirmSecret": True})
    assert secret["ok"] is True, secret
    dto = secret["result"]
    assert dto["credential"] and dto["scope"] == "loopback-only" and dto["taskIds"] == ["task-1"]
    assert _rpc("collab.owner.shareOnce", {"memberId": "bob", "confirmSecret": True})["ok"] is False
    stopped = _rpc("collab.owner.stop", {})
    assert stopped["ok"] is True and stopped["result"]["state"] == "stopped"
    restarted = _rpc("collab.owner.start", {})
    assert restarted["ok"] is True
    assert restarted["result"]["epoch"] > dto["epoch"]
    assert _rpc("collab.owner.shareOnce", {"memberId": "bob", "confirmSecret": True})["result"]["credential"] != dto["credential"]
    assert _rpc("collab.owner.stop", {})["ok"] is True


def test_local_preview_uses_loopback_and_remains_unapproved(world):
    preview = _rpc("collab.owner.previewCreate", _create_args())["result"]
    assert _rpc("collab.owner.create", {"previewId": preview["previewId"]})["ok"]
    assert _rpc("collab.owner.start", {})["ok"]
    assert _rpc("collab.owner.shareOnce", {"memberId": "bob", "confirmSecret": True})["ok"]
    reply = _rpc("collab.owner.localPreview", {"memberId": "bob", "taskId": "task-1"})
    assert reply["ok"] is True, reply
    dto = reply["result"]
    assert dto["preview"]["memberId"] == "bob"
    assert "credential" not in json.dumps(dto)
    assert dto["endpoint"].startswith("http://127.0.0.1:")
    assert dto["preview"]["previewId"] in world.manager._host._candidates
    assert world.manager._host._approved == {}  # Preview handoff is never implicit approval.
    assert _rpc("collab.owner.stop", {})["ok"]


@pytest.mark.parametrize("bad", [
    {"unexpected": 1}, {"sessionId": "", "targetVersion": "v", "goal": "g", "ownerId": "a",
     "memberIds": ["a"], "tasks": []},
])
def test_exact_parameter_whitelist(bad):
    with pytest.raises(BridgeError) as exc:
        owner._preview_create(bad, None)
    assert exc.value.code == "owner_invalid"


def test_share_rejects_wrong_project_before_one_shot_reveal(world, monkeypatch):
    p = world.manager.preview_create(world.root, **{"session_id": "s", "target_version": "v",
        "goal": "g", "owner_id": "alice", "member_ids": ["alice", "bob"],
        "tasks": [{"id": "t", "owner": "bob", "goal": "g", "scopes": ["src/"]}]})
    world.manager.create(p["previewId"], world.root)
    world.manager.start(world.root)
    monkeypatch.setattr(state, "get_project", lambda: SimpleNamespace(root=str(world.root) + "-other"))
    response = _rpc("collab.owner.shareOnce", {"memberId": "bob", "confirmSecret": True})
    assert response["ok"] is False
    assert "bob" not in world.manager.status()["exportedMembers"]
    world.manager.stop()


def test_shutdown_stops_only_existing_manager_without_blocking(world):
    preview = world.manager.preview_create(world.root, session_id="s", target_version="v", goal="g",
                                           owner_id="alice", member_ids=["alice"], tasks=[])
    world.manager.create(preview["previewId"], world.root)
    world.manager.start(world.root)
    owner.shutdown()
    deadline = time.monotonic() + 5
    while world.manager.status()["state"] != "stopped" and time.monotonic() < deadline:
        threading.Event().wait(.005)
    assert world.manager.status()["state"] == "stopped"
