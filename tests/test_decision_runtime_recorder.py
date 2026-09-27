"""decision_runtime.recorder.CanonicalDecisionRecorder + the decision.made
canonical event — must behave exactly like agent.activity is described as
behaving: recorded verbatim, but invisible to run_runtime.projector and
never counted as execution activity by run_runtime.completion.RunCompletionGate.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_runtime.models import ChoiceAnswer, DecisionResult, NoulAnswer  # noqa: E402
from decision_runtime.recorder import CanonicalDecisionRecorder, decision_result_payload  # noqa: E402
from process_runtime import ProcessRequest  # noqa: E402
from run_runtime import RunEventType, RunRuntime, RunStore  # noqa: E402
from run_runtime.completion import RunCompletionGate  # noqa: E402
from run_runtime.projector import project_run  # noqa: E402
from verification_runtime.models import VerificationCheck, VerificationPlan  # noqa: E402


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _decision_result():
    return DecisionResult(
        decision_id="verification_failure_triage", question_set_version="v1",
        answers={
            "failure_kind": ChoiceAnswer(choice="code_bug", probabilities={"code_bug": 0.9, "other": 0.1}, confidence=0.8),
            "caused_by_change": NoulAnswer(noul=0.7),
        },
        backend="rule", model_version="rule-v1", latency_ms=3, prompt_tokens=0, fallback_used=False,
    )


def test_record_appends_decision_made_with_full_payload(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalDecisionRecorder(runtime, run.run_id)
    result = _decision_result()

    recorder.record(result)

    events = runtime.events(run.run_id, limit=50).events
    decision_events = [e for e in events if e.type == RunEventType.DECISION_MADE]
    assert len(decision_events) == 1
    event = decision_events[0]
    assert event.source == "decision_runtime"
    assert event.correlation_id == "verification_failure_triage"
    assert event.payload == decision_result_payload(result)
    assert event.payload["fallback_used"] is False
    assert event.payload["answers"]["failure_kind"]["choice"] == "code_bug"


def test_decision_made_is_ignored_by_the_projector(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalDecisionRecorder(runtime, run.run_id)
    before = runtime.get_run(run.run_id)

    recorder.record(_decision_result())

    events = runtime.events(run.run_id, limit=50).events
    decision_event = next(e for e in events if e.type == RunEventType.DECISION_MADE)
    projected = project_run(before, decision_event)
    # Unknown-to-the-projector type: RunRecord comes back completely unchanged
    # (see run_runtime.projector.project_run's module docstring).
    assert projected == before


def test_decision_made_never_counts_as_execution_activity_for_the_completion_gate(tmp_path):
    """Interleave a decision.made between execution.completed and
    verification.* the same way run_runtime.reviewer's canonical events are
    proven not to trip RunCompletionGate's staleness check."""
    runtime, run = setup_runtime(tmp_path)
    execution_id = "exec-1"
    runtime.record(
        run_id=run.run_id, type=RunEventType.EXECUTION_STARTED, payload={},
        execution_id=execution_id, source="native_agent",
    )
    runtime.record(
        run_id=run.run_id, type=RunEventType.EXECUTION_COMPLETED, payload={},
        execution_id=execution_id, source="native_agent",
    )

    recorder = CanonicalDecisionRecorder(runtime, run.run_id)
    recorder.record(_decision_result())  # must NOT look like "newer execution activity"

    verification_id = "ver-1"
    runtime.record(
        run_id=run.run_id, type=RunEventType.VERIFICATION_STARTED,
        payload={"verification_id": verification_id, "plan_id": "plan-1", "check_count": 1},
        correlation_id=verification_id, source="verification",
    )
    runtime.record(
        run_id=run.run_id, type=RunEventType.VERIFICATION_COMPLETED,
        payload={
            "verification_id": verification_id, "plan_id": "plan-1", "status": "pass", "duration_ms": 1,
            "counts": {"pass": True, "fail": False, "timeout": False, "error": False, "total": 1},
        },
        correlation_id=verification_id, source="verification",
    )

    gate = RunCompletionGate(runtime)
    gate.complete_verified(run.run_id, verification_id=verification_id)  # must not raise "stale" error

    final = runtime.get_run(run.run_id)
    assert final.status.value == "succeeded"
