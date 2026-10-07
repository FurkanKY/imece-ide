from __future__ import annotations

import json
import subprocess
import threading

from webhost import state
import webhost.api.peer as peer_api


class Context:
    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None
    def resolve(self, value):
        self.result = value; self.event.set()
    def fail(self, code, message):
        self.error = (code, message); self.event.set()


class Manager:
    def __init__(self):
        self.entered = threading.Event(); self.continue_join = threading.Event(); self.discarded = []
    def join(self, root, generation, bundle, *, confirm_pin):
        self.entered.set(); self.continue_join.wait(2)
        return {"peerHandle": "a" * 32, "projectRoot": root, "state": "active"}
    def disconnect(self, **kwargs):
        self.discarded.append(kwargs)
        return {"disconnected": True, "warning": None}


def make_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "initial"], check=True)


def test_async_join_stale_fence_discards_exact_handle(tmp_path):
    root, other = tmp_path / "root", tmp_path / "other"
    make_repo(root); make_repo(other)
    state.set_project(str(root))
    manager = Manager(); state.set_peer_manager(manager)
    ctx = Context()
    peer_api._submit(ctx, str(root.resolve()), state.project_generation(),
                     lambda m: m.join(str(root), state.project_generation(), {}, confirm_pin=True))
    assert manager.entered.wait(2)
    state.set_project(str(other))
    manager.continue_join.set()
    assert ctx.event.wait(2)
    assert ctx.error and ctx.error[0] == "peer_stale"
    assert manager.discarded == [{"handle": "a" * 32}]
    state.set_peer_manager(None)
