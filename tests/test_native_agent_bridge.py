import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import (  # noqa: E402
    AgentSession,
    ApprovalDecision,
    ModelStopReason,
    ModelToolCall,
    ModelTurn,
    ModelUsage,
)
from agent_runtime.errors import AgentBackendError, AgentRecordingError  # noqa: E402
from agent_runtime.events import (  # noqa: E402
    ModelStarted,
    ToolCompleted,
    ToolStarted,
)
from run_runtime import (  # noqa: E402
    CanonicalAgentEventSink,
    RunEventSpec,
    RunEventType,
    RunRuntime,
    RunStatus,
    RunStore,
    recover_running_runs,
)
from run_runtime.bus import EventBus  # noqa: E402
from run_runtime.errors import EventSequenceError, RunProjectionError  # noqa: E402
from tool_runtime import (  # noqa: E402
    PermissionEffect,
    PermissionRule,
    PolicyEvaluator,
    ToolExecutionContext,
    ToolRegistry,
)
from tool_runtime.tools.workspace_files import register_workspace_tools  # noqa: E402
from workspace.local import LocalWorkspace  # noqa: E402


class ScriptedSession:
    def __init__(self, turns):
        self.turns = list(turns)
        self.inputs = []

    def respond(self, input_items):
        self.inputs.append(input_items)
        if not self.turns:
            raise AssertionError("script exhausted")
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        return turn


class ScriptedBackend:
    def __init__(self, turns):
        self.session = ScriptedSession(turns)

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        return self.session


class FailingSink:
    def __init__(self, event_type):
        self.event_type = event_type
        self.events = []

    def emit(self, event):
        self.events.append(event)
        if isinstance(event, self.event_type):
            raise RuntimeError("recording failed")


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def run_session(runtime, run, workspace, backend, policy=None, sink=None):
    registry = ToolRegistry()
    register_workspace_tools(registry)
    policy = policy or PolicyEvaluator([PermissionRule("*", "*", PermissionEffect.ALLOW)])
    sink = sink or CanonicalAgentEventSink(runtime, run.run_id, execution_id="exec_test")
    return AgentSession(
        backend=backend,
        registry=registry,
        policy=policy,
        context=ToolExecutionContext(workspace, run_id=run.run_id, execution_id="exec_test"),
        event_sink=sink,
        execution_id="exec_test",
    )


def event_types(runtime, run):
    return [event.type for event in runtime.events(run.run_id, limit=200).events]


def test_record_many_is_atomic_and_publishes_latest_seq_after_commit(tmp_path):
    notices = []

    class SpyBus(EventBus):
        def publish(self, notice):
            notices.append(notice)
            assert runtime.store.get_run(run.run_id).last_event_seq == notice.latest_seq
            super().publish(notice)

    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"), bus=SpyBus())
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    events, projected = runtime.record_many(
        run_id=run.run_id,
        specs=(
            RunEventSpec(RunEventType.RUN_STARTED, {}),
            RunEventSpec(RunEventType.RUN_PHASE_CHANGED, {"phase": "executing"}),
        ),
        expected_last_event_seq=0,
    )
    assert [event.seq for event in events] == [1, 2]
    assert projected.last_event_seq == 2
    assert projected.status is RunStatus.RUNNING
    assert projected.phase.value == "executing"
    assert [(notice.run_id, notice.latest_seq) for notice in notices] == [(run.run_id, 2)]


def test_record_many_rolls_back_on_second_projection_failure(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    before = runtime.store.get_run(run.run_id)
    with pytest.raises(RunProjectionError):
        runtime.record_many(
            run_id=run.run_id,
            specs=(
                RunEventSpec("future.event", {"ok": True}),
                RunEventSpec(RunEventType.RUN_PHASE_CHANGED, {"phase": "invalid"}),
            ),
            expected_last_event_seq=before.last_event_seq,
        )
    assert runtime.store.get_run(run.run_id).last_event_seq == before.last_event_seq
    assert len(runtime.events(run.run_id, limit=200).events) == 1


def test_record_many_rejects_stale_whole_batch(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    runtime.record(run_id=run.run_id, type="future.one", payload={})
    with pytest.raises(EventSequenceError):
        runtime.record_many(
            run_id=run.run_id,
            specs=(RunEventSpec("future.a", {}), RunEventSpec("future.b", {})),
            expected_last_event_seq=1,
        )
    assert runtime.store.get_run(run.run_id).last_event_seq == 2


def test_text_only_agent_is_canonically_completed_before_return(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    session = run_session(
        runtime,
        run,
        LocalWorkspace(tmp_path),
        ScriptedBackend([ModelTurn("finished", (), ModelStopReason.COMPLETED, ModelUsage(2, 3))]),
    )
    outcome = session.start("do it")
    assert outcome.final_text == "finished"
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    assert event_types(runtime, run)[1:] == [
        RunEventType.EXECUTION_STARTED,
        RunEventType.TURN_STARTED,
        RunEventType.MODEL_STARTED,
        RunEventType.MODEL_COMPLETED,
        RunEventType.USAGE_RECORDED,
        RunEventType.TURN_COMPLETED,
        RunEventType.EXECUTION_COMPLETED,
    ]
    events = runtime.events(run.run_id, limit=200).events
    assert len({event.execution_id for event in events[1:]}) == 1
    assert len({event.turn_id for event in events if event.turn_id}) == 1


def test_real_workspace_tools_emit_tool_lifecycle_and_metadata(tmp_path):
    (tmp_path / "input.txt").write_text("source", encoding="utf-8")
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("read-1", "read_file", {"path": "input.txt"}),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn("", (ModelToolCall("write-1", "write_file", {"path": "output.txt", "content": "changed"}),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn("done", (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    outcome = run_session(runtime, run, LocalWorkspace(tmp_path), backend).start("change")
    assert outcome.final_text == "done"
    assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "changed"
    events = runtime.events(run.run_id, limit=200).events
    for call_id in ("read-1", "write-1"):
        lifecycle = [event for event in events if event.payload.get("call_id") == call_id]
        assert [event.type for event in lifecycle] == [
            RunEventType.TOOL_REQUESTED,
            RunEventType.TOOL_STARTED,
            RunEventType.TOOL_COMPLETED,
        ]
        assert len({event.item_id for event in lifecycle}) == 1
    read_completed = next(event for event in events if event.type == RunEventType.TOOL_COMPLETED and event.payload["call_id"] == "read-1")
    assert "metadata" in read_completed.payload


def test_recoverable_tool_failure_is_canonical_without_tool_started(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("bad", "missing_tool", {}),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn("recovered", (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    outcome = run_session(runtime, run, LocalWorkspace(tmp_path), backend).start("recover")
    assert outcome.final_text == "recovered"
    events = runtime.events(run.run_id, limit=200).events
    bad = [event.type for event in events if event.payload.get("call_id") == "bad"]
    assert bad == [RunEventType.TOOL_REQUESTED, RunEventType.TOOL_FAILED]


def test_approval_bridge_waits_then_resumes_atomically(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    policy = PolicyEvaluator([PermissionRule("edit", "x.txt", PermissionEffect.ASK)])
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("write", "write_file", {"path": "x.txt", "content": "x"}),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn("done", (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    session = run_session(runtime, run, LocalWorkspace(tmp_path), backend, policy)
    pause = session.start("write")
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER
    assert not (tmp_path / "x.txt").exists()
    before = event_types(runtime, run)
    assert RunEventType.PERMISSION_REQUESTED in before
    assert RunEventType.RUN_WAITING_USER in before
    assert RunEventType.TOOL_STARTED not in before
    with pytest.raises(Exception):
        session.resume(ApprovalDecision("wrong", pause.approval_fingerprint, True, pause.session_id))
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER
    outcome = session.resume(ApprovalDecision(pause.call_id, pause.approval_fingerprint, True, pause.session_id))
    assert outcome.final_text == "done"
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "x"
    types = event_types(runtime, run)
    assert types.index(RunEventType.PERMISSION_RESOLVED) < types.index(RunEventType.RUN_RESUMED) < types.index(RunEventType.TOOL_STARTED)
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


def test_approval_denial_is_canonical_and_model_recovers(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    policy = PolicyEvaluator([PermissionRule("edit", "x.txt", PermissionEffect.ASK)])
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("write", "write_file", {"path": "x.txt", "content": "x"}),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn("continued", (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    session = run_session(runtime, run, LocalWorkspace(tmp_path), backend, policy)
    pause = session.start("write")
    before = runtime.events(run.run_id, limit=200).events
    requested = next(event for event in before if event.type == RunEventType.PERMISSION_REQUESTED)
    outcome = session.resume(
        ApprovalDecision(pause.call_id, pause.approval_fingerprint, False, pause.session_id)
    )
    assert outcome.final_text == "continued"
    assert not (tmp_path / "x.txt").exists()
    result = backend.session.inputs[1][0].result
    assert result.call_id == "write"
    assert result.is_error is True
    events = runtime.events(run.run_id, limit=200).events
    resolved_index = next(i for i, event in enumerate(events) if event.type == RunEventType.PERMISSION_RESOLVED)
    resumed_index = next(i for i, event in enumerate(events) if event.type == RunEventType.RUN_RESUMED)
    failed = next(event for event in events if event.type == RunEventType.TOOL_FAILED and event.payload["call_id"] == "write")
    assert events[resolved_index].payload["approved"] is False
    assert resolved_index < resumed_index < events.index(failed)
    assert failed.payload["stage"] == "approval"
    assert failed.payload["recoverable"] is True
    assert failed.item_id == requested.item_id
    assert not any(event.type == RunEventType.TOOL_STARTED and event.payload.get("call_id") == "write" for event in events)
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


@pytest.mark.parametrize("invalid_return", [object(), {"text": "not a ModelTurn"}])
def test_invalid_model_return_records_model_failed_before_execution_failed(tmp_path, invalid_return):
    runtime, run = setup_runtime(tmp_path)
    session = run_session(
        runtime,
        run,
        LocalWorkspace(tmp_path),
        ScriptedBackend([invalid_return]),
    )
    from agent_runtime.errors import AgentProtocolError

    with pytest.raises(AgentProtocolError):
        session.start("invalid")
    events = runtime.events(run.run_id, limit=200).events
    types = [event.type for event in events]
    model_started = types.index(RunEventType.MODEL_STARTED)
    assert types[model_started:model_started + 4] == [
        RunEventType.MODEL_STARTED,
        RunEventType.MODEL_FAILED,
        RunEventType.EXECUTION_FAILED,
    ]
    assert RunEventType.MODEL_COMPLETED not in types
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


def test_internal_prepare_failure_closes_tool_lifecycle(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("same", "read_file", {"path": "a.txt"}),), ModelStopReason.TOOL_USE, ModelUsage()),
    ])
    session = run_session(runtime, run, LocalWorkspace(tmp_path), backend)
    from agent_runtime.errors import AgentToolRuntimeError
    from tool_runtime.errors import ToolPreparedCallError

    session.dispatcher.prepare = lambda *_args: (_ for _ in ()).throw(ToolPreparedCallError("invariant"))

    with pytest.raises(AgentToolRuntimeError):
        session.start("duplicate")
    events = runtime.events(run.run_id, limit=200).events
    failed = [event for event in events if event.type == RunEventType.TOOL_FAILED and event.payload.get("call_id") == "same"]
    assert len(failed) == 1
    assert failed[0].payload["recoverable"] is False
    assert failed[0].payload["stage"] == "prepare"
    assert failed[0].item_id == next(event.item_id for event in events if event.type == RunEventType.TOOL_REQUESTED and event.payload.get("call_id") == "same")
    assert RunEventType.TOOL_STARTED not in [event.type for event in events if event.payload.get("call_id") == "same"]
    assert events[-1].type == RunEventType.EXECUTION_FAILED
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


def test_internal_execute_failure_closes_started_tool_lifecycle(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("call", "read_file", {"path": "a.txt"}),), ModelStopReason.TOOL_USE, ModelUsage()),
    ])
    session = run_session(runtime, run, LocalWorkspace(tmp_path), backend)
    from agent_runtime.errors import AgentToolRuntimeError
    from tool_runtime.errors import ToolCallConsumedError

    session.dispatcher.execute = lambda *_args: (_ for _ in ()).throw(ToolCallConsumedError("invariant"))
    with pytest.raises(AgentToolRuntimeError):
        session.start("execute invariant")
    events = runtime.events(run.run_id, limit=200).events
    lifecycle = [event for event in events if event.payload.get("call_id") == "call"]
    assert [event.type for event in lifecycle] == [
        RunEventType.TOOL_REQUESTED,
        RunEventType.TOOL_STARTED,
        RunEventType.TOOL_FAILED,
    ]
    assert lifecycle[-1].payload["recoverable"] is False
    assert lifecycle[-1].payload["stage"] == "execute"
    assert len({event.item_id for event in lifecycle}) == 1
    assert events[-1].type == RunEventType.EXECUTION_FAILED
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


def test_provider_failure_records_model_and_execution_failure(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([RuntimeError("transport")])
    session = run_session(runtime, run, LocalWorkspace(tmp_path), backend)
    with pytest.raises(AgentBackendError) as error:
        session.start("fail")
    assert isinstance(error.value.__cause__, RuntimeError)
    types = event_types(runtime, run)
    assert types[-4:] == [
        RunEventType.TURN_STARTED,
        RunEventType.MODEL_STARTED,
        RunEventType.MODEL_FAILED,
        RunEventType.EXECUTION_FAILED,
    ]
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


def test_sink_failure_before_model_or_tool_side_effect_fails_closed(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    model_sink = FailingSink(ModelStarted)
    backend = ScriptedBackend([ModelTurn("never", (), ModelStopReason.COMPLETED, ModelUsage())])
    session = run_session(runtime, run, LocalWorkspace(tmp_path), backend, sink=model_sink)
    with pytest.raises(AgentRecordingError):
        session.start("blocked")
    assert backend.session.inputs == []

    runtime, run = setup_runtime(tmp_path / "tool")
    tool_sink = FailingSink(ToolStarted)
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("write", "write_file", {"path": "x.txt", "content": "x"}),), ModelStopReason.TOOL_USE, ModelUsage()),
    ])
    session = run_session(runtime, run, LocalWorkspace(tmp_path / "tool"), backend, sink=tool_sink)
    with pytest.raises(AgentRecordingError):
        session.start("blocked tool")
    assert not (tmp_path / "tool" / "x.txt").exists()


def test_sink_failure_after_tool_execution_does_not_reexecute(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = FailingSink(ToolCompleted)
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("write", "write_file", {"path": "x.txt", "content": "x"}),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn("must not receive", (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    session = run_session(runtime, run, LocalWorkspace(tmp_path), backend, sink=sink)
    with pytest.raises(AgentRecordingError):
        session.start("write")
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "x"
    assert len(backend.session.inputs) == 1


def test_recovery_marks_unfinished_tool_outcome_unknown(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    runtime.record_many(
        run_id=run.run_id,
        specs=(
            RunEventSpec(RunEventType.TOOL_REQUESTED, {"call_id": "c", "tool_name": "write_file", "arguments": {}}, execution_id="e", turn_id="t", item_id="i"),
            RunEventSpec(RunEventType.TOOL_STARTED, {"call_id": "c", "tool_name": "write_file"}, execution_id="e", turn_id="t", item_id="i"),
        ),
        expected_last_event_seq=runtime.get_run(run.run_id).last_event_seq,
    )
    report = recover_running_runs(runtime)
    assert run.run_id in report.interrupted_run_ids
    events = runtime.events(run.run_id, limit=200).events
    interrupted = [event for event in events if event.type == RunEventType.TOOL_INTERRUPTED]
    assert len(interrupted) == 1
    assert interrupted[0].payload["outcome_unknown"] is True
    assert events[-1].type == RunEventType.RUN_INTERRUPTED
    assert runtime.get_run(run.run_id).status is RunStatus.INTERRUPTED
