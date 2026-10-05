"""Bridge contract tests for local shared-delivery RPCs."""
import json
import os
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from collab_runtime.context import SharedSnapshot, parse_snapshot_dict
from collab_runtime.delivery import SharedDeliveryService
from collab_runtime.models import build_context, build_initial_state, build_task
from collab_runtime.store import GitStore
from run_runtime.models import RunStatus
from webhost import state
from webhost.api import delivery
from webhost.api import run as run_api
from webhost.bridge import BridgeError
from webhost.bridge import HostBridge
from workspace.worktree import GitWorktreeWorkspace


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "Test",
                               "GIT_AUTHOR_EMAIL": "test@local",
                               "GIT_COMMITTER_NAME": "Test",
                               "GIT_COMMITTER_EMAIL": "test@local"}).stdout.decode().strip()


@pytest.fixture
def delivery_world(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@local"], root)
    (root / "app.py").write_text("base\n")
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "base"], root)
    base = _git(["rev-parse", "HEAD"], root)
    workspace = GitWorktreeWorkspace.create(source_root=root, run_id="delivery-test",
                                            base_dir=tmp_path / "worktrees")
    hub = GitStore.create_bare(tmp_path / "hub.git")
    store_path = GitStore.create_bare(tmp_path / "store.git", what="store")
    store = GitStore(store=store_path, remote=str(hub))
    revision = store.init_session(build_initial_state(session_id="s1", target_version="v1",
                                                       base_commit=base))
    revision = store.update_context(build_context(goal="goal", decisions=[], interfaces={}),
                                    expected_revision=revision)
    store.upsert_task(build_task(task_id="task1", owner="owner", goal="task",
                                 scopes=["app.py"], status="running", context_revision=revision),
                      expected_revision=revision)
    head, snapshot_state = store.fetch_state()
    binding = SharedSnapshot(head, snapshot_state.context_hash, snapshot_state, "task1")
    class Session:
        project_root = root
        run_id = "run1"
        active = False

        @property
        def accepted_binding(self):
            return parse_snapshot_dict(binding.to_dict())

        def status(self):
            return {"sessionId": "s1", "taskId": "task1", "memberId": "owner"}

    session = Session()
    borrowed = {"session": session, "workspace": workspace, "binding": binding,
                "project_root": root.resolve(), "generation": 7, "run_id": "run1",
                "available_paths": ("app.py",)}

    real_borrow = run_api.borrow_delivery_context

    @contextmanager
    def borrow(run_id):
        if run_id != "run1":
            raise RuntimeError("invalid")
        yield borrowed

    monkeypatch.setattr(run_api, "borrow_delivery_context", borrow)
    monkeypatch.setattr(state, "get_project", lambda: SimpleNamespace(root=str(root)))
    monkeypatch.setattr(state, "project_generation", lambda: 7)
    service = SharedDeliveryService()
    monkeypatch.setattr(state, "get_delivery_service", lambda: service)
    yield SimpleNamespace(root=root, workspace=workspace, hub=hub, store=store, session=session,
                          real_borrow=real_borrow,
                          store_path=store_path, binding=binding, service=service)
    workspace.dispose()


class _Context:
    def __init__(self):
        self.done = threading.Event()
        self.result = None
        self.error = None

    def resolve(self, result):
        self.result = result
        self.done.set()

    def fail(self, code, message):
        self.error = (code, message)
        self.done.set()


def _host_bridge_call(method, params):
    app = QCoreApplication.instance() or QCoreApplication([])
    bridge = HostBridge()
    replies = []
    bridge.reply.connect(lambda raw: replies.append(json.loads(raw)))
    bridge.call(json.dumps({"id": 11, "method": method, "params": params}))
    deadline = __import__("time").monotonic() + 10
    while not replies and __import__("time").monotonic() < deadline:
        app.processEvents()
        threading.Event().wait(.005)
    assert replies, "HostBridge did not return delivery RPC response"
    return replies[0]


def test_delivery_bridge_preview_publish_list_candidate_are_explicit_and_metadata_only(delivery_world):
    world = delivery_world
    (world.workspace.root / "app.py").write_text("shared code\n")
    args = {"runId": "run1", "storePath": str(world.store_path),
            "hubPath": str(world.hub), "paths": ["app.py"]}
    response = _host_bridge_call("collab.delivery.preview", args)
    assert response["ok"] is True, response
    preview = response["result"]
    assert preview["paths"] == ["app.py"]
    assert not world.store.remote_proposal_head(preview["proposalId"])
    published = _host_bridge_call("collab.delivery.publish", {
        "runId": "run1", "previewId": preview["previewId"]})
    assert published["ok"] is True, published
    receipt = published["result"]
    assert receipt["proposalId"] == preview["proposalId"]
    listed_reply = _host_bridge_call("collab.delivery.list", {
        key: args[key] for key in ("runId", "storePath", "hubPath")})
    assert listed_reply["ok"] is True, listed_reply
    listed = listed_reply["result"]
    assert listed["proposals"][0]["proposalId"] == preview["proposalId"]
    assert "content" not in json.dumps(listed)
    candidate_dir = world.root.parent / "candidate"
    candidate_reply = _host_bridge_call("collab.delivery.candidate", {
        "runId": "run1", "storePath": str(world.store_path), "hubPath": str(world.hub),
        "proposalIds": [preview["proposalId"]], "outputPath": str(candidate_dir),
    })
    assert candidate_reply["ok"] is True, candidate_reply
    candidate = candidate_reply["result"]
    assert candidate["conflicts"] == []
    assert candidate["candidate"]["verification"]["status"] == "not_run"
    assert (candidate_dir / "app.py").read_text() == "shared code\n"


@pytest.mark.parametrize("params", [
    {"runId": "run1", "storePath": "s", "hubPath": "h", "paths": "app.py"},
    {"runId": "run1", "storePath": "s", "hubPath": "h", "paths": ["app.py"], "unexpected": True},
])
def test_delivery_bridge_rejects_forged_shapes_and_exact_types(params):
    with pytest.raises(BridgeError) as exc:
        delivery._preview(params, _Context())
    assert exc.value.code == "delivery_invalid"
    with pytest.raises(BridgeError):
        delivery._candidate({"runId": "r", "storePath": "s", "hubPath": "h",
                             "proposalIds": ["p"], "outputPath": "o", "verify": 1}, _Context())


def test_discard_is_memory_only_and_idempotent(delivery_world):
    world = delivery_world
    (world.workspace.root / "app.py").write_text("discard me\n")
    preview_reply = _host_bridge_call("collab.delivery.preview", {"runId": "run1",
        "storePath": str(world.store_path), "hubPath": str(world.hub), "paths": ["app.py"]})
    preview = preview_reply["result"]
    assert _host_bridge_call("collab.delivery.discard", {"previewId": preview["previewId"]})["result"] == {}
    assert _host_bridge_call("collab.delivery.discard", {"previewId": preview["previewId"]})["result"] == {}
    assert not world.store.remote_proposal_head(preview["proposalId"])


def test_native_context_borrow_requires_idle_waiting_user_accepted_session(delivery_world, monkeypatch):
    world = delivery_world
    class Worker:
        def isFinished(self):
            return True

    monkeypatch.setitem(run_api._active, "run_id", "run1")
    monkeypatch.setitem(run_api._active, "engine", "pipeline")
    monkeypatch.setitem(run_api._active, "collab_session", world.session)
    monkeypatch.setitem(run_api._active, "workspace", world.workspace)
    monkeypatch.setitem(run_api._active, "worker", Worker())
    monkeypatch.setitem(run_api._active, "proposals", [{"path": "app.py"}])
    monkeypatch.setitem(run_api._active, "coordinator",
                        SimpleNamespace(get_run=lambda: SimpleNamespace(status=RunStatus.WAITING_USER)))
    with world.real_borrow("run1") as borrowed:
        assert borrowed["binding"] is not world.session.accepted_binding
        assert borrowed["binding"].to_dict() == world.binding.to_dict()
        assert borrowed["available_paths"] == ("app.py",)
        assert run_api._delivery_leases
        assert run_api._delivery_is_busy(world.session, "run1")
    assert not run_api._delivery_is_busy(world.session, "run1")

    monkeypatch.setitem(run_api._active, "engine", "legacy")
    with pytest.raises(RuntimeError):
        with world.real_borrow("run1"):
            pass
