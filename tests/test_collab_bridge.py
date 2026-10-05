import json
import time

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication

from collab_runtime.coordinator import Snapshot
from collab_runtime.host import CollaborationHost
from collab_runtime.models import SessionState, SharedContext, Task
from webhost import state
from webhost.bridge import HostBridge
import webhost.api.collab  # registers only collaboration handlers


HEAD, REV, SECRET = "a" * 40, "b" * 40, "secret-not-for-the-bridge-012345"


class Client:
    def snapshot(self):
        task = Task("task-1", "member-1", "approved goal", ("src/",), "running", REV)
        return Snapshot(REV, SessionState("session-1", "beta", HEAD,
            SharedContext("context text", (), ()), {task.id: task}))


def call(app, bridge, method, params):
    replies = []
    bridge.reply.connect(lambda raw: replies.append(json.loads(raw)))
    bridge.call(json.dumps({"id": 1, "method": method, "params": params}))
    deadline = time.monotonic() + 3
    while not replies and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert replies, "async bridge reply timed out"
    return replies[-1]


def test_preview_and_approval_are_async_root_bound_and_secret_free(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    project = tmp_path / "project"
    project.mkdir()
    state.set_project(str(project))
    host = CollaborationHost(tmp_path / "cursor", head_reader=lambda _root: HEAD,
                             client_factory=lambda *_a, **_kw: Client())
    state.set_collaboration_host(host)
    bridge = HostBridge()
    preview_reply = call(app, bridge, "collab.preview", {
        "endpoint": "http://127.0.0.1:1234", "credential": SECRET,
        "memberId": "member-1", "taskId": "task-1"})
    assert preview_reply["ok"]
    preview = preview_reply["result"]
    assert SECRET not in json.dumps(preview)
    approved = call(app, bridge, "collab.approve", {"previewId": preview["previewId"]})
    assert approved["ok"] and approved["result"]["resetCursor"] is False
    assert SECRET not in json.dumps(approved)
    assert call(app, bridge, "collab.status", {})["result"] == {"collaboration": None}


def test_preview_rejects_unknown_fields_and_malformed_types(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    project = tmp_path / "project"
    project.mkdir()
    state.set_project(str(project))
    bridge = HostBridge()
    for params in ({"credential": SECRET, "endpoint": "x", "memberId": "m", "taskId": "t",
                    "projectRoot": str(project)},
                   {"credential": 12, "endpoint": "x", "memberId": "m", "taskId": "t"}):
        reply = call(app, bridge, "collab.preview", params)
        assert not reply["ok"] and reply["error"]["code"] == "collab_invalid"
        assert SECRET not in json.dumps(reply)
