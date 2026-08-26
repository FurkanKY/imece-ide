import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import ModelStopReason, ModelTurn, ModelUsage  # noqa: E402
from agent_runtime.events import (  # noqa: E402
    ApprovalRequested,
    ExecutionCompleted,
    ExecutionFailed,
    ExecutionStarted,
    ModelCompleted,
    ModelStarted,
    TurnCompleted,
    TurnStarted,
)
from planner_runtime.errors import PlannerProtocolError, PlannerRecordingError  # noqa: E402
from planner_runtime.models import PlanReport, PlanStep, TaskComplexity, TaskProfile, TaskScope  # noqa: E402
from planner_runtime.parser import MAX_MODEL_OUTPUT_CHARS  # noqa: E402
from planner_runtime.runner import PlannerRunner  # noqa: E402
from run_runtime import (  # noqa: E402
    CanonicalPlannerEventSink,
    RunEventType,
    RunRuntime,
    RunStatus,
    RunStore,
)
from run_runtime.completion import RunCompletionGate  # noqa: E402
from tool_runtime.models import PermissionRequest  # noqa: E402
from workspace.local import LocalWorkspace  # noqa: E402


class ScriptedSession:
    def __init__(self, turns):
        self.turns = list(turns)

    def respond(self, input_items):
        value = self.turns.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ScriptedBackend:
    def __init__(self, turns):
        self.session = ScriptedSession(turns)

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        return self.session


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def event_types(runtime, run):
    return [event.type for event in runtime.events(run.run_id, limit=200).events]


def append_worker_execution(runtime, run_id, execution_id):
    runtime.record(
        run_id=run_id, type=RunEventType.EXECUTION_STARTED, payload={"task": "do it"},
        execution_id=execution_id, correlation_id=execution_id, source="native_agent",
    )
    runtime.record(
        run_id=run_id, type=RunEventType.EXECUTION_COMPLETED, payload={"final_text": "done"},
        execution_id=execution_id, correlation_id=execution_id, source="native_agent",
    )


def append_verification(runtime, run_id, verification_id, status="pass"):
    from run_runtime import RunEventSpec

    runtime.record_many(run_id=run_id, specs=(
        RunEventSpec(
            type=RunEventType.VERIFICATION_STARTED,
            payload={"verification_id": verification_id, "plan_id": "plan", "check_count": 1},
            correlation_id=verification_id, source="verification",
        ),
        RunEventSpec(
            type=RunEventType.VERIFICATION_COMPLETED,
            payload={
                "verification_id": verification_id, "plan_id": "plan", "status": status,
                "duration_ms": 1,
                "counts": {"pass": 1, "fail": 0, "timeout": 0, "error": 0, "total": 1},
            },
            correlation_id=verification_id, source="verification",
        ),
    ))


# ---------------- direct event-mapping tests ----------------


def test_execution_started_maps_to_plan_started_without_full_prompt(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_plan-1", "GIANT PROMPT " * 1000))
    events = runtime.events(run.run_id, limit=200).events
    started = events[-1]
    assert started.type == RunEventType.PLAN_STARTED
    assert started.execution_id is None
    assert started.correlation_id == "plan-1"
    assert started.source == "planner"
    # task_sha256 has exactly one authority (PlannerRunner -> PlanReport ->
    # plan.completed); plan.started never carries it, and the sink cannot
    # even accept one — see test_sink_constructor_does_not_accept_task_sha256.
    assert started.payload == {"plan_id": "plan-1"}


def test_plan_started_never_contains_a_task_sha256_field(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_plan-1", "prompt"))
    started = runtime.events(run.run_id, limit=200).events[-1]
    assert "task_sha256" not in started.payload


def test_sink_constructor_does_not_accept_task_sha256(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    with pytest.raises(TypeError):
        CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1", task_sha256="a" * 64)


def test_execution_completed_does_not_persist_plan_completed(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_plan-1", "prompt"))
    before = event_types(runtime, run)
    sink.emit(ExecutionCompleted("planner_exec_plan-1", '{"summary":"x"}', 1, 0, 0, 1, 1, None))
    after = event_types(runtime, run)
    assert after == before
    assert RunEventType.PLAN_COMPLETED not in after


def test_execution_failed_maps_to_plan_failed_terminal(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_plan-1", "prompt"))
    sink.emit(ExecutionFailed("planner_exec_plan-1", "AgentBackendError", "transport down"))
    events = runtime.events(run.run_id, limit=200).events
    failed = events[-1]
    assert failed.type == RunEventType.PLAN_FAILED
    assert failed.execution_id is None
    assert failed.payload["plan_id"] == "plan-1"
    assert failed.payload["error_type"] == "AgentBackendError"
    assert RunEventType.PLAN_COMPLETED not in event_types(runtime, run)
    assert RunEventType.RUN_FAILED not in event_types(runtime, run)


def test_turn_model_tool_usage_events_are_execution_id_none_and_planner_scoped(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_plan-1", "prompt"))
    sink.emit(TurnStarted("planner_exec_plan-1", 1, "turn-1"))
    sink.emit(ModelStarted("planner_exec_plan-1", 1, "turn-1", "item-1"))
    sink.emit(_model_completed("plan text", execution_id="planner_exec_plan-1"))
    sink.emit(TurnCompleted("planner_exec_plan-1", 1, "turn-1"))
    usage_seen = False
    for event in runtime.events(run.run_id, limit=200).events:
        if event.type in (RunEventType.RUN_CREATED, RunEventType.RUN_STARTED):
            continue
        assert event.execution_id is None
        assert event.source == "planner"
        assert event.correlation_id == "plan-1"
        if event.type == RunEventType.USAGE_RECORDED:
            usage_seen = True
    assert usage_seen


def test_approval_requested_never_produces_run_waiting_user(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_plan-1", "prompt"))
    sink.emit(TurnStarted("planner_exec_plan-1", 1, "turn-1"))
    sink.emit(ApprovalRequested(
        "planner_exec_plan-1", 1, "turn-1", "item-1", "call-1", "write_file", "fp", (PermissionRequest("edit", "x.py"),),
    ))
    types = event_types(runtime, run)
    assert RunEventType.RUN_WAITING_USER not in types
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    assert RunEventType.PERMISSION_REQUESTED in types


def test_mismatched_transient_execution_id_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_plan-1", "prompt"))
    with pytest.raises(ValueError):
        sink.emit(TurnStarted("some_other_exec_id", 1, "turn-1"))


def test_plan_id_reuse_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    first = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    first.emit(ExecutionStarted("planner_exec_1", "prompt"))
    second = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    with pytest.raises(ValueError):
        second.emit(ExecutionStarted("planner_exec_2", "prompt"))


def test_duplicate_terminal_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_plan-1", "prompt"))
    sink.emit(ExecutionFailed("planner_exec_plan-1", "Boom", "failed"))
    with pytest.raises(PlannerRecordingError):
        sink.fail("plan-1", "Boom", "failed again")


def test_sink_requires_running_run(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_CANCELLED, payload={})
    with pytest.raises(ValueError):
        CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")


# ---------------- end-to-end via PlannerRunner ----------------


def _plan_json():
    return (
        '{"summary":"Do it.","steps":[{"title":"Step 1","objective":"Do the first part."}],'
        '"acceptance_criteria":["tests pass"],"risks":[],'
        '"task_profile":{"complexity":"LOW","scope":"LOCAL"}}'
    )


def test_end_to_end_valid_plan_via_runner_produces_expected_lifecycle(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([
        ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage(5, 5)),
    ])
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-e2e")
    report = PlannerRunner(backend).run(LocalWorkspace(tmp_path), "Implement X", recorder=sink, plan_id="plan-e2e")
    assert report.summary == "Do it."
    types = event_types(runtime, run)
    assert types[1:] == [
        RunEventType.PLAN_STARTED,
        RunEventType.TURN_STARTED,
        RunEventType.MODEL_STARTED,
        RunEventType.MODEL_COMPLETED,
        RunEventType.USAGE_RECORDED,
        RunEventType.TURN_COMPLETED,
        RunEventType.PLAN_COMPLETED,
    ]
    events = runtime.events(run.run_id, limit=200).events
    planner_events = [event for event in events if event.source == "planner"]
    assert all(event.execution_id is None for event in planner_events)
    assert all(event.correlation_id == "plan-e2e" for event in planner_events)
    completed = events[-1]
    assert completed.payload["summary"] == "Do it."
    assert completed.payload["steps"] == [{"title": "Step 1", "objective": "Do the first part."}]
    assert completed.payload["task_profile"] == {"complexity": "LOW", "scope": "LOCAL"}
    assert completed.payload["task_sha256"] == report.task_sha256
    assert completed.payload["repository_fingerprint"] == report.repository_fingerprint


def test_task_sha256_has_a_single_authority_end_to_end(tmp_path):
    """original task -> PlannerRunner-computed SHA -> PlanReport.task_sha256
    -> plan.completed.payload["task_sha256"]: one chain, one value, proven
    against an independently computed hashlib reference — not merely
    self-consistent with whatever PlannerRunner happened to compute."""
    import hashlib

    runtime, run = setup_runtime(tmp_path)
    task = "Implement the rate limiter feature exactly as specified."
    expected_sha = hashlib.sha256(task.encode("utf-8")).hexdigest()
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-sha")

    report = PlannerRunner(backend).run(LocalWorkspace(tmp_path), task, recorder=sink, plan_id="plan-sha")

    assert report.task_sha256 == expected_sha
    events = runtime.events(run.run_id, limit=200).events
    started = next(e for e in events if e.type == RunEventType.PLAN_STARTED)
    assert "task_sha256" not in started.payload
    completed = next(e for e in events if e.type == RunEventType.PLAN_COMPLETED)
    assert completed.payload["task_sha256"] == expected_sha == report.task_sha256


def test_end_to_end_malformed_output_produces_plan_failed_not_completed(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([
        ModelTurn("SUMMARY: not json", (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-bad")
    with pytest.raises(PlannerProtocolError):
        PlannerRunner(backend).run(LocalWorkspace(tmp_path), "Implement X", recorder=sink, plan_id="plan-bad")
    events = runtime.events(run.run_id, limit=200).events
    assert events[-1].type == RunEventType.PLAN_FAILED
    assert RunEventType.PLAN_COMPLETED not in [event.type for event in events]
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


# ---------------- plan.completed / plan.failed never terminate the Run ----------------


def test_plan_completed_does_not_terminate_run(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    PlannerRunner(backend).run(LocalWorkspace(tmp_path), "Implement X", recorder=sink, plan_id="plan-1")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


def test_plan_failed_does_not_terminate_run(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([ModelTurn("not json", (), ModelStopReason.COMPLETED, ModelUsage())])
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    with pytest.raises(PlannerProtocolError):
        PlannerRunner(backend).run(LocalWorkspace(tmp_path), "Implement X", recorder=sink, plan_id="plan-1")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


# ---------------- RunCompletionGate regression: planner activity never counts as execution activity ----------------


def test_planner_activity_does_not_make_verification_stale_for_completion_gate(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    append_worker_execution(runtime, run.run_id, "exec-1")
    append_verification(runtime, run.run_id, "ver-1", "pass")

    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-gate")
    PlannerRunner(backend).run(LocalWorkspace(tmp_path), "Implement X", recorder=sink, plan_id="plan-gate")

    before_completion = runtime.events(run.run_id, limit=200).events
    execution_lifecycle_types = {
        RunEventType.EXECUTION_STARTED, RunEventType.EXECUTION_COMPLETED, RunEventType.EXECUTION_FAILED,
    }
    planner_events = [event for event in before_completion if event.source == "planner"]
    assert planner_events, "planner must have produced canonical events"
    assert all(event.type not in execution_lifecycle_types for event in planner_events)
    assert all(event.execution_id is None for event in planner_events)

    RunCompletionGate(runtime).complete_verified(run.run_id, verification_id="ver-1")
    completed = runtime.get_run(run.run_id)
    assert completed.status is RunStatus.SUCCEEDED
    assert runtime.events(run.run_id, limit=200).events[-1].type == RunEventType.RUN_COMPLETED


# ---------------- plan_id validation at the canonical bridge ----------------


@pytest.mark.parametrize(
    "bad_plan_id",
    ["", "has a space", "x" * 129, "../evil", "plan/1", "plan\x00id"],
)
def test_sink_construction_rejects_invalid_plan_id(tmp_path, bad_plan_id):
    runtime, run = setup_runtime(tmp_path)
    with pytest.raises(Exception):
        CanonicalPlannerEventSink(runtime, run.run_id, plan_id=bad_plan_id)


def test_sink_construction_accepts_bounded_stable_plan_id(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan_" + "a" * 30)
    assert sink.plan_id == "plan_" + "a" * 30


# ---------------- terminal uniqueness is canonical, not process-local ----------------


def _report(plan_id="plan-1"):
    return PlanReport(
        plan_id=plan_id, summary="ok",
        steps=(PlanStep(title="t", objective="o"),),
        acceptance_criteria=(), risks=(),
        task_profile=TaskProfile(complexity=TaskComplexity.LOW, scope=TaskScope.LOCAL),
        repository_fingerprint="a" * 64, task_sha256="b" * 64,
    )


def test_complete_before_execution_started_is_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    with pytest.raises(PlannerRecordingError):
        sink.complete(_report())
    assert event_types(runtime, run) == [RunEventType.RUN_STARTED]


def test_fail_before_execution_started_is_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    with pytest.raises(PlannerRecordingError):
        sink.fail("plan-1", "SomeError", "boom")
    assert event_types(runtime, run) == [RunEventType.RUN_STARTED]


def test_second_sink_with_reused_plan_id_cannot_fail_without_its_own_start(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    first = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    first.emit(ExecutionStarted("planner_exec_1", "prompt"))
    first.emit(ExecutionCompleted("planner_exec_1", "{}", 1, 0, 0, 1, 1, None))
    first.complete(_report())

    second = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    with pytest.raises(PlannerRecordingError):
        second.fail("plan-1", "SomeError", "boom")

    events = runtime.events(run.run_id, limit=200).events
    terminals = [e for e in events if e.type in (
        RunEventType.PLAN_COMPLETED, RunEventType.PLAN_FAILED, RunEventType.PLAN_INTERRUPTED,
    )]
    assert len(terminals) == 1
    assert terminals[0].type == RunEventType.PLAN_COMPLETED


def test_second_sink_with_reused_plan_id_cannot_complete_after_first_failed(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    first = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    first.emit(ExecutionStarted("planner_exec_1", "prompt"))
    first.emit(ExecutionFailed("planner_exec_1", "Boom", "backend down"))

    second = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    with pytest.raises(PlannerRecordingError):
        second.complete(_report())

    events = runtime.events(run.run_id, limit=200).events
    terminals = [e for e in events if e.type in (
        RunEventType.PLAN_COMPLETED, RunEventType.PLAN_FAILED, RunEventType.PLAN_INTERRUPTED,
    )]
    assert len(terminals) == 1
    assert terminals[0].type == RunEventType.PLAN_FAILED


def test_complete_without_observed_execution_completed_is_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_1", "prompt"))
    sink.emit(TurnStarted("planner_exec_1", 1, "turn-1"))
    with pytest.raises(PlannerRecordingError):
        sink.complete(_report())
    events = runtime.events(run.run_id, limit=200).events
    assert RunEventType.PLAN_COMPLETED not in [e.type for e in events]


def test_complete_after_started_and_execution_completed_succeeds(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_1", "prompt"))
    sink.emit(ExecutionCompleted("planner_exec_1", "{}", 1, 0, 0, 1, 1, None))
    sink.complete(_report())
    events = runtime.events(run.run_id, limit=200).events
    assert events[-1].type == RunEventType.PLAN_COMPLETED


# ---------------- bound planner model text before canonical storage ----------------


def _model_completed(text, *, turn_index=1, turn_id="turn-1", item_id="item-1", execution_id="planner_exec_1"):
    return ModelCompleted(
        execution_id, turn_index, turn_id, item_id, text, (), ModelStopReason.COMPLETED.value, ModelUsage(1, 1),
    )


def test_oversized_model_completed_text_is_bounded_in_canonical_storage(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_1", "prompt"))
    sink.emit(TurnStarted("planner_exec_1", 1, "turn-1"))
    sink.emit(ModelStarted("planner_exec_1", 1, "turn-1", "item-1"))
    oversized = "x" * (MAX_MODEL_OUTPUT_CHARS + 5000)
    sink.emit(_model_completed(oversized))

    events = runtime.events(run.run_id, limit=200).events
    completed = next(e for e in events if e.type == RunEventType.MODEL_COMPLETED)
    assert len(completed.payload["text"]) <= MAX_MODEL_OUTPUT_CHARS
    assert completed.payload["text_truncated"] is True
    assert completed.payload["text"] == oversized[:MAX_MODEL_OUTPUT_CHARS]


def test_ordinary_model_completed_text_is_preserved_exactly(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_1", "prompt"))
    sink.emit(TurnStarted("planner_exec_1", 1, "turn-1"))
    sink.emit(ModelStarted("planner_exec_1", 1, "turn-1", "item-1"))
    sink.emit(_model_completed(_plan_json()))

    completed = next(e for e in runtime.events(run.run_id, limit=200).events if e.type == RunEventType.MODEL_COMPLETED)
    assert completed.payload["text"] == _plan_json()
    assert completed.payload["text_truncated"] is False


def test_oversized_final_answer_end_to_end_bounds_storage_but_rejects_plan(tmp_path):
    """Full PlannerRunner path: canonical model.completed storage is bounded,
    but the actual oversized AgentOutcome.final_text still reaches the
    parser unchanged and is rejected -> plan.failed, never plan.completed,
    and the Run stays non-terminal. Oversized model output must never be
    truncated before the parser sees it — the parser is what rejects it."""
    runtime, run = setup_runtime(tmp_path)
    oversized = "x" * (MAX_MODEL_OUTPUT_CHARS + 1)
    backend = ScriptedBackend([ModelTurn(oversized, (), ModelStopReason.COMPLETED, ModelUsage())])
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-oversized")

    with pytest.raises(PlannerProtocolError):
        PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t", recorder=sink, plan_id="plan-oversized")

    events = runtime.events(run.run_id, limit=200).events
    model_completed = next(e for e in events if e.type == RunEventType.MODEL_COMPLETED)
    assert len(model_completed.payload["text"]) <= MAX_MODEL_OUTPUT_CHARS
    assert model_completed.payload["text_truncated"] is True

    assert events[-1].type == RunEventType.PLAN_FAILED
    assert RunEventType.PLAN_COMPLETED not in [e.type for e in events]
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


# ---------------- transient planner lifecycle ordering ----------------


def test_execution_completed_before_started_is_rejected_and_never_arms_completion_flag(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")

    with pytest.raises(PlannerRecordingError):
        sink.emit(ExecutionCompleted("planner_exec_1", "{}", 1, 0, 0, 1, 1, None))
    assert event_types(runtime, run) == [RunEventType.RUN_STARTED]

    sink.emit(ExecutionStarted("planner_exec_1", "prompt"))
    with pytest.raises(PlannerRecordingError):
        sink.complete(_report())
    events = runtime.events(run.run_id, limit=200).events
    assert RunEventType.PLAN_COMPLETED not in [e.type for e in events]


def test_turn_started_before_execution_started_is_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    with pytest.raises(PlannerRecordingError):
        sink.emit(TurnStarted("planner_exec_1", 1, "turn-1"))
    assert event_types(runtime, run) == [RunEventType.RUN_STARTED]


def test_lifecycle_event_after_plan_completed_is_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_1", "prompt"))
    sink.emit(ExecutionCompleted("planner_exec_1", "{}", 1, 0, 0, 1, 1, None))
    sink.complete(_report())

    before = event_types(runtime, run)
    with pytest.raises(PlannerRecordingError):
        sink.emit(TurnStarted("planner_exec_1", 1, "turn-1"))
    after = event_types(runtime, run)
    assert after == before
    assert after[-1] == RunEventType.PLAN_COMPLETED


def test_lifecycle_event_after_plan_failed_is_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_1", "prompt"))
    sink.emit(ExecutionFailed("planner_exec_1", "X", "y"))

    before = event_types(runtime, run)
    with pytest.raises(PlannerRecordingError):
        sink.emit(ModelStarted("planner_exec_1", 1, "turn-1", "item-1"))
    after = event_types(runtime, run)
    assert after == before
    assert after[-1] == RunEventType.PLAN_FAILED


def test_normal_happy_path_lifecycle_ordering_unaffected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    sink.emit(ExecutionStarted("planner_exec_1", "prompt"))
    sink.emit(TurnStarted("planner_exec_1", 1, "turn-1"))
    sink.emit(ModelStarted("planner_exec_1", 1, "turn-1", "item-1"))
    sink.emit(_model_completed(_plan_json()))
    sink.emit(TurnCompleted("planner_exec_1", 1, "turn-1"))
    sink.emit(ExecutionCompleted("planner_exec_1", "{}", 1, 0, 0, 1, 1, None))
    sink.complete(_report())
    assert event_types(runtime, run)[-1] == RunEventType.PLAN_COMPLETED


# ---------------- planner never emits canonical execution.* ----------------


def test_planner_never_emits_canonical_execution_events(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    sink = CanonicalPlannerEventSink(runtime, run.run_id, plan_id="plan-1")
    PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t", recorder=sink, plan_id="plan-1")
    execution_lifecycle_types = {
        RunEventType.EXECUTION_STARTED, RunEventType.EXECUTION_COMPLETED, RunEventType.EXECUTION_FAILED,
    }
    types = event_types(runtime, run)
    assert not (execution_lifecycle_types & set(types))
