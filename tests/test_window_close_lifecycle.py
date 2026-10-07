"""Exercise the real ShellWindow closeEvent in an isolated QtWebEngine process."""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="QtWebEngine offscreen lifecycle fixture is Linux-only")
def test_window_close_event_drains_and_retains_restartable_workspace(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    script = r'''
import json, os, subprocess, sys, tempfile, threading, time
from pathlib import Path
from PySide6.QtWidgets import QApplication
from agent_execution_runtime import AgentExecutionPorts
from agent_runtime.cancellation import OperationCancelledError
from change_runtime import GitWorktreeChangeProvider
from executor_runtime.native_verification import NativeVerificationAttemptAdapter
from run_runtime import RunRuntime, RunStore
from webhost import state
from webhost.run_registry import RunRegistry
import webhost.api.run as run_api
import engine_factory

home = Path.home()
source = home / "source"
source.mkdir()
subprocess.run(["git", "init", "-q", str(source)], check=True)
subprocess.run(["git", "-C", str(source), "config", "user.email", "fixture@example.test"], check=True)
subprocess.run(["git", "-C", str(source), "config", "user.name", "Fixture"], check=True)
(source / "base.txt").write_text("base\n")
subprocess.run(["git", "-C", str(source), "add", "base.txt"], check=True)
subprocess.run(["git", "-C", str(source), "commit", "-qm", "baseline"], check=True)
app = QApplication([])
state.set_project(str(source))
runtime = RunRuntime(RunStore(home / "runtime.sqlite3"))
state.set_run_runtime(runtime)
run_api.workspaces_dir = lambda: home / "workspaces"
engine_factory.workspaces_dir = lambda: home / "workspaces"
started = threading.Event()
class Worker:
    def __init__(self, runtime, run_id): self.runtime, self.run_id = runtime, run_id
    def run(self, workspace, request, *, execution_id, cancel_token=None):
        workspace.write_text("partial.txt", "preserved\n")
        started.set()
        while not cancel_token.cancelled:
            time.sleep(.005)
        raise OperationCancelledError("window closed")

def ports(rt, rid, provider):
    return AgentExecutionPorts(Worker(rt, rid), NativeVerificationAttemptAdapter(rt, rid), GitWorktreeChangeProvider())
run_api.build_agent_ports = ports
from webhost.window import ShellWindow
window = ShellWindow()
replies = {}
window.bridge.reply.connect(lambda raw: replies.update({json.loads(raw)["id"]: json.loads(raw)}))
def rpc(method, params, call_id):
    window.bridge.call(json.dumps({"id": call_id, "method": method, "params": params}))
    deadline = time.monotonic() + 15
    while call_id not in replies and time.monotonic() < deadline:
        app.processEvents(); time.sleep(.005)
    assert call_id in replies, f"no reply for {method}"
    assert replies[call_id]["ok"], replies[call_id]
    return replies.pop(call_id)["result"]
run_id = rpc("run.start", {"task": "preserve partial work", "providerId": "openai"}, 1)["runId"]
assert started.wait(10)
slot = run_api._run_registry.get(run_id)
root = slot.workspace.root
window._web_ready = True  # mirror a mounted UI after its explicit close confirmation
window._close_confirmed = True
window.close()  # invokes ShellWindow.closeEvent -> run_api.shutdown
assert window.isHidden()
deadline = time.monotonic() + 10
while slot.worker is not None and time.monotonic() < deadline:
    app.processEvents(); time.sleep(.005)
assert slot.worker is None, "closeEvent returned before native worker drained"
assert (root / "partial.txt").read_text() == "preserved\n"
assert runtime.get_run(run_id).workspace_snapshot["state"] == "quiescent"
assert runtime.get_run(run_id).status.value == "cancelled"
assert slot.workspace is None, "closeEvent must stash, not dispose, owned worktree"
assert root.exists()
# Reopen the canonical SQLite projection as a new host would.
state.set_run_runtime(RunRuntime(RunStore(home / "runtime.sqlite3")))
run_api._run_registry = RunRegistry()
run_api._active.update({"worker": None, "run_id": None, "workspace": None, "coordinator": None,
                        "proposals": [], "engine": "legacy", "cancel_event": None})
class ResumedWorker:
    def __init__(self, runtime, run_id): self.runtime, self.run_id = runtime, run_id
    def run(self, workspace, request, *, execution_id, cancel_token=None):
        workspace.write_text("resumed.txt", "fresh execution\n")
        self.runtime.record(run_id=self.run_id, type="execution.completed", execution_id=execution_id,
                            payload={"final_text": "resumed", "model_turns": None, "tool_calls": None})
        from fix_runtime.ports import WorkerAttemptResult
        return WorkerAttemptResult(execution_id)
run_api.build_agent_ports = lambda rt, rid, provider: AgentExecutionPorts(
    ResumedWorker(rt, rid), NativeVerificationAttemptAdapter(rt, rid), GitWorktreeChangeProvider())
task_id = runtime.get_run(run_id).task_id
continued = rpc("run.restart", {"runId": run_id}, 2)
assert continued["runId"] == run_id
slot = run_api._run_registry.get(run_id)
deadline = time.monotonic() + 15
while slot.worker is not None and time.monotonic() < deadline:
    app.processEvents(); time.sleep(.005)
assert slot.worker is None
assert slot.workspace.root == root
assert (root / "resumed.txt").read_text() == "fresh execution\n"
assert runtime.get_run(run_id).task_id == task_id
run_api.shutdown()
'''
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "appdata"),
        "QT_QPA_PLATFORM": "offscreen",
        "QTWEBENGINE_DISABLE_SANDBOX": "1",
        "PYTHONPATH": str(repo),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    Path(env["HOME"]).mkdir()
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=repo, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45,
    )
    assert completed.returncode == 0, completed.stdout
