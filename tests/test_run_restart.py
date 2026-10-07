"""Real Qt/Git/SQLite restart: old evidence is never actionable."""
import json
import os
import subprocess
import sys

import pytest

from agent_execution_runtime import AgentExecutionPorts
from change_runtime import GitWorktreeChangeProvider
from executor_runtime.native_verification import NativeVerificationAttemptAdapter
from run_runtime import RunRuntime, RunStore
from webhost import state
from webhost.run_registry import RunRegistry
import webhost.api.run as run_api
from test_agent_application_e2e import (bridge, qapp, git_repo, isolated_state,
                                       WritingWorker, ScriptedBackend, _pump_until)
from test_run_pipeline_bridge import rpc

_STRICT_NOFOLLOW = os.name == "nt" or (os.scandir in os.supports_fd and os.open in os.supports_dir_fd)


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    # Simulated process restarts must not restore historical pending slots into
    # another module's global registry after monkeypatch teardown.
    monkeypatch.setattr(run_api, "_run_registry", RunRegistry())


@pytest.mark.skipif(not _STRICT_NOFOLLOW, reason="Strict no-follow workspace sealing unavailable")
def test_orderly_close_mid_task_preserves_partial_workspace_for_explicit_restart(monkeypatch, bridge, qapp, git_repo):
    import threading
    import time
    from agent_runtime.cancellation import OperationCancelledError
    started = threading.Event()
    class InterruptedWriter(WritingWorker):
        def run(self, workspace, request, *, execution_id, cancel_token=None):
            workspace.write_text("partial.txt", "partial work\n")
            started.set()
            while not cancel_token.cancelled:
                time.sleep(.01)
            raise OperationCancelledError("closed")
    monkeypatch.setattr(run_api, "build_agent_ports", lambda runtime, run_id, provider:
        AgentExecutionPorts(InterruptedWriter(runtime, run_id),
            NativeVerificationAttemptAdapter(runtime, run_id), GitWorktreeChangeProvider()))
    run_id = rpc(bridge, "run.start", {"task": "Continue partial work", "providerId": "openai"})["result"]["runId"]
    assert started.wait(10)
    slot = run_api._run_registry.get(run_id)
    workspace_root = slot.workspace.root
    run_api.shutdown()
    assert _pump_until(qapp, lambda: slot.worker is None)
    assert (workspace_root / "partial.txt").read_text() == "partial work\n"
    runtime = state.get_run_runtime()
    assert runtime.get_run(run_id).status.value == "cancelled"
    assert runtime.get_run(run_id).workspace_snapshot["state"] == "quiescent"
    monkeypatch.setattr(run_api, "_run_registry", RunRegistry())
    run_api._active.update({"worker": None, "run_id": None, "workspace": None, "coordinator": None, "engine": "legacy"})
    state.set_run_runtime(RunRuntime(RunStore(runtime.store.db_path)))
    monkeypatch.setattr(run_api, "build_agent_ports", lambda runtime, run_id, provider:
        AgentExecutionPorts(WritingWorker(runtime, run_id),
            NativeVerificationAttemptAdapter(runtime, run_id), GitWorktreeChangeProvider()))
    assert rpc(bridge, "run.restart", {"runId": run_id})["ok"]
    assert _pump_until(qapp, lambda: run_api._run_registry.get(run_id).worker is None)
    resumed = run_api._run_registry.get(run_id)
    assert resumed.workspace.root == workspace_root
    assert {item["path"] for item in resumed.proposals} == {"partial.txt", "agent.txt"}
    assert not (git_repo / "partial.txt").exists()


@pytest.mark.skipif(not _STRICT_NOFOLLOW, reason="Strict no-follow workspace sealing unavailable")
def test_native_candidate_bridge_merges_verifies_applies_and_rolls_back(monkeypatch, bridge, qapp, git_repo, tmp_path):
    from fix_runtime.ports import WorkerAttemptResult
    from webhost.api import candidate as candidate_api
    monkeypatch.setattr(candidate_api, "workspaces_dir", lambda: tmp_path / "workspaces")
    (git_repo / ".imece").mkdir()
    (git_repo / ".imece" / "verify.json").write_text(json.dumps([{
        "id": "both", "title": "Both results", "argv": [sys.executable, "-c",
        "from pathlib import Path; assert len(list(Path('.').glob('result-*.txt'))) == 2"],
        "timeout_ms": 5000,
    }]))
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "clean candidate baseline"], cwd=git_repo, check=True, capture_output=True)
    class PerRunWriter(WritingWorker):
        def run(self, workspace, request, *, execution_id, cancel_token=None):
            workspace.write_text(f"result-{self.run_id}.txt", "result\n")
            self.runtime.record(run_id=self.run_id, type="execution.completed", execution_id=execution_id,
                                payload={"final_text": "Written", "model_turns": None, "tool_calls": None})
            return WorkerAttemptResult(execution_id)
    monkeypatch.setattr(run_api, "build_agent_ports", lambda runtime, run_id, provider:
        AgentExecutionPorts(PerRunWriter(runtime, run_id), NativeVerificationAttemptAdapter(runtime, run_id), GitWorktreeChangeProvider()))
    ids = [rpc(bridge, "run.start", {"task": f"Result {i}", "providerId": "openai"})["result"]["runId"] for i in range(2)]
    assert _pump_until(qapp, lambda: all(run_api._run_registry.get(id).worker is None for id in ids))
    receipt = rpc(bridge, "candidate.prepare", {"runIds": ids, "verify": True})["result"]["candidate"]
    assert receipt["verification"]["status"] == "pass"
    assert not list(git_repo.glob("result-*.txt"))
    applied = rpc(bridge, "candidate.apply", {"candidateId": receipt["candidateId"]})["result"]
    assert applied["checkpointId"] and len(list(git_repo.glob("result-*.txt"))) == 2
    runtime = state.get_run_runtime()
    state.set_run_runtime(RunRuntime(RunStore(runtime.store.db_path)))
    assert rpc(bridge, "candidate.list", {})["result"]["candidates"][0]["state"] == "applied"
    rolled_back = rpc(bridge, "candidate.rollback", {"candidateId": receipt["candidateId"]})
    assert rolled_back["ok"] and len(rolled_back["result"]["restored"]) == 2
    assert not list(git_repo.glob("result-*.txt"))


@pytest.mark.skipif(not _STRICT_NOFOLLOW, reason="Strict no-follow workspace sealing unavailable")
def test_restart_runs_real_native_file_tools_without_inheriting_old_evidence(monkeypatch, bridge, qapp, git_repo):
    from agent_runtime import ModelStopReason, ModelToolCall, ModelTurn, ModelUsage
    import engine_factory
    backend = ScriptedBackend()
    backend.turns += [
        ModelTurn("", (ModelToolCall("write-resumed", "write_file", {"path": "native.txt", "content": "resumed result\n"}),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn("Fresh resumed result", (), ModelStopReason.COMPLETED, ModelUsage()),
    ]
    monkeypatch.setattr(engine_factory, "_default_backend_factory", lambda _provider: backend)
    run_id = rpc(bridge, "run.start", {"task": "Write native file", "providerId": "openai"})["result"]["runId"]
    assert _pump_until(qapp, lambda: run_api._run_registry.get(run_id).worker is None)
    runtime = state.get_run_runtime()
    first_execution = run_api._run_registry.get(run_id).evidence["execution_id"]
    run_api.shutdown()
    monkeypatch.setattr(run_api, "_run_registry", RunRegistry())
    run_api._active.update({"worker": None, "run_id": None, "workspace": None, "coordinator": None, "proposals": [], "engine": "legacy"})
    state.set_run_runtime(RunRuntime(RunStore(runtime.store.db_path)))
    assert rpc(bridge, "run.restart", {"runId": run_id})["ok"]
    assert _pump_until(qapp, lambda: run_api._run_registry.get(run_id).worker is None)
    slot = run_api._run_registry.get(run_id)
    assert backend.opens == 2
    assert slot.evidence["execution_id"] != first_execution
    assert (slot.workspace.root / "native.txt").read_text() == "resumed result\n"
    assert not (git_repo / "native.txt").exists()


@pytest.mark.skipif(not _STRICT_NOFOLLOW, reason="Strict no-follow workspace sealing unavailable")
def test_close_during_verification_reaps_detached_child_and_restart_verifies_fresh(monkeypatch, bridge, qapp, git_repo):
    import time
    import psutil
    (git_repo / ".imece").mkdir()
    script = (
        "from pathlib import Path; import subprocess,sys,time; "
        "marker=Path('verify-once'); "
        "sys.exit(0) if marker.exists() else None; marker.touch(); "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
        "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "Path('verify-child.pid').write_text(str(p.pid)); time.sleep(30)"
    )
    (git_repo / ".imece" / "verify.json").write_text(json.dumps([{
        "id": "interruptible", "title": "Interruptible check", "argv": [sys.executable, "-c", script],
        "timeout_ms": 30000,
    }]))
    subprocess.run(["git", "add", ".imece/verify.json"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "interruptible verification"], cwd=git_repo,
                   check=True, capture_output=True)
    monkeypatch.setattr(run_api, "build_agent_ports", lambda runtime, run_id, provider:
        AgentExecutionPorts(WritingWorker(runtime, run_id),
            NativeVerificationAttemptAdapter(runtime, run_id), GitWorktreeChangeProvider()))
    run_id = rpc(bridge, "run.start", {"task": "Write agent.txt", "providerId": "openai"})["result"]["runId"]
    slot = run_api._run_registry.get(run_id)
    workspace_root = slot.workspace.root
    child_file = workspace_root / "verify-child.pid"
    assert _pump_until(qapp, child_file.exists)
    child_pid = int(child_file.read_text())
    runtime = state.get_run_runtime()
    run_api.shutdown()
    assert _pump_until(qapp, lambda: slot.worker is None)
    assert runtime.get_run(run_id).status.value == "cancelled"
    events = runtime.events(run_id, after_seq=0, limit=1000).events
    interrupted = [event for event in events if event.type == "verification.check_interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0].payload["check_id"] == "interruptible"
    assert interrupted[0].payload["producer_quiescent"] is True
    assert interrupted[0].execution_id == interrupted[0].payload["verification_id"]
    assert not any(event.type == "verification.check_completed"
                   and event.payload.get("verification_id") == interrupted[0].payload["verification_id"]
                   for event in events)
    assert runtime.get_run(run_id).workspace_snapshot["state"] == "quiescent"
    assert not psutil.pid_exists(child_pid), "subreaper must reap the cancelled detached child"
    monkeypatch.setattr(run_api, "_run_registry", RunRegistry())
    run_api._active.update({"worker": None, "run_id": None, "workspace": None, "coordinator": None, "proposals": [], "engine": "legacy"})
    state.set_run_runtime(RunRuntime(RunStore(runtime.store.db_path)))
    assert rpc(bridge, "run.restart", {"runId": run_id})["ok"]
    assert _pump_until(qapp, lambda: run_api._run_registry.get(run_id).worker is None)
    assert run_api._run_registry.get(run_id).workspace.root == workspace_root
    fresh = [event for event in state.get_run_runtime().events(run_id, after_seq=0, limit=1000).events
             if event.type == "verification.check_completed"]
    assert fresh and fresh[-1].execution_id != interrupted[0].execution_id
    assert fresh[-1].payload["status"] == "pass"


@pytest.mark.skipif(not _STRICT_NOFOLLOW, reason="Strict no-follow workspace sealing unavailable")
def test_verified_process_tree_receipt_authorizes_explicit_restart(monkeypatch, bridge, qapp, git_repo):
    (git_repo / ".imece").mkdir()
    (git_repo / ".imece" / "verify.json").write_text(json.dumps([{
        "id": "file", "title": "File exists", "argv": [sys.executable, "-c",
        "from pathlib import Path; assert Path('agent.txt').read_text() == 'agent result\\n'"],
        "timeout_ms": 5000,
    }]))
    subprocess.run(["git", "add", ".imece/verify.json"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "verification"], cwd=git_repo,
                   check=True, capture_output=True)
    monkeypatch.setattr(run_api, "build_agent_ports", lambda runtime, run_id, provider:
        AgentExecutionPorts(WritingWorker(runtime, run_id),
            NativeVerificationAttemptAdapter(runtime, run_id), GitWorktreeChangeProvider()))
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    first = rpc(bridge, "run.start", {"task": "Write agent.txt", "providerId": "openai"})
    run_id = first["result"]["runId"]
    assert _pump_until(qapp, lambda: run_api._run_registry.get(run_id).worker is None)
    runtime = state.get_run_runtime()
    old_slot = run_api._run_registry.get(run_id)
    assert old_slot.evidence["verification"]["outcome"] == "pass"
    original_workspace = old_slot.workspace
    assert runtime.get_run(run_id).workspace_snapshot["state"] == "quiescent"
    assert original_workspace.ownership.lease.fd is not None
    run_api.shutdown()
    assert original_workspace.root.exists()
    assert old_slot.workspace is None
    monkeypatch.setattr(run_api, "_run_registry", RunRegistry())
    history = rpc(bridge, "run.get", {"runId": run_id})["result"]
    assert history["readOnly"] and history["continuationAvailable"]
    assert history["proposals"] == [] and history["checkpointId"] is None
    assert not rpc(bridge, "run.applyProposals", {"runId": run_id, "paths": ["agent.txt"]})["ok"]
    run_api._active.update({"worker": None, "run_id": None, "coordinator": None,
                           "workspace": None, "proposals": [], "engine": "legacy"})
    state.set_run_runtime(RunRuntime(RunStore(runtime.store.db_path)))
    try:
        assert rpc(bridge, "run.restart", {"runId": run_id})["ok"]
        assert _pump_until(qapp, lambda: run_api._run_registry.get(run_id).worker is None)
        resumed = run_api._run_registry.get(run_id)
        assert resumed.workspace.root == original_workspace.root
        assert resumed.evidence["verification"]["outcome"] == "pass"
        assert not (git_repo / "agent.txt").exists()
    finally:
        run_api.shutdown()
