import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import ModelStopReason, ModelTurn, ModelUsage  # noqa: E402
from agent_runtime.errors import AgentRecordingError  # noqa: E402
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
from review_runtime.errors import ReviewInputError, ReviewProtocolError, ReviewRecordingError  # noqa: E402
from review_runtime.models import ReviewReport, ReviewRequest, ReviewVerdict  # noqa: E402
from review_runtime.parser import MAX_MODEL_OUTPUT_CHARS  # noqa: E402
from review_runtime.runner import ReviewerRunner  # noqa: E402
from run_runtime import (  # noqa: E402
    CanonicalReviewEventSink,
    RunEventType,
    RunRuntime,
    RunStatus,
    RunStore,
)
from run_runtime.completion import RunCompletionGate  # noqa: E402
from run_runtime.errors import EventSequenceError  # noqa: E402
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


def test_execution_started_maps_to_review_started_without_full_prompt(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_rev-1", "GIANT PROMPT " * 1000))
    events = runtime.events(run.run_id, limit=200).events
    started = events[-1]
    assert started.type == RunEventType.REVIEW_STARTED
    assert started.execution_id is None
    assert started.correlation_id == "rev-1"
    assert started.source == "reviewer"
    assert started.payload == {"review_id": "rev-1"}


def test_execution_completed_does_not_persist_review_completed(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_rev-1", "prompt"))
    before = event_types(runtime, run)
    sink.emit(ExecutionCompleted("review_exec_rev-1", '{"verdict":"APPROVED"}', 1, 0, 0, 1, 1, None))
    after = event_types(runtime, run)
    assert after == before
    assert RunEventType.REVIEW_COMPLETED not in after


def test_execution_failed_maps_to_review_failed_terminal(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_rev-1", "prompt"))
    sink.emit(ExecutionFailed("review_exec_rev-1", "AgentBackendError", "transport down"))
    events = runtime.events(run.run_id, limit=200).events
    failed = events[-1]
    assert failed.type == RunEventType.REVIEW_FAILED
    assert failed.execution_id is None
    assert failed.payload["review_id"] == "rev-1"
    assert failed.payload["error_type"] == "AgentBackendError"
    assert RunEventType.REVIEW_COMPLETED not in event_types(runtime, run)
    assert RunEventType.RUN_FAILED not in event_types(runtime, run)


def test_turn_model_tool_events_are_execution_id_none_and_reviewer_scoped(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_rev-1", "prompt"))
    sink.emit(TurnStarted("review_exec_rev-1", 1, "turn-1"))
    sink.emit(ModelStarted("review_exec_rev-1", 1, "turn-1", "item-1"))
    sink.emit(TurnCompleted("review_exec_rev-1", 1, "turn-1"))
    for event in runtime.events(run.run_id, limit=200).events:
        if event.type in (RunEventType.RUN_CREATED, RunEventType.RUN_STARTED):
            continue
        assert event.execution_id is None
        assert event.source == "reviewer"
        assert event.correlation_id == "rev-1"


def test_approval_requested_never_produces_run_waiting_user(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_rev-1", "prompt"))
    sink.emit(TurnStarted("review_exec_rev-1", 1, "turn-1"))
    sink.emit(ApprovalRequested(
        "review_exec_rev-1", 1, "turn-1", "item-1", "call-1", "write_file", "fp", (PermissionRequest("edit", "x.py"),),
    ))
    types = event_types(runtime, run)
    assert RunEventType.RUN_WAITING_USER not in types
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    assert RunEventType.PERMISSION_REQUESTED in types


def test_mismatched_transient_execution_id_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_rev-1", "prompt"))
    with pytest.raises(ValueError):
        sink.emit(TurnStarted("some_other_exec_id", 1, "turn-1"))


def test_review_id_reuse_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    first = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    first.emit(ExecutionStarted("review_exec_1", "prompt"))
    second = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    with pytest.raises(ValueError):
        second.emit(ExecutionStarted("review_exec_2", "prompt"))


def test_duplicate_terminal_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_rev-1", "prompt"))
    sink.emit(ExecutionFailed("review_exec_rev-1", "Boom", "failed"))
    with pytest.raises(ReviewRecordingError):
        sink.fail("rev-1", "Boom", "failed again")


def test_sink_requires_running_run(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    RunCompletionGate  # keep import used
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_CANCELLED, payload={})
    with pytest.raises(ValueError):
        CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")


# ---------------- end-to-end via ReviewerRunner ----------------


def test_end_to_end_approved_review_via_runner_produces_expected_lifecycle(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([
        ModelTurn('{"verdict":"APPROVED","summary":"Looks correct.","findings":[]}', (), ModelStopReason.COMPLETED, ModelUsage(5, 5)),
    ])
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-e2e")
    request = ReviewRequest(task="Implement X", diff="+ added line\n")
    report = ReviewerRunner(backend).run(LocalWorkspace(tmp_path), request, recorder=sink, review_id="rev-e2e")
    assert report.verdict.value == "APPROVED"
    types = event_types(runtime, run)
    assert types[1:] == [
        RunEventType.REVIEW_STARTED,
        RunEventType.TURN_STARTED,
        RunEventType.MODEL_STARTED,
        RunEventType.MODEL_COMPLETED,
        RunEventType.USAGE_RECORDED,
        RunEventType.TURN_COMPLETED,
        RunEventType.REVIEW_COMPLETED,
    ]
    events = runtime.events(run.run_id, limit=200).events
    reviewer_events = [event for event in events if event.source == "reviewer"]
    assert all(event.execution_id is None for event in reviewer_events)
    assert all(event.correlation_id == "rev-e2e" for event in reviewer_events)
    completed = events[-1]
    assert completed.payload["verdict"] == "APPROVED"
    assert completed.payload["note"] == "Looks correct."
    assert completed.payload["findings"] == []
    assert completed.payload["diff_sha256"] == request.diff_sha256


def test_end_to_end_malformed_output_produces_review_failed_not_completed(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([
        ModelTurn("VERDICT: APPROVED", (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-bad")
    request = ReviewRequest(task="Implement X", diff="+ line\n")
    with pytest.raises(Exception):
        ReviewerRunner(backend).run(LocalWorkspace(tmp_path), request, recorder=sink, review_id="rev-bad")
    events = runtime.events(run.run_id, limit=200).events
    assert events[-1].type == RunEventType.REVIEW_FAILED
    assert RunEventType.REVIEW_COMPLETED not in [event.type for event in events]
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


# ---------------- RunCompletionGate regression: reviewer activity never counts as execution activity ----------------


def test_reviewer_activity_does_not_make_verification_stale_for_completion_gate(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    append_worker_execution(runtime, run.run_id, "exec-1")
    append_verification(runtime, run.run_id, "ver-1", "pass")

    backend = ScriptedBackend([
        ModelTurn('{"verdict":"APPROVED","summary":"ok","findings":[]}', (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-gate")
    request = ReviewRequest(task="Implement X", diff="+ line\n")
    ReviewerRunner(backend).run(LocalWorkspace(tmp_path), request, recorder=sink, review_id="rev-gate")

    before_completion = runtime.events(run.run_id, limit=200).events
    execution_lifecycle_types = {
        RunEventType.EXECUTION_STARTED, RunEventType.EXECUTION_COMPLETED, RunEventType.EXECUTION_FAILED,
    }
    reviewer_events = [event for event in before_completion if event.source == "reviewer"]
    assert reviewer_events, "reviewer must have produced canonical events"
    assert all(event.type not in execution_lifecycle_types for event in reviewer_events)
    assert all(event.execution_id is None for event in reviewer_events)

    RunCompletionGate(runtime).complete_verified(run.run_id, verification_id="ver-1")
    completed = runtime.get_run(run.run_id)
    assert completed.status is RunStatus.SUCCEEDED
    assert runtime.events(run.run_id, limit=200).events[-1].type == RunEventType.RUN_COMPLETED


# ---------------- hardening: review_id validation at the canonical bridge ----------------


@pytest.mark.parametrize(
    "bad_review_id",
    ["", "has a space", "x" * 129, "../evil", "rev/1", "rev\x00id"],
)
def test_sink_construction_rejects_invalid_review_id(tmp_path, bad_review_id):
    runtime, run = setup_runtime(tmp_path)
    with pytest.raises(Exception):
        CanonicalReviewEventSink(runtime, run.run_id, review_id=bad_review_id)


def test_sink_construction_accepts_bounded_stable_review_id(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev_" + "a" * 30)
    assert sink.review_id == "rev_" + "a" * 30


# ---------------- hardening: terminal uniqueness is canonical, not process-local ----------------


def _report(review_id="rev-1", verdict=ReviewVerdict.APPROVED, findings=()):
    return ReviewReport(
        review_id=review_id, verdict=verdict, summary="ok", findings=findings,
        repository_fingerprint="a" * 64, diff_sha256="b" * 64,
        verification_id=None, verification_status=None,
    )


def test_complete_before_execution_started_is_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    with pytest.raises(ReviewRecordingError):
        sink.complete(_report())
    assert event_types(runtime, run) == [RunEventType.RUN_STARTED]


def test_fail_before_execution_started_is_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    with pytest.raises(ReviewRecordingError):
        sink.fail("rev-1", "SomeError", "boom")
    assert event_types(runtime, run) == [RunEventType.RUN_STARTED]


def test_second_sink_with_reused_review_id_cannot_fail_without_its_own_start(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    first = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    first.emit(ExecutionStarted("review_exec_1", "prompt"))
    first.emit(ExecutionCompleted("review_exec_1", "{}", 1, 0, 0, 1, 1, None))
    first.complete(_report())

    second = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    with pytest.raises(ReviewRecordingError):
        second.fail("rev-1", "SomeError", "boom")

    events = runtime.events(run.run_id, limit=200).events
    terminals = [e for e in events if e.type in (
        RunEventType.REVIEW_COMPLETED, RunEventType.REVIEW_FAILED, RunEventType.REVIEW_INTERRUPTED,
    )]
    assert len(terminals) == 1
    assert terminals[0].type == RunEventType.REVIEW_COMPLETED


def test_second_sink_with_reused_review_id_cannot_complete_after_first_failed(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    first = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    first.emit(ExecutionStarted("review_exec_1", "prompt"))
    first.emit(ExecutionFailed("review_exec_1", "Boom", "backend down"))

    second = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    with pytest.raises(ReviewRecordingError):
        second.complete(_report())

    events = runtime.events(run.run_id, limit=200).events
    terminals = [e for e in events if e.type in (
        RunEventType.REVIEW_COMPLETED, RunEventType.REVIEW_FAILED, RunEventType.REVIEW_INTERRUPTED,
    )]
    assert len(terminals) == 1
    assert terminals[0].type == RunEventType.REVIEW_FAILED


def test_complete_without_observed_execution_completed_is_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_1", "prompt"))
    sink.emit(TurnStarted("review_exec_1", 1, "turn-1"))
    with pytest.raises(ReviewRecordingError):
        sink.complete(_report())
    events = runtime.events(run.run_id, limit=200).events
    assert RunEventType.REVIEW_COMPLETED not in [e.type for e in events]


def test_complete_after_started_and_execution_completed_succeeds(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_1", "prompt"))
    sink.emit(ExecutionCompleted("review_exec_1", "{}", 1, 0, 0, 1, 1, None))
    sink.complete(_report())
    events = runtime.events(run.run_id, limit=200).events
    assert events[-1].type == RunEventType.REVIEW_COMPLETED


def test_every_hardening_scenario_leaves_exactly_one_terminal(tmp_path):
    # complete-before-start
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    with pytest.raises(ReviewRecordingError):
        sink.complete(_report())
    terminals = [e for e in runtime.events(run.run_id, limit=200).events if e.type in (
        RunEventType.REVIEW_COMPLETED, RunEventType.REVIEW_FAILED, RunEventType.REVIEW_INTERRUPTED,
    )]
    assert len(terminals) == 0

    # fail-before-start
    runtime, run = setup_runtime(tmp_path / "b")
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    with pytest.raises(ReviewRecordingError):
        sink.fail("rev-1", "X", "y")
    terminals = [e for e in runtime.events(run.run_id, limit=200).events if e.type in (
        RunEventType.REVIEW_COMPLETED, RunEventType.REVIEW_FAILED, RunEventType.REVIEW_INTERRUPTED,
    )]
    assert len(terminals) == 0

    # start + complete, second sink fail without start
    runtime, run = setup_runtime(tmp_path / "c")
    first = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    first.emit(ExecutionStarted("review_exec_1", "prompt"))
    first.emit(ExecutionCompleted("review_exec_1", "{}", 1, 0, 0, 1, 1, None))
    first.complete(_report())
    second = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    with pytest.raises(ReviewRecordingError):
        second.fail("rev-1", "X", "y")
    terminals = [e for e in runtime.events(run.run_id, limit=200).events if e.type in (
        RunEventType.REVIEW_COMPLETED, RunEventType.REVIEW_FAILED, RunEventType.REVIEW_INTERRUPTED,
    )]
    assert len(terminals) == 1

    # start + fail, second sink complete
    runtime, run = setup_runtime(tmp_path / "d")
    first = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    first.emit(ExecutionStarted("review_exec_1", "prompt"))
    first.emit(ExecutionFailed("review_exec_1", "X", "y"))
    second = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    with pytest.raises(ReviewRecordingError):
        second.complete(_report())
    terminals = [e for e in runtime.events(run.run_id, limit=200).events if e.type in (
        RunEventType.REVIEW_COMPLETED, RunEventType.REVIEW_FAILED, RunEventType.REVIEW_INTERRUPTED,
    )]
    assert len(terminals) == 1

    # start, no ExecutionCompleted, complete rejected
    runtime, run = setup_runtime(tmp_path / "e")
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_1", "prompt"))
    with pytest.raises(ReviewRecordingError):
        sink.complete(_report())
    terminals = [e for e in runtime.events(run.run_id, limit=200).events if e.type in (
        RunEventType.REVIEW_COMPLETED, RunEventType.REVIEW_FAILED, RunEventType.REVIEW_INTERRUPTED,
    )]
    assert len(terminals) == 0

    # start + ExecutionCompleted + complete succeeds
    runtime, run = setup_runtime(tmp_path / "f")
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_1", "prompt"))
    sink.emit(ExecutionCompleted("review_exec_1", "{}", 1, 0, 0, 1, 1, None))
    sink.complete(_report())
    terminals = [e for e in runtime.events(run.run_id, limit=200).events if e.type in (
        RunEventType.REVIEW_COMPLETED, RunEventType.REVIEW_FAILED, RunEventType.REVIEW_INTERRUPTED,
    )]
    assert len(terminals) == 1
    assert terminals[0].type == RunEventType.REVIEW_COMPLETED


# ---------------- hardening: bound reviewer model text before canonical storage ----------------


def _model_completed(text, *, turn_index=1, turn_id="turn-1", item_id="item-1"):
    return ModelCompleted(
        "review_exec_1", turn_index, turn_id, item_id, text, (), ModelStopReason.COMPLETED.value, ModelUsage(1, 1),
    )


def test_oversized_model_completed_text_is_bounded_in_canonical_storage(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_1", "prompt"))
    sink.emit(TurnStarted("review_exec_1", 1, "turn-1"))
    sink.emit(ModelStarted("review_exec_1", 1, "turn-1", "item-1"))
    oversized = "x" * (MAX_MODEL_OUTPUT_CHARS + 5000)
    sink.emit(_model_completed(oversized))

    events = runtime.events(run.run_id, limit=200).events
    completed = next(e for e in events if e.type == RunEventType.MODEL_COMPLETED)
    assert len(completed.payload["text"]) <= MAX_MODEL_OUTPUT_CHARS
    assert completed.payload["text_truncated"] is True
    assert completed.payload["text"] == oversized[:MAX_MODEL_OUTPUT_CHARS]


def test_ordinary_model_completed_text_is_preserved_exactly(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_1", "prompt"))
    sink.emit(TurnStarted("review_exec_1", 1, "turn-1"))
    sink.emit(ModelStarted("review_exec_1", 1, "turn-1", "item-1"))
    short_text = '{"verdict":"APPROVED","summary":"ok","findings":[]}'
    sink.emit(_model_completed(short_text))

    completed = next(e for e in runtime.events(run.run_id, limit=200).events if e.type == RunEventType.MODEL_COMPLETED)
    assert completed.payload["text"] == short_text
    assert completed.payload["text_truncated"] is False


def test_oversized_final_answer_end_to_end_bounds_storage_but_rejects_review(tmp_path):
    """Full ReviewerRunner path: canonical model.completed storage is bounded,
    but the actual oversized AgentOutcome.final_text still reaches the
    parser unchanged and is rejected -> review.failed, never review.completed,
    and the Run stays non-terminal."""
    runtime, run = setup_runtime(tmp_path)
    oversized = "x" * (MAX_MODEL_OUTPUT_CHARS + 1)
    backend = ScriptedBackend([ModelTurn(oversized, (), ModelStopReason.COMPLETED, ModelUsage())])
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-oversized")
    request = ReviewRequest(task="t", diff="d")

    with pytest.raises(ReviewProtocolError):
        ReviewerRunner(backend).run(LocalWorkspace(tmp_path), request, recorder=sink, review_id="rev-oversized")

    events = runtime.events(run.run_id, limit=200).events
    model_completed = next(e for e in events if e.type == RunEventType.MODEL_COMPLETED)
    assert len(model_completed.payload["text"]) <= MAX_MODEL_OUTPUT_CHARS
    assert model_completed.payload["text_truncated"] is True

    assert events[-1].type == RunEventType.REVIEW_FAILED
    assert RunEventType.REVIEW_COMPLETED not in [e.type for e in events]
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


# ---------------- hardening: transient review lifecycle ordering ----------------


def test_execution_completed_before_started_is_rejected_and_never_arms_completion_flag(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")

    with pytest.raises(ReviewRecordingError):
        sink.emit(ExecutionCompleted("review_exec_1", "{}", 1, 0, 0, 1, 1, None))
    assert event_types(runtime, run) == [RunEventType.RUN_STARTED]

    sink.emit(ExecutionStarted("review_exec_1", "prompt"))
    with pytest.raises(ReviewRecordingError):
        sink.complete(_report())
    events = runtime.events(run.run_id, limit=200).events
    assert RunEventType.REVIEW_COMPLETED not in [e.type for e in events]


def test_turn_started_before_execution_started_is_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    with pytest.raises(ReviewRecordingError):
        sink.emit(TurnStarted("review_exec_1", 1, "turn-1"))
    assert event_types(runtime, run) == [RunEventType.RUN_STARTED]


def test_lifecycle_event_after_review_completed_is_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_1", "prompt"))
    sink.emit(ExecutionCompleted("review_exec_1", "{}", 1, 0, 0, 1, 1, None))
    sink.complete(_report())

    before = event_types(runtime, run)
    with pytest.raises(ReviewRecordingError):
        sink.emit(TurnStarted("review_exec_1", 1, "turn-1"))
    after = event_types(runtime, run)
    assert after == before
    assert after[-1] == RunEventType.REVIEW_COMPLETED


def test_lifecycle_event_after_review_failed_is_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_1", "prompt"))
    sink.emit(ExecutionFailed("review_exec_1", "X", "y"))

    before = event_types(runtime, run)
    with pytest.raises(ReviewRecordingError):
        sink.emit(ModelStarted("review_exec_1", 1, "turn-1", "item-1"))
    after = event_types(runtime, run)
    assert after == before
    assert after[-1] == RunEventType.REVIEW_FAILED


def test_normal_happy_path_lifecycle_ordering_unaffected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    sink = CanonicalReviewEventSink(runtime, run.run_id, review_id="rev-1")
    sink.emit(ExecutionStarted("review_exec_1", "prompt"))
    sink.emit(TurnStarted("review_exec_1", 1, "turn-1"))
    sink.emit(ModelStarted("review_exec_1", 1, "turn-1", "item-1"))
    sink.emit(_model_completed('{"verdict":"APPROVED","summary":"ok","findings":[]}'))
    sink.emit(TurnCompleted("review_exec_1", 1, "turn-1"))
    sink.emit(ExecutionCompleted("review_exec_1", "{}", 1, 0, 0, 1, 1, None))
    sink.complete(_report())
    assert event_types(runtime, run)[-1] == RunEventType.REVIEW_COMPLETED
