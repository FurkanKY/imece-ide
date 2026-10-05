import json, os, subprocess, threading, time
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QCoreApplication
from webhost import state
from webhost.bridge import HostBridge
import webhost.api.owner  # register owner RPC handlers
from collab_runtime.owner import OwnerSessionManager


def test_owner_product_bridge_requires_confirmation_and_uses_cas(tmp_path, monkeypatch):
    root = tmp_path / "project"; root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c", "user.email=test@local", "commit", "--allow-empty", "-qm", "base"], check=True)
    manager = OwnerSessionManager(tmp_path / "private")
    monkeypatch.setattr(state, "get_project", lambda: SimpleNamespace(root=str(root)))
    monkeypatch.setattr(state, "project_generation", lambda: 1)
    monkeypatch.setattr(state, "get_owner_manager", lambda: manager)
    def rpc(method, params):
        app = QCoreApplication.instance() or QCoreApplication([]); replies=[]; host=HostBridge()
        host.reply.connect(lambda raw: replies.append(json.loads(raw)))
        host.call(json.dumps({"id":1,"method":method,"params":params}))
        limit=time.monotonic()+15
        while not replies and time.monotonic()<limit:
            app.processEvents(); threading.Event().wait(.003)
        assert replies
        return replies[0]
    try:
        preview_reply=rpc("collab.owner.previewCreate", {"sessionId":"demo","targetVersion":"v1","goal":"g","ownerId":"alice","memberIds":["alice","bob"],"tasks":[{"id":"t","owner":"bob","goal":"w","scopes":["src/"]}]})
        assert preview_reply["ok"], preview_reply
        prev=preview_reply["result"]
        assert rpc("collab.owner.create", {"previewId":prev["previewId"]})["ok"]
        assert rpc("collab.owner.start", {})["ok"]
        board=rpc("collab.owner.snapshot", {})["result"]
        assert "credential" not in json.dumps(board) and "contextHash" in board
        params={"confirm":False,"expectedRevision":board["revision"],"expectedEpoch":board["epoch"],"expectedSessionId":"demo","context":{"goal":"changed","decisions":[],"interfaces":{}}}
        assert rpc("collab.owner.updateContext", params)["error"]["code"] == "owner_confirmation_required"
        params["confirm"]=True
        receipt=rpc("collab.owner.updateContext", params)
        assert receipt["ok"] and receipt["result"]["revision"] != board["revision"]
        assert rpc("collab.owner.stop", {})["ok"]
    finally:
        manager.stop()
