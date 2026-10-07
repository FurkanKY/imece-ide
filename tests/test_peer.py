from __future__ import annotations

import subprocess
import threading
import shutil

import pytest

from collab_runtime.errors import ValidationError
from collab_runtime.models import build_initial_state
from collab_runtime.peer import PeerSessionManager, validate_bundle
from collab_runtime.owner import OwnerSessionManager


class FakeClient:
    def __init__(self, endpoint, *, certificate_sha256, session_id):
        self.session_id = session_id
        self.left = []
        self.state = build_initial_state(session_id=session_id, target_version="v1", base_commit=HEAD)
    def pair(self, code, member):
        return "A" * 40
    def snapshot(self, credential):
        return "b" * 40, self.state
    def leave(self, credential):
        self.left.append(credential)


HEAD = ""  # assigned by fixture before the fake client is constructed


def bundle():
    return {"memberId": "alice", "code": "C" * 40, "expiresInSeconds": 300,
            "controlEndpoint": "https://127.0.0.1:41001", "proposalEndpoint": "https://127.0.0.1:41002",
            "certificateSha256": "a" * 64, "sessionId": "peer-test", "epoch": 2}


def repo(tmp_path):
    global HEAD
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "initial"], check=True)
    HEAD = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return root


def test_peer_manager_real_tls_owner_pair_refresh_leave_and_revocation(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("temporary real TLS fixture requires existing OpenSSL")
    root = repo(tmp_path)
    private = tmp_path / "private"
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-subj", "/CN=localhost"], check=True, capture_output=True)
    owner = OwnerSessionManager(private)
    peer = PeerSessionManager()
    try:
        preview = owner.preview_create(root, session_id="peer-test", target_version="v1",
            goal="Coordinate safely", owner_id="alice", member_ids=["alice", "bob"],
            tasks=[{"id": "task-1", "owner": "bob", "goal": "Implement safely", "scopes": ["src/"]}])
        owner.create(preview["previewId"], root)
        owner.start(root, allow_lan=True, bind_address="127.0.0.1", certificate=str(cert), private_key=str(key))
        invite = owner.issue_lan_invitation("bob")
        joined = peer.join(root, 1, invite, confirm_pin=True, pin=invite["certificateSha256"])
        assert joined["state"] == "active" and "credential" not in str(joined) and invite["code"] not in str(joined)
        saved_token = peer._active["token"]
        assert peer.refresh(root, 1, joined["peerHandle"])["tasks"][0]["id"] == "task-1"
        peer.disconnect(root, 1, joined["peerHandle"])
        with pytest.raises(Exception):
            owner._coordinator.snapshot(saved_token)
        second = owner.issue_lan_invitation("bob")
        joined2 = peer.join(root, 1, second, confirm_pin=True, pin=second["certificateSha256"])
        token2 = peer._active["token"]
        owner.revoke_lan_member("bob")
        with pytest.raises(Exception):
            peer.refresh(root, 1, joined2["peerHandle"])
        assert peer.status(root, 1, joined2["peerHandle"])["state"] == "unknown"
        assert token2 not in str(peer.status(root, 1, joined2["peerHandle"]))
    finally:
        peer.shutdown()
        owner.stop()


def test_bundle_validation_and_join_metadata_ram_only(tmp_path):
    root = repo(tmp_path)
    calls = []
    manager = PeerSessionManager(client_factory=lambda *a, **k: (calls.append((a, k)) or FakeClient(*a, **k)))
    raw = bundle()
    invalid = dict(raw, proposalEndpoint="https://127.0.0.1:41001")
    with pytest.raises(ValidationError):
        manager.join(root, 3, invalid, confirm_pin=True, pin="a" * 64)
    assert not calls
    with pytest.raises(ValidationError):
        manager.join(root, 3, raw, confirm_pin=True, pin="0" * 64)
    assert not calls
    result = manager.join(root, 3, raw, confirm_pin=True, pin="a" * 64)
    assert result["memberId"] == "alice" and result["state"] == "active"
    assert "credential" not in result and "token" not in result and "code" not in result
    with pytest.raises(ValidationError):
        manager.join(root, 3, raw, confirm_pin=True, pin="a" * 64)
    assert manager.status(root, 3, result["peerHandle"]) == result
    assert manager.disconnect(root, 3, result["peerHandle"]) == {"disconnected": True, "warning": None}


def test_optional_epoch_and_returned_metadata_are_detached(tmp_path):
    root = repo(tmp_path)
    raw = bundle(); raw.pop("epoch")
    manager = PeerSessionManager(client_factory=FakeClient)
    joined = manager.join(root, 1, raw, confirm_pin=True, pin="a" * 64)
    joined["context"]["goal"] = "tampered"
    joined["tasks"].append({"id": "tampered"})
    status = manager.status(root, 1, joined["peerHandle"])
    assert status["epoch"] == 0 and status["context"]["goal"] == ""
    assert not status["tasks"] and "code" not in manager._active["bundle"]


def test_forget_detaches_during_blocked_refresh_without_resurrection(tmp_path):
    root = repo(tmp_path)
    entered, release = threading.Event(), threading.Event()
    class Blocking(FakeClient):
        snapshots = 0
        def snapshot(self, credential):
            self.snapshots += 1
            if self.snapshots > 1:
                entered.set(); assert release.wait(3)
            return super().snapshot(credential)
    manager = PeerSessionManager(client_factory=Blocking)
    joined = manager.join(root, 1, bundle(), confirm_pin=True, pin="a" * 64)
    errors = []
    worker = threading.Thread(target=lambda: _capture(errors, manager.refresh, root, 1, joined["peerHandle"]))
    worker.start(); assert entered.wait(2)
    manager.forget_project(None)
    with pytest.raises(ValidationError):
        manager.status(root, 1, joined["peerHandle"])
    release.set(); worker.join(3)
    assert not worker.is_alive() and errors


def _capture(errors, function, *args):
    try: function(*args)
    except Exception as exc: errors.append(exc)


def test_peer_rejects_dirty_head_change_after_admission(tmp_path):
    root = repo(tmp_path)
    manager = PeerSessionManager(client_factory=FakeClient)
    joined = manager.join(root, 1, bundle(), confirm_pin=True, pin="a" * 64)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "advance"], check=True)
    with pytest.raises(ValidationError):
        manager.refresh(root, 1, joined["peerHandle"])
    status = manager.status(root, 1, joined["peerHandle"])
    assert status["state"] == "unknown" and "context" not in status
