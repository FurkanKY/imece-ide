"""Focused real-host bridge coverage for explicit single-provider runs."""
import json
import sys
import time

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication

from agent_execution_runtime import AgentExecutionPorts
from change_runtime import GitWorktreeChangeProvider
from executor_runtime import NativeVerificationAttemptAdapter
from fix_runtime.ports import WorkerAttemptResult
from executor_runtime.native_verification import NativeVerificationAttemptAdapter
from agent_runtime import ModelStopReason, ModelToolCall, ModelTurn, ModelUsage
from run_runtime.events import RunEventType
from run_runtime import RunRuntime, RunStore
from run_runtime.legacy import LegacyRunCoordinator
from agents import DEFAULT_ROUTING
import engine_factory
import ui_prefs
from test_run_pipeline_bridge import bridge as bridge_fixture, git_repo, rpc
from webhost import state
import webhost.api.run as run_api


@pytest.fixture(scope="session")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def bridge(qapp):
    return bridge_fixture.__wrapped__(qapp)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch, git_repo):
    run_api._active.update({"worker": None, "coordinator": None, "run_id": None, "proposals": [],
                            "engine": "legacy", "workspace": None, "cancel_event": None})
    monkeypatch.setattr(engine_factory, "workspaces_dir", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(ui_prefs, "load", lambda: {**ui_prefs.DEFAULTS, "ai_engine": "auto"})
    state.set_project(str(git_repo))
    state.set_run_runtime(RunRuntime(RunStore(tmp_path / "runs.sqlite3")))
    yield
    worker = run_api._active.get("worker")
    if worker is not None:
        if worker.isRunning():
            cancel_event = run_api._active.get("cancel_event")
            if cancel_event is not None:
                cancel_event.set()
            assert worker.wait(5000), "agent worker did not stop after cooperative cancellation"
        assert not worker.isRunning(), "agent worker must be quiescent before fixture cleanup"
    qapp = QCoreApplication.instance()
    if qapp is not None:
        qapp.processEvents()
    streamer = run_api._active.get("activity_streamer")
    run_api._stop_activity_streamer()
    if streamer is not None:
        assert streamer.wait(2000), "activity streamer did not stop before fixture cleanup"
    run_api._dispose_workspace()
    run_api._active.update({"worker": None, "coordinator": None, "run_id": None, "proposals": [],
                            "engine": "legacy", "workspace": None, "cancel_event": None})
    state._active = None
    state.set_run_runtime(None)


class WritingWorker:
    def __init__(self, runtime, run_id):
        self.runtime, self.run_id = runtime, run_id

    def run(self, workspace, request, *, execution_id, cancel_token=None):
        workspace.write_text("agent.txt", "agent result\n")
        self.runtime.record(run_id=self.run_id, type=RunEventType.EXECUTION_COMPLETED,
                            execution_id=execution_id,
                            payload={"final_text": "Created agent.txt", "model_turns": None,
                                     "tool_calls": None})
        return WorkerAttemptResult(execution_id)


class ScriptedBackend:
    """Model backend fake used through the real NativeWorkerAttemptAdapter."""
    def __init__(self):
        self.turns = [
            ModelTurn("", (ModelToolCall("write-1", "write_file",
                                          {"path": "native.txt", "content": "native result\n"}),),
                      ModelStopReason.TOOL_USE, ModelUsage()),
            ModelTurn("Implemented native fixture.", (), ModelStopReason.COMPLETED, ModelUsage()),
        ]
        self.opens = 0

    def open_session(self, **_kwargs):
        self.opens += 1
        backend = self
        class Session:
            def respond(self, _input_items):
                return backend.turns.pop(0)
        return Session()


def _pump_until(app, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


def test_provider_id_runs_one_agent_and_returns_evidence_proposal(monkeypatch, bridge, qapp, git_repo):
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    def ports(runtime, run_id, provider_id):
        return AgentExecutionPorts(WritingWorker(runtime, run_id),
                                   NativeVerificationAttemptAdapter(runtime, run_id),
                                   GitWorktreeChangeProvider())

    monkeypatch.setattr(run_api, "build_agent_ports", ports)
    reply = rpc(bridge, "run.start", {"task": "write agent.txt", "providerId": "openai"})
    assert "result" in reply
    run_id = reply["result"]["runId"]
    runtime = state.get_run_runtime()
    canonical_run = runtime.get_run(run_id)
    assert canonical_run.routing == {"agent_provider": "openai"}
    task_record = runtime.store.get_task(canonical_run.task_id)
    assert set(task_record.__dataclass_fields__) == {"task_id", "project_root", "prompt", "created_at"}
    assert _pump_until(qapp, lambda: any(e.get("channel") == "run.finished" for e in events))
    assert (git_repo / "agent.txt").exists() is False
    payloads = [e.get("payload", {}).get("ev", {}) for e in events]
    assert any(e.get("type") == "evidence" and e.get("verification", {}).get("outcome") == "not_run"
               for e in payloads)
    assert any(e.get("type") == "proposal" and e.get("proposals") for e in payloads)
    assert run_api._active["engine"] == "agent"
    assert run_api._active["coordinator"].run_id == run_id


def test_agent_follow_up_reuses_run_workspace_and_reject_keeps_source_untouched(monkeypatch, bridge, qapp, git_repo):
    class FollowingWorker(WritingWorker):
        def __init__(self, runtime, run_id):
            super().__init__(runtime, run_id)
            self.requests = []

        def run(self, workspace, request, *, execution_id, cancel_token=None):
            self.requests.append(request.task)
            name = "first.txt" if len(self.requests) == 1 else "second.txt"
            workspace.write_text(name, "proposal\n")
            self.runtime.record(run_id=self.run_id, type=RunEventType.EXECUTION_COMPLETED,
                                execution_id=execution_id, payload={"final_text": name})
            return WorkerAttemptResult(execution_id)

    created = []
    def ports(runtime, run_id, provider_id):
        worker = FollowingWorker(runtime, run_id)
        created.append(worker)
        return AgentExecutionPorts(worker, NativeVerificationAttemptAdapter(runtime, run_id),
                                   GitWorktreeChangeProvider())

    monkeypatch.setattr(run_api, "build_agent_ports", ports)
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    started = rpc(bridge, "run.start", {"task": "Keep the original goal", "providerId": "openai"})
    run_id = started["result"]["runId"]
    assert _pump_until(qapp, lambda: run_api._active["coordinator"].get_run().status.value == "waiting_user")
    workspace = run_api._active["workspace"]
    monkeypatch.setattr(ui_prefs, "load", lambda: {
        **ui_prefs.DEFAULTS,
        "ai_engine": "auto",
        "planner_provider": "different-planner",
        "coder_provider": "different-coder",
        "reviewer_provider": "different-reviewer",
    })
    resumed = rpc(bridge, "run.followUp", {"feedback": "Also add the second file"})
    assert resumed["result"]["runId"] == run_id
    assert _pump_until(qapp, lambda: run_api._active["coordinator"].get_run().status.value == "waiting_user"
                       and len(created[0].requests) == 2)
    assert run_api._active["workspace"] is workspace
    assert run_api._active["agent_provider_id"] == "openai"
    assert state.get_run_runtime().get_run(run_id).routing == {"agent_provider": "openai"}
    assert "Keep the original goal" in created[0].requests[1]
    assert "Also add the second file" in created[0].requests[1]
    events_before_reject = run_api.state.get_run_runtime().events(run_id).events
    assert sum(event.type == RunEventType.RUN_RESUMED for event in events_before_reject) == 1
    rejected = rpc(bridge, "run.rejectProposals", {})
    assert rejected["ok"]
    assert not (git_repo / "first.txt").exists()
    assert not (git_repo / "second.txt").exists()


def test_provider_bridge_uses_real_native_worker_with_scripted_backend(monkeypatch, bridge, qapp, git_repo):
    backend = ScriptedBackend()
    monkeypatch.setattr(engine_factory, "_default_backend_factory", lambda _provider_id: backend)
    (git_repo / ".imece").mkdir()
    (git_repo / ".imece" / "verify.json").write_text(json.dumps([{
        "id": "native-file", "title": "Check generated file",
        "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('native.txt').read_text() == 'native result\\n'"],
        "timeout_ms": 5000,
    }]))
    import subprocess
    subprocess.run(["git", "add", ".imece/verify.json"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "add verification fixture"], cwd=git_repo,
                   check=True, capture_output=True)
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    reply = rpc(bridge, "run.start", {"task": "create native.txt", "providerId": "openai"})
    assert "result" in reply
    run_id = reply["result"]["runId"]
    assert _pump_until(qapp, lambda: any(e.get("channel") == "run.finished" for e in events))
    assert backend.opens == 1
    assert not (git_repo / "native.txt").exists()
    assert any(e.get("payload", {}).get("ev", {}).get("type") == "proposal" for e in events)
    evidence = next(e["payload"]["ev"] for e in events
                    if e.get("payload", {}).get("ev", {}).get("type") == "evidence")
    assert evidence["verification"]["outcome"] == "pass"
    canonical = state.get_run_runtime().events(run_id).events
    assert any(event.type == RunEventType.EXECUTION_COMPLETED for event in canonical)
    assert run_api._active["coordinator"].get_run().status.value == "waiting_user"


def test_legacy_coordinator_retains_three_role_defaults(tmp_path, git_repo):
    runtime = RunRuntime(RunStore(tmp_path / "legacy-runs.sqlite3"))
    coordinator = LegacyRunCoordinator.start(
        runtime, project_root=str(git_repo), task="legacy task",
    )
    assert coordinator.get_run().routing == DEFAULT_ROUTING
