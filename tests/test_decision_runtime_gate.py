"""decision_runtime.gate.VerificationFailureGate — orchestration tests.

Uses FakeDecisionBackend (no baseline rerun: run_baseline=False, so no real
git repo is needed here — decision_runtime.baseline is covered on its own in
tests/test_decision_runtime_baseline.py) to test: fact extraction from a
real VerificationReport/Plan, the design-rule-1 fallback to
RuleDecisionBackend on a DecisionBackendError, and optional canonical
decision.made recording.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_runtime.errors import DecisionBackendError  # noqa: E402
from decision_runtime.fake_backend import FakeDecisionBackend  # noqa: E402
from decision_runtime.gate import VerificationFailureGate  # noqa: E402
from decision_runtime.policy import DecisionPolicy  # noqa: E402
from decision_runtime.recorder import CanonicalDecisionRecorder  # noqa: E402
from decision_runtime.triage import TriageAction, build_triage_spec, build_triage_state, TriageFacts, RuleDecisionBackend  # noqa: E402
from process_runtime import ProcessRequest, ProcessResult  # noqa: E402
from run_runtime import RunEventType, RunRuntime, RunStore  # noqa: E402
from verification_runtime.models import VerificationCheck, VerificationCheckResult, VerificationPlan, VerificationReport, VerificationStatus  # noqa: E402


def _plan_and_failing_report():
    plan = VerificationPlan("plan-1", (VerificationCheck("c1", "Check", ProcessRequest(("true",))),))
    process = ProcessResult(
        argv=("true",), cwd=".", exit_code=1, timed_out=False, duration_ms=1,
        stdout="", stderr="ModuleNotFoundError: No module named 'requests'\n",
        stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )
    result = VerificationCheckResult("c1", "Check", VerificationStatus.FAIL, process)
    report = VerificationReport(verification_id="ver-1", plan_id="plan-1", results=(result,), duration_ms=1)
    return plan, report


def _rule_result_for(state):
    spec = build_triage_spec("verification_failure_triage")
    return RuleDecisionBackend().decide(spec, state)


def test_evaluate_routes_a_confident_rule_answer_without_baseline():
    plan, report = _plan_and_failing_report()
    backend = RuleDecisionBackend()
    gate = VerificationFailureGate(backend, run_baseline=False)

    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    assert outcome.action is TriageAction.NEEDS_USER
    assert outcome.failure_kind == "missing_dependency"


def test_backend_failure_falls_back_to_rule_backend_and_marks_fallback_used():
    plan, report = _plan_and_failing_report()
    backend = FakeDecisionBackend([DecisionBackendError("boom, backend is down")])
    gate = VerificationFailureGate(backend, run_baseline=False)

    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    assert outcome.action is TriageAction.NEEDS_USER  # rule fallback still classifies confidently
    assert outcome.result.fallback_used is True
    assert outcome.result.backend == RuleDecisionBackend.BACKEND_NAME


def test_evaluate_records_a_decision_made_event_when_a_recorder_is_given(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    recorder = CanonicalDecisionRecorder(runtime, run.run_id)

    plan, report = _plan_and_failing_report()
    gate = VerificationFailureGate(RuleDecisionBackend(), recorder=recorder, run_baseline=False)
    gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    events = runtime.events(run.run_id, limit=50).events
    decision_events = [e for e in events if e.type == RunEventType.DECISION_MADE]
    assert len(decision_events) == 1
    assert decision_events[0].payload["answers"]["failure_kind"]["choice"] == "missing_dependency"


def test_evaluate_requires_a_failing_check_matching_the_plan():
    plan = VerificationPlan("plan-1", (VerificationCheck("c1", "Check", ProcessRequest(("true",))),))
    process = ProcessResult(
        argv=("true",), cwd=".", exit_code=0, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )
    passing_report = VerificationReport(
        verification_id="ver-1", plan_id="plan-1",
        results=(VerificationCheckResult("c1", "Check", VerificationStatus.PASS, process),), duration_ms=1,
    )
    gate = VerificationFailureGate(RuleDecisionBackend(), run_baseline=False)
    with pytest.raises(ValueError):
        gate.evaluate(workspace=object(), verification_plan=plan, verification_report=passing_report)


def test_low_confidence_default_guess_continues_fix_loop():
    plan = VerificationPlan("plan-1", (VerificationCheck("c1", "Check", ProcessRequest(("true",))),))
    process = ProcessResult(
        argv=("true",), cwd=".", exit_code=1, timed_out=False, duration_ms=1,
        stdout="", stderr="assert 4 == 5\n",
        stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )
    report = VerificationReport(
        verification_id="ver-1", plan_id="plan-1",
        results=(VerificationCheckResult("c1", "Check", VerificationStatus.FAIL, process),), duration_ms=1,
    )
    gate = VerificationFailureGate(RuleDecisionBackend(), run_baseline=False)
    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)
    assert outcome.action is TriageAction.CONTINUE_FIX_LOOP
