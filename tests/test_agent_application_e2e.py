"""Focused real-host bridge coverage for explicit single-provider runs."""
import json
import os
import sys
import threading
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
    root = str(git_repo.resolve())
    for slot in run_api._run_registry.slots():
        if slot.project_root != root:
            continue
        if slot.worker is not None and slot.worker.isRunning():
            slot.cancel_event.set()
            assert slot.worker.wait(5000)
        if slot.activity_streamer is not None:
            slot.activity_streamer.request_stop()
            slot.activity_streamer.wait(2000)
            assert slot.activity_streamer.isFinished()
            slot.activity_streamer = None
        if slot.workspace is not None:
            slot.workspace.dispose()
            slot.workspace = None
        if slot.worker is not None and slot.worker.isFinished():
            slot.worker = None
        assert run_api._run_registry.forget_cleaned(slot.run_id)
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


def _supports_safe_inventory():
    """No-follow file-identity inspection is POSIX-only; without it the run's
    own check may pass while the delivered evidence is correctly invalidated."""
    return (
        os.scandir in os.supports_fd
        and os.open in os.supports_dir_fd
        and hasattr(os, "O_NOFOLLOW")
    )


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


def test_two_agent_runs_keep_workspaces_and_proposals_isolated(monkeypatch, bridge, qapp, git_repo, tmp_path):
    release = threading.Event()
    class PerRunWriter(WritingWorker):
        def run(self, workspace, request, *, execution_id, cancel_token=None):
            assert release.wait(5), "both agents should be admitted before either completes"
            goal = request.task.split("\n", 2)[1] if request.task.startswith("Original task:\n") else request.task
            name = goal.rsplit(" ", 1)[-1]
            workspace.write_text(name, f"{name}\n")
            self.runtime.record(run_id=self.run_id, type=RunEventType.EXECUTION_COMPLETED,
                                execution_id=execution_id, payload={"final_text": name})
            return WorkerAttemptResult(execution_id)

    def ports(runtime, run_id, provider_id):
        return AgentExecutionPorts(PerRunWriter(runtime, run_id),
                                   NativeVerificationAttemptAdapter(runtime, run_id),
                                   GitWorktreeChangeProvider())

    monkeypatch.setattr(run_api, "build_agent_ports", ports)
    a = rpc(bridge, "run.start", {"task": "write alpha.txt", "providerId": "openai"})["result"]["runId"]
    b = rpc(bridge, "run.start", {"task": "write beta.txt", "providerId": "openai"})["result"]["runId"]
    assert a != b
    assert run_api._run_registry.get(a).worker.isRunning()
    assert run_api._run_registry.get(b).worker.isRunning()
    third = rpc(bridge, "run.start", {"task": "write gamma.txt", "providerId": "openai"})
    assert third["error"]["code"] == "run_capacity"
    legacy_start = rpc(bridge, "run.start", {"task": "legacy must not replace agents"})
    assert legacy_start["error"]["code"] == "busy"
    ambiguous = rpc(bridge, "run.rejectProposals", {})
    assert ambiguous["error"]["code"] == "run_id_required"
    unknown = rpc(bridge, "run.cancel", {"runId": "not-a-run"})
    assert unknown["error"]["code"] == "unknown_run"
    assert len(run_api._run_registry.open_slots(str(git_repo.resolve()))) == 2
    release.set()
    assert _pump_until(qapp, lambda: all(
        run_api._run_registry.get(run_id).proposals for run_id in (a, b)))
    slot_a, slot_b = run_api._run_registry.get(a), run_api._run_registry.get(b)
    listing = rpc(bridge, "run.list", {})["result"]["runs"]
    assert {item["runId"] for item in listing} >= {a, b}
    detail = rpc(bridge, "run.get", {"runId": a})["result"]
    assert detail["runId"] == a and detail["engine"] == "agent"
    detail["evidence"]["verification"]["outcome"] = "mutated by caller"
    assert slot_a.evidence["verification"]["outcome"] != "mutated by caller"
    prior_b_workspace = slot_b.workspace
    prior_b_proposals = [dict(proposal) for proposal in slot_b.proposals]
    continued = rpc(bridge, "run.followUp", {"runId": a, "feedback": "Keep the same goal"})
    assert continued["result"]["runId"] == a
    assert _pump_until(qapp, lambda: bool(slot_a.proposals))
    assert slot_b.workspace is prior_b_workspace and slot_b.proposals == prior_b_proposals
    state.set_project(str(tmp_path))
    wrong_project = rpc(bridge, "run.get", {"runId": a})
    assert wrong_project["error"]["code"] == "run_project_mismatch"
    state.set_project(str(git_repo))
    assert slot_a.workspace is not slot_b.workspace
    assert slot_a.workspace.root != slot_b.workspace.root
    assert [p["path"] for p in slot_a.proposals] == ["alpha.txt"]
    assert [p["path"] for p in slot_b.proposals] == ["beta.txt"]

    applied = rpc(bridge, "run.applyProposals", {"runId": a, "paths": ["alpha.txt"]})
    assert applied["result"]["applied"] == ["alpha.txt"]
    applied_detail = rpc(bridge, "run.get", {"runId": a})["result"]
    assert applied_detail["checkpointId"] == applied["result"]["checkpointId"]
    assert (git_repo / "alpha.txt").read_text() == "alpha.txt\n"
    assert slot_b.proposals[0]["path"] == "beta.txt"
    assert slot_b.workspace is not None
    rejected = rpc(bridge, "run.rejectProposals", {"runId": b})
    assert rejected["ok"]
    rejected_detail = rpc(bridge, "run.get", {"runId": b})["result"]
    assert rejected_detail["proposals"] == [] and rejected_detail["evidence"] is None
    assert not (git_repo / "beta.txt").exists()
    assert run_api._run_registry.get(a).coordinator.get_run().status.value == "succeeded"
    third = rpc(bridge, "run.start", {"task": "write gamma.txt", "providerId": "openai"})
    assert third["ok"], "clean terminal workspaces must release bounded admission"
    slot_c = run_api._run_registry.get(third["result"]["runId"])
    assert _pump_until(qapp, lambda: bool(slot_c.proposals))
    assert rpc(bridge, "run.rejectProposals", {"runId": slot_c.run_id})["ok"]


def test_cancel_targets_only_selected_live_agent(monkeypatch, bridge, qapp, git_repo):
    source_a_before = (git_repo / "a.txt").read_text(encoding="utf-8")
    entered = {name: threading.Event() for name in ("a.txt", "b.txt")}
    release_b = threading.Event()

    class CancellableGate:
        def __init__(self, runtime, run_id):
            self.runtime, self.run_id = runtime, run_id

        def run(self, workspace, request, *, execution_id, cancel_token=None):
            name = request.task.rsplit(" ", 1)[-1]
            entered[name].set()
            if name == "a.txt":
                while not cancel_token.cancelled:
                    time.sleep(.005)
                cancel_token.raise_if_cancelled()
            while not release_b.wait(.005):
                cancel_token.raise_if_cancelled()
            workspace.write_text(name, "pending\n")
            self.runtime.record(run_id=self.run_id, type=RunEventType.EXECUTION_COMPLETED,
                                execution_id=execution_id, payload={"final_text": name})
            return WorkerAttemptResult(execution_id)

    def ports(runtime, run_id, provider_id):
        return AgentExecutionPorts(CancellableGate(runtime, run_id),
                                   NativeVerificationAttemptAdapter(runtime, run_id),
                                   GitWorktreeChangeProvider())

    monkeypatch.setattr(run_api, "build_agent_ports", ports)
    a = rpc(bridge, "run.start", {"task": "write a.txt", "providerId": "openai"})["result"]["runId"]
    b = rpc(bridge, "run.start", {"task": "write b.txt", "providerId": "openai"})["result"]["runId"]
    assert entered["a.txt"].wait(5) and entered["b.txt"].wait(5)
    slot_a, slot_b = run_api._run_registry.get(a), run_api._run_registry.get(b)
    assert slot_a.worker.isRunning() and slot_b.worker.isRunning()
    cancelled = rpc(bridge, "run.cancel", {"runId": a})
    assert cancelled["result"]["runId"] == a
    assert slot_a.cancel_event.is_set()
    assert not slot_b.cancel_event.is_set()
    assert slot_b.worker.isRunning() and slot_b.workspace.root.is_dir()
    assert _pump_until(qapp, lambda: slot_a.coordinator.get_run().status.value == "cancelled")
    assert slot_b.worker.isRunning() and slot_b.workspace.root.is_dir()
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == source_a_before
    assert not (git_repo / "b.txt").exists()
    release_b.set()
    assert _pump_until(qapp, lambda: bool(slot_b.proposals))
    assert slot_b.proposals[0]["path"] == "b.txt"
    assert rpc(bridge, "run.rejectProposals", {"runId": b})["ok"]


def test_shutdown_cancels_all_agents_but_retains_busy_workspace_ownership(monkeypatch, bridge, qapp, git_repo):
    entered = {"one.txt": threading.Event(), "two.txt": threading.Event()}
    release = threading.Event()

    class ShutdownGate:
        def __init__(self, runtime, run_id):
            self.runtime, self.run_id = runtime, run_id

        def run(self, workspace, request, *, execution_id, cancel_token=None):
            name = request.task.rsplit(" ", 1)[-1]
            entered[name].set()
            release.wait(8)  # deliberately does not observe cancellation until the gate opens
            cancel_token.raise_if_cancelled()
            workspace.write_text(name, "done\n")
            self.runtime.record(run_id=self.run_id, type=RunEventType.EXECUTION_COMPLETED,
                                execution_id=execution_id, payload={"final_text": name})
            return WorkerAttemptResult(execution_id)

    def ports(runtime, run_id, provider_id):
        return AgentExecutionPorts(ShutdownGate(runtime, run_id),
                                   NativeVerificationAttemptAdapter(runtime, run_id),
                                   GitWorktreeChangeProvider())

    monkeypatch.setattr(run_api, "build_agent_ports", ports)
    ids = [rpc(bridge, "run.start", {"task": f"write {name}", "providerId": "openai"})
           ["result"]["runId"] for name in entered]
    assert all(entered[name].wait(5) for name in entered)
    slots = [run_api._run_registry.get(run_id) for run_id in ids]
    roots = [slot.workspace.root for slot in slots]
    run_api.shutdown()
    assert all(slot.cancel_event.is_set() for slot in slots)
    assert all(slot.worker.isRunning() and root.is_dir() for slot, root in zip(slots, roots))
    release.set()
    assert _pump_until(qapp, lambda: all(slot.workspace is None for slot in slots))


def test_overlapping_agent_apply_uses_source_hash_cas_without_path_claims(monkeypatch, bridge, qapp, git_repo):
    class SharedPathWriter(WritingWorker):
        def run(self, workspace, request, *, execution_id, cancel_token=None):
            content = request.task.rsplit(" ", 1)[-1] + "\n"
            workspace.write_text("shared.txt", content)
            self.runtime.record(run_id=self.run_id, type=RunEventType.EXECUTION_COMPLETED,
                                execution_id=execution_id, payload={"final_text": content})
            return WorkerAttemptResult(execution_id)

    def ports(runtime, run_id, provider_id):
        return AgentExecutionPorts(SharedPathWriter(runtime, run_id),
                                   NativeVerificationAttemptAdapter(runtime, run_id),
                                   GitWorktreeChangeProvider())

    monkeypatch.setattr(run_api, "build_agent_ports", ports)
    a = rpc(bridge, "run.start", {"task": "write first", "providerId": "openai"})["result"]["runId"]
    b = rpc(bridge, "run.start", {"task": "write second", "providerId": "openai"})["result"]["runId"]
    slot_a, slot_b = run_api._run_registry.get(a), run_api._run_registry.get(b)
    assert _pump_until(qapp, lambda: bool(slot_a.proposals) and bool(slot_b.proposals))
    applied = rpc(bridge, "run.applyProposals", {"runId": a, "paths": ["shared.txt"]})["result"]
    assert applied["applied"] == ["shared.txt"] and applied["checkpointId"]
    checkpoint_files_before = set((git_repo / ".imece" / "checkpoints").glob("*.json"))
    conflict = rpc(bridge, "run.applyProposals", {"runId": b, "paths": ["shared.txt"]})["result"]
    assert conflict["applied"] == [] and conflict["checkpointId"] is None
    assert conflict["conflicts"][0]["path"] == "shared.txt"
    assert (git_repo / "shared.txt").read_text() == "first\n"
    assert set((git_repo / ".imece" / "checkpoints").glob("*.json")) == checkpoint_files_before
    assert slot_b.proposals and slot_b.workspace is not None
    assert slot_b.coordinator.get_run().status.value == "waiting_user"
    assert rpc(bridge, "run.rejectProposals", {"runId": b})["ok"]


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
    assert _pump_until(qapp, lambda: run_api._active["coordinator"].get_run().status.value == "waiting_user"
                       and bool(run_api._run_registry.get(run_id).proposals))
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
                       and len(created[0].requests) == 2
                       and bool(run_api._run_registry.get(run_id).proposals))
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
    canonical = state.get_run_runtime().events(run_id).events
    completed_verifications = [event for event in canonical
                               if event.type == RunEventType.VERIFICATION_COMPLETED]
    completed_checks = [event for event in canonical
                        if event.type == RunEventType.VERIFICATION_CHECK_COMPLETED]
    assert len(completed_verifications) == 1
    assert completed_verifications[0].payload["status"] == "pass"
    assert [(event.payload["check_id"], event.payload["status"])
            for event in completed_checks] == [("native-file", "pass")]
    # The REAL verification report passes on every platform; the delivered
    # evidence can only keep that "pass" where the no-follow file-identity
    # inspection that backs it is supported.
    verification = evidence["verification"]
    assert verification["outcome"] == (
        "pass" if verification["fingerprint_complete"] else "invalidated"
    )
    if _supports_safe_inventory():
        assert verification["fingerprint_complete"] is True
    else:
        assert verification["fingerprint_complete"] is False
    assert any(event.type == RunEventType.EXECUTION_COMPLETED for event in canonical)
    assert run_api._active["coordinator"].get_run().status.value == "waiting_user"


def test_legacy_coordinator_retains_three_role_defaults(tmp_path, git_repo):
    runtime = RunRuntime(RunStore(tmp_path / "legacy-runs.sqlite3"))
    coordinator = LegacyRunCoordinator.start(
        runtime, project_root=str(git_repo), task="legacy task",
    )
    assert coordinator.get_run().routing == DEFAULT_ROUTING
