import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentSession, ApprovalDecision, ModelStopReason, ModelToolCall, ModelTurn, ModelUsage  # noqa: E402
from agent_runtime.errors import AgentApprovalError, AgentLifecycleError  # noqa: E402
from process_runtime import ProcessRequest, ProcessResult, ProcessRunner  # noqa: E402
from process_runtime.errors import ProcessSpawnError  # noqa: E402
from run_runtime import CanonicalAgentEventSink, RunEventType, RunRuntime, RunStatus, RunStore  # noqa: E402
from tool_runtime import (  # noqa: E402
    ApprovalGrant,
    Dispatcher,
    PermissionEffect,
    PermissionRule,
    PolicyEvaluator,
    ToolCall,
    ToolExecutionContext,
    ToolRegistry,
)
from tool_runtime.errors import ToolApprovalRequiredError, ToolDeniedError, ToolExecutionError  # noqa: E402
from tool_runtime.tools.process import RunProcessExecutor, register_process_tool  # noqa: E402
from workspace.local import LocalWorkspace  # noqa: E402


class ScriptedSession:
    def __init__(self, turns):
        self.turns = list(turns)
        self.inputs = []

    def respond(self, input_items):
        self.inputs.append(input_items)
        value = self.turns.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ScriptedBackend:
    def __init__(self, turns):
        self.session = ScriptedSession(turns)

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        return self.session


class CountingRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, workspace, request):
        self.calls.append((workspace, request))
        return self.result


def registry_with_process(runner=None):
    registry = ToolRegistry()
    register_process_tool(registry, runner)
    return registry


def context(tmp_path):
    return ToolExecutionContext(LocalWorkspace(tmp_path))


def test_run_process_tool_returns_real_success_and_nonzero_as_observations(tmp_path):
    registry = registry_with_process(ProcessRunner())
    dispatcher = Dispatcher(registry, PolicyEvaluator([
        PermissionRule("process.execute", "*", PermissionEffect.ALLOW),
    ]))
    ctx = context(tmp_path)
    prepared = dispatcher.prepare(
        ToolCall("ok", "run_process", {"argv": [sys.executable, "-c", "print('ok')"]}), ctx
    )
    observation = dispatcher.execute(prepared, ctx)
    assert observation.metadata["exit_code"] == 0
    assert observation.metadata["timed_out"] is False
    assert observation.metadata["execution_isolation"] == "host"
    assert "ok" in observation.content

    prepared = dispatcher.prepare(
        ToolCall("bad", "run_process", {"argv": [sys.executable, "-c", "import sys; sys.exit(3)"]}), ctx
    )
    observation = dispatcher.execute(prepared, ctx)
    assert observation.metadata["exit_code"] == 3
    assert observation.metadata["timed_out"] is False


def test_run_process_permission_resource_binds_exact_request(tmp_path):
    runner = CountingRunner(ProcessResult(
        argv=(sys.executable,), cwd=".", exit_code=0, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
        stdout_bytes=0, stderr_bytes=0,
    ))
    registry = registry_with_process(runner)
    dispatcher = Dispatcher(registry, PolicyEvaluator())
    ctx = context(tmp_path)
    prepared = dispatcher.prepare(
        ToolCall("p", "run_process", {"argv": [sys.executable, "-c", "print(1)"], "env": {"X": "1"}}), ctx
    )
    assert prepared.policy_decision.effect is PermissionEffect.ASK
    resource = json.loads(prepared.permission_requests[0].resource)
    assert resource["argv"] == [sys.executable, "-c", "print(1)"]
    assert resource["env"] == {"X": "1"}
    with pytest.raises(ToolApprovalRequiredError):
        dispatcher.execute(prepared, ctx)
    with pytest.raises(ToolDeniedError):
        denied_dispatcher = Dispatcher(registry, PolicyEvaluator([
                PermissionRule("process.execute", "*", PermissionEffect.DENY),
            ]))
        denied = denied_dispatcher.prepare(
                ToolCall("denied", "run_process", {"argv": [sys.executable]}), ctx
            )
            # The exception is raised by execute, not prepare.
        denied_dispatcher.execute(denied, ctx)
    assert runner.calls == []


def test_run_process_ask_approval_executes_once_and_deny_does_not(tmp_path):
    registry = registry_with_process(CountingRunner(ProcessResult(
        argv=(sys.executable,), cwd=".", exit_code=0, timed_out=False, duration_ms=1,
        stdout="ok", stderr="", stdout_truncated=False, stderr_truncated=False,
        stdout_bytes=2, stderr_bytes=0,
    )))
    ctx = context(tmp_path)
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("run", "run_process", {"argv": [sys.executable, "-c", "print('x')"]}),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn("done", (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    session = AgentSession(
        backend=backend,
        registry=registry,
        policy=PolicyEvaluator(),
        context=ctx,
    )
    pause = session.start("run")
    assert not registry._executor_for("run_process")._runner.calls
    denied = session.resume(ApprovalDecision(pause.call_id, pause.approval_fingerprint, False, pause.session_id))
    assert denied.final_text == "done"
    assert not registry._executor_for("run_process")._runner.calls


def test_run_process_ask_approval_wrong_then_approve_executes_once_and_propagates_metadata(tmp_path):
    runner = CountingRunner(ProcessResult(
        argv=(sys.executable,), cwd=".", exit_code=3, timed_out=True, duration_ms=12,
        stdout="out", stderr="err", stdout_truncated=True, stderr_truncated=False,
        stdout_bytes=100, stderr_bytes=3,
    ))
    registry = registry_with_process(runner)
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("approve-me", "run_process", {
            "argv": [sys.executable, "-c", "print('x')"],
        }),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn("done", (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    session = AgentSession(
        backend=backend,
        registry=registry,
        policy=PolicyEvaluator(),
        context=context(tmp_path),
    )
    pause = session.start("run")
    assert not runner.calls

    with pytest.raises(AgentApprovalError):
        session.resume(ApprovalDecision("wrong", pause.approval_fingerprint, True, pause.session_id))
    assert not runner.calls

    outcome = session.resume(
        ApprovalDecision(pause.call_id, pause.approval_fingerprint, True, pause.session_id)
    )
    assert outcome.final_text == "done"
    assert len(runner.calls) == 1
    result = backend.session.inputs[1][0].result
    assert result.is_error is False
    assert result.metadata == {
        "exit_code": 3,
        "timed_out": True,
        "duration_ms": 12,
        "stdout_truncated": True,
        "stderr_truncated": False,
        "stdout_bytes": 100,
        "stderr_bytes": 3,
        "cwd": ".",
        "argv": [sys.executable],
        "execution_isolation": "host",
    }
    with pytest.raises(AgentLifecycleError):
        session.resume(
            ApprovalDecision(pause.call_id, pause.approval_fingerprint, True, pause.session_id)
        )
    assert len(runner.calls) == 1


def test_run_process_infrastructure_failure_translates_at_dispatcher_and_agent_boundaries(tmp_path):
    class FailingRunner:
        def run(self, workspace, request):
            raise ProcessSpawnError("executable unavailable")

    registry = registry_with_process(FailingRunner())
    dispatcher = Dispatcher(registry, PolicyEvaluator([
        PermissionRule("process.execute", "*", PermissionEffect.ALLOW),
    ]))
    ctx = context(tmp_path)
    prepared = dispatcher.prepare(
        ToolCall("spawn-failure", "run_process", {"argv": [sys.executable]}), ctx
    )
    with pytest.raises(ToolExecutionError) as exc_info:
        dispatcher.execute(prepared, ctx)
    assert isinstance(exc_info.value.__cause__, ProcessSpawnError)

    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("spawn-failure-agent", "run_process", {
            "argv": [sys.executable],
        }),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn("recovered", (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    session = AgentSession(
        backend=backend,
        registry=registry,
        policy=PolicyEvaluator([
            PermissionRule("process.execute", "*", PermissionEffect.ALLOW),
        ]),
        context=ctx,
    )
    outcome = session.start("run")
    assert outcome.final_text == "recovered"
    assert backend.session.inputs[1][0].result.is_error is True


def test_process_tool_canonical_bridge_preserves_metadata(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="run process")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    registry = registry_with_process(ProcessRunner())
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("process-1", "run_process", {
            "argv": [sys.executable, "-c", "print('ok')"],
        }),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn("finished", (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    session = AgentSession(
        backend=backend,
        registry=registry,
        policy=PolicyEvaluator([PermissionRule("process.execute", "*", PermissionEffect.ALLOW)]),
        context=ToolExecutionContext(LocalWorkspace(tmp_path), run_id=run.run_id, execution_id="exec-process"),
        event_sink=CanonicalAgentEventSink(runtime, run.run_id, execution_id="exec-process"),
        execution_id="exec-process",
    )
    outcome = session.start("run process")
    assert outcome.final_text == "finished"
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    events = runtime.events(run.run_id, limit=200).events
    completed = next(event for event in events if event.type == RunEventType.TOOL_COMPLETED)
    assert completed.payload["call_id"] == "process-1"
    assert completed.payload["metadata"]["exit_code"] == 0
    assert completed.payload["metadata"]["timed_out"] is False
    assert completed.payload["metadata"]["execution_isolation"] == "host"
    assert completed.item_id == next(event.item_id for event in events if event.type == RunEventType.TOOL_REQUESTED)
    continuation = backend.session.inputs[1][0].result
    assert continuation.is_error is False
    assert continuation.metadata["exit_code"] == 0
