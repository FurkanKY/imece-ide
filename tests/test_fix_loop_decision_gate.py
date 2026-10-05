"""FixLoopRunner <-> decision_runtime wiring (docs/JEV-DESIGN.md Spike S1).

Uses a scripted fake VerificationFailureGate (not the real RuleDecisionBackend
— that classification logic is covered by tests/test_decision_triage_fixtures.py)
so these tests are pure control-flow tests of FixLoopRunner's own wiring:
NEEDS_USER (missing_dependency/environment_or_tooling) and MARK_PRE_EXISTING
both stop the loop WITHOUT settling the Run (FixLoopStatus.NEEDS_USER — see
fix_runtime.models.FixLoopStatus's docstring: the caller, pipeline_runtime.
PipelineRunner, is responsible for finishing settlement via an advisory
review, tested separately in tests/test_pipeline_decision_gate.py),
RERUN_VERIFICATION_ONCE lets a flaky check pass through to review,
CONTINUE_FIX_LOOP attaches the classification to the NEXT attempt's rendered
fix-worker input, and any exception out of the gate degrades to "gate
absent" (today's behaviour) — see design rule 1.

With no decision_gate at all (the default, "decision_layer": "off"), the
existing tests/test_fix_loop_runner.py suite already proves nothing changes.
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from change_runtime.models import WorkspaceChangeSet  # noqa: E402
from decision_runtime import gate as gate_module  # noqa: E402
from decision_runtime.fake_backend import FakeDecisionBackend  # noqa: E402
from decision_runtime.gate import VerificationFailureGate  # noqa: E402
from decision_runtime.models import ChoiceAnswer, DecisionResult, NoulAnswer, ScoreAnswer  # noqa: E402
from decision_runtime.triage import (  # noqa: E402
    QUESTION_SET_VERSION,
    RuleDecisionBackend,
    TriageAction,
    TriageFacts,
    TriageOutcome,
    build_triage_spec,
    build_triage_state,
)
from fix_runtime.models import FixLoopRequest, FixLoopStatus, FixTrigger, FixTriggerKind  # noqa: E402
from fix_runtime.ports import WorkerAttemptResult  # noqa: E402
from fix_runtime.runner import FixLoopRunner  # noqa: E402
from process_runtime import ProcessRequest, ProcessResult  # noqa: E402
from review_runtime.models import ReviewReport, ReviewVerdict  # noqa: E402
from run_runtime import RunEventSpec, RunEventType, RunRuntime, RunStatus, RunStore  # noqa: E402
from verification_runtime import VerificationCheck, VerificationPlan  # noqa: E402
from verification_runtime.models import VerificationCheckResult, VerificationReport, VerificationStatus  # noqa: E402


# ---------------- fakes (mirrors tests/test_fix_loop_runner.py's own fakes) ----------------


class FakeWorkspace:
    def __init__(self, content: str = "", *, root: Path | None = None):
        self.content = content
        # FixLoopRunner reads project rules via workspace.root (see
        # context_runtime.rules.load_project_rules); default to a fresh,
        # empty temp dir so "no rules" is the deterministic default.
        self.root = root if root is not None else Path(tempfile.mkdtemp(prefix="imece-fake-workspace-"))


class FakeChangeProvider:
    def capture(self, workspace: FakeWorkspace) -> WorkspaceChangeSet:
        if not workspace.content:
            return WorkspaceChangeSet(diff="", changed_paths=())
        return WorkspaceChangeSet(diff=workspace.content, changed_paths=("file.txt",))


class FakeWorkerAttemptRunner:
    """Also captures each attempt's request/rendered_input so tests can assert
    the decision classification actually reached the fix prompt and the bounded
    render recipe that lets it be re-bound canonically later."""

    def __init__(self, runtime, run_id, *, changes=True):
        self._runtime = runtime
        self._run_id = run_id
        self._changes = changes
        self.call_count = 0
        self.rendered_inputs: list[str] = []
        self.requests: list = []

    def run(self, workspace: FakeWorkspace, request, *, execution_id: str, cancel_token=None) -> WorkerAttemptResult:
        self.call_count += 1
        self.rendered_inputs.append(request.rendered_input)
        self.requests.append(request)
        should_change = self._changes[self.call_count - 1] if isinstance(self._changes, list) else self._changes
        if should_change:
            workspace.content += f"change-{self.call_count}\n"
        self._runtime.record(
            run_id=self._run_id, type=RunEventType.EXECUTION_COMPLETED, payload={"final_text": "done"},
            execution_id=execution_id, correlation_id=execution_id, source="native_agent",
        )
        return WorkerAttemptResult(execution_id=execution_id)


def _process_result(exit_code=0):
    return ProcessResult(
        argv=("true",), cwd=".", exit_code=exit_code, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )


def _verification_result(verification_id, status):
    process = _process_result(0 if status is VerificationStatus.PASS else 1)
    result = VerificationCheckResult("c1", "Check", status, process)
    return VerificationReport(verification_id=verification_id, plan_id="plan-1", results=(result,), duration_ms=1)


class FakeVerificationAttemptRunner:
    def __init__(self, runtime, run_id, statuses):
        self._runtime = runtime
        self._run_id = run_id
        self._statuses = list(statuses)
        self.calls: list[str] = []

    def run(self, workspace, plan, *, verification_id, cancel_token=None):
        self.calls.append(verification_id)
        status = self._statuses.pop(0)
        report = _verification_result(verification_id, status)
        self._runtime.record_many(run_id=self._run_id, specs=(
            RunEventSpec(
                type=RunEventType.VERIFICATION_STARTED,
                payload={"verification_id": verification_id, "plan_id": plan.plan_id, "check_count": 1},
                correlation_id=verification_id, source="verification",
            ),
            RunEventSpec(
                type=RunEventType.VERIFICATION_COMPLETED,
                payload={
                    "verification_id": verification_id, "plan_id": plan.plan_id, "status": status.value,
                    "duration_ms": 1,
                    "counts": {
                        "pass": status is VerificationStatus.PASS, "fail": status is VerificationStatus.FAIL,
                        "timeout": False, "error": False, "total": 1,
                    },
                },
                correlation_id=verification_id, source="verification",
            ),
        ))
        return report


class FakeReviewAttemptRunner:
    def __init__(self, runtime, run_id, verdicts):
        self._runtime = runtime
        self._run_id = run_id
        self._verdicts = list(verdicts)
        self.calls: list[str] = []

    def run(self, workspace, request, *, review_id, cancel_token=None, pinned_paths=()):
        self.calls.append(review_id)
        verdict = self._verdicts.pop(0)
        report = ReviewReport(
            review_id=review_id, verdict=verdict, summary="s", findings=(),
            repository_fingerprint="a" * 64, diff_sha256=request.diff_sha256,
            verification_id=request.verification_report.verification_id,
            verification_status=request.verification_report.status.value,
        )
        self._runtime.record_many(run_id=self._run_id, specs=(
            RunEventSpec(
                type=RunEventType.REVIEW_STARTED, payload={"review_id": review_id},
                correlation_id=review_id, source="reviewer",
            ),
            RunEventSpec(
                type=RunEventType.REVIEW_COMPLETED,
                payload={
                    "review_id": review_id, "verdict": verdict.value, "note": "s", "summary": "s",
                    "findings": [],
                    "repository_fingerprint": report.repository_fingerprint, "diff_sha256": report.diff_sha256,
                    "verification_id": report.verification_id, "verification_status": report.verification_status,
                },
                correlation_id=review_id, source="reviewer",
            ),
        ))
        return report


def _valid_verification_plan():
    return VerificationPlan("plan-1", (VerificationCheck("c1", "Check", ProcessRequest(("true",))),))


def _initial_fail_trigger(verification_id="ver-0"):
    return FixTrigger(
        kind=FixTriggerKind.VERIFICATION_FAIL,
        verification_report=_verification_result(verification_id, VerificationStatus.FAIL),
    )


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _decision_result():
    """A minimal, genuinely-valid DecisionResult to embed in a scripted TriageOutcome."""
    spec = build_triage_spec("d1")
    state = build_triage_state(
        TriageFacts(check_id="c1", command=("true",), exit_code=1, timed_out=False,
                    error_block="boom", changed_paths=())
    )
    return RuleDecisionBackend().decide(spec, state)


class ScriptedGate:
    """A fake VerificationFailureGate: returns pre-scripted TriageOutcomes in
    order, or raises a pre-scripted exception (to test design-rule-1
    fallback). Records every call's kwargs for assertions."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def evaluate(self, **kwargs):
        self.calls.append(kwargs)
        entry = self._script.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return entry


def _outcome(action, *, failure_kind="code_bug", needs_user_message=None):
    return TriageOutcome(
        action=action, failure_kind=failure_kind, confidence=0.99,
        result=_decision_result(), needs_user_message=needs_user_message,
    )


# ==================== NEEDS_USER (missing_dependency/environment_or_tooling) ====================


def test_needs_user_stops_the_loop_without_settling_the_run(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    worker = FakeWorkerAttemptRunner(runtime, run.run_id, changes=True)
    verification = FakeVerificationAttemptRunner(runtime, run.run_id, [VerificationStatus.FAIL])
    reviewer = FakeReviewAttemptRunner(runtime, run.run_id, [])
    message = "Doğrulama adımı gerekli bir bağımlılık eksik olduğu için başarısız oldu."
    gate = ScriptedGate([_outcome(TriageAction.NEEDS_USER, failure_kind="missing_dependency", needs_user_message=message)])
    runner = FixLoopRunner(
        runtime, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=FakeChangeProvider(), decision_gate=gate,
    )
    request = FixLoopRequest(task="fix it", trigger=_initial_fail_trigger(), verification_plan=_valid_verification_plan())

    report = runner.run(run.run_id, FakeWorkspace(), request)

    assert report.status is FixLoopStatus.NEEDS_USER
    assert report.reason == "needs_user_environment"
    assert report.needs_user_message == message
    assert worker.call_count == 1  # no second attempt was started
    assert len(gate.calls) == 1
    # FixLoopRunner does NOT settle the Run for NEEDS_USER -- the caller
    # (pipeline_runtime.PipelineRunner) is responsible for that (see
    # fix_runtime.models.FixLoopStatus.NEEDS_USER's docstring).
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


# ==================== MARK_PRE_EXISTING ====================


def test_pre_existing_stops_the_loop_without_settling_the_run(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    worker = FakeWorkerAttemptRunner(runtime, run.run_id, changes=True)
    verification = FakeVerificationAttemptRunner(runtime, run.run_id, [VerificationStatus.FAIL])
    reviewer = FakeReviewAttemptRunner(runtime, run.run_id, [])
    gate = ScriptedGate([_outcome(TriageAction.MARK_PRE_EXISTING, failure_kind="unrelated_preexisting")])
    runner = FixLoopRunner(
        runtime, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=FakeChangeProvider(), decision_gate=gate,
    )
    request = FixLoopRequest(task="fix it", trigger=_initial_fail_trigger(), verification_plan=_valid_verification_plan())

    report = runner.run(run.run_id, FakeWorkspace(), request)

    assert report.status is FixLoopStatus.NEEDS_USER
    assert report.reason == "pre_existing_failure"
    assert report.needs_user_message is None
    assert worker.call_count == 1
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


# ==================== RERUN_VERIFICATION_ONCE ====================


def test_rerun_once_lets_a_flaky_pass_reach_review(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    worker = FakeWorkerAttemptRunner(runtime, run.run_id, changes=True)
    # first call FAILs, the triage-triggered rerun PASSes.
    verification = FakeVerificationAttemptRunner(runtime, run.run_id, [VerificationStatus.FAIL, VerificationStatus.PASS])
    reviewer = FakeReviewAttemptRunner(runtime, run.run_id, [ReviewVerdict.APPROVED])
    gate = ScriptedGate([_outcome(TriageAction.RERUN_VERIFICATION_ONCE, failure_kind="flaky_or_timeout")])
    runner = FixLoopRunner(
        runtime, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=FakeChangeProvider(), decision_gate=gate,
    )
    request = FixLoopRequest(task="fix it", trigger=_initial_fail_trigger(), verification_plan=_valid_verification_plan())

    report = runner.run(run.run_id, FakeWorkspace(), request)

    assert report.status is FixLoopStatus.COMPLETED
    assert len(verification.calls) == 2  # ran once, then the triage rerun
    assert len(set(verification.calls)) == 2  # each got a fresh verification_id
    assert worker.call_count == 1  # no separate fix attempt was needed
    assert len(gate.calls) == 1


def test_rerun_once_still_failing_falls_back_to_todays_behaviour(tmp_path):
    """"re-run the check once before deciding" — a second FAIL after the
    rerun is NOT triaged again; it just falls through to the normal
    fix-attempt-or-exhaust logic (today's behaviour)."""
    runtime, run = setup_runtime(tmp_path)
    worker = FakeWorkerAttemptRunner(runtime, run.run_id, changes=[True, True])
    verification = FakeVerificationAttemptRunner(
        runtime, run.run_id, [VerificationStatus.FAIL, VerificationStatus.FAIL, VerificationStatus.PASS]
    )
    reviewer = FakeReviewAttemptRunner(runtime, run.run_id, [ReviewVerdict.APPROVED])
    gate = ScriptedGate([_outcome(TriageAction.RERUN_VERIFICATION_ONCE, failure_kind="flaky_or_timeout")])
    runner = FixLoopRunner(
        runtime, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=FakeChangeProvider(), decision_gate=gate,
    )
    request = FixLoopRequest(
        task="fix it", trigger=_initial_fail_trigger(), verification_plan=_valid_verification_plan(),
        max_fix_attempts=2,
    )

    report = runner.run(run.run_id, FakeWorkspace(), request)

    assert report.status is FixLoopStatus.COMPLETED
    assert worker.call_count == 2  # the rerun's FAIL led to a real second fix attempt
    assert len(gate.calls) == 1  # the gate is only ever consulted once (attempt 1)


# ==================== CONTINUE_FIX_LOOP (classification in the fix prompt) ====================


def test_continue_fix_loop_attaches_classification_to_next_prompt(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    worker = FakeWorkerAttemptRunner(runtime, run.run_id, changes=[True, True])
    verification = FakeVerificationAttemptRunner(
        runtime, run.run_id, [VerificationStatus.FAIL, VerificationStatus.PASS]
    )
    reviewer = FakeReviewAttemptRunner(runtime, run.run_id, [ReviewVerdict.APPROVED])
    gate = ScriptedGate([_outcome(TriageAction.CONTINUE_FIX_LOOP, failure_kind="code_bug")])
    runner = FixLoopRunner(
        runtime, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=FakeChangeProvider(), decision_gate=gate,
    )
    request = FixLoopRequest(
        task="fix it", trigger=_initial_fail_trigger(), verification_plan=_valid_verification_plan(),
        max_fix_attempts=2,
    )

    report = runner.run(run.run_id, FakeWorkspace(), request)

    assert report.status is FixLoopStatus.COMPLETED
    assert worker.call_count == 2
    assert "decision_classification: code_bug" not in worker.rendered_inputs[0]
    assert "decision_classification: code_bug" in worker.rendered_inputs[1]


def test_render_recipe_scopes_the_classification_to_exactly_one_attempt(tmp_path):
    """The attempt recipe must carry the SAME classification that attempt's
    prompt was rendered with -- no more (a later attempt would inherit a label
    its own prompt does not contain) and no less (a canonical re-bind would
    drop it)."""
    runtime, run = setup_runtime(tmp_path)
    worker = FakeWorkerAttemptRunner(runtime, run.run_id, changes=[True, True, True])
    verification = FakeVerificationAttemptRunner(
        runtime, run.run_id, [VerificationStatus.FAIL, VerificationStatus.FAIL, VerificationStatus.PASS]
    )
    reviewer = FakeReviewAttemptRunner(runtime, run.run_id, [ReviewVerdict.APPROVED])
    gate = ScriptedGate([
        _outcome(TriageAction.CONTINUE_FIX_LOOP, failure_kind="code_bug"),
        None,  # gate off / no triage evidence -> attempt 3 carries no label
    ])
    runner = FixLoopRunner(
        runtime, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=FakeChangeProvider(), decision_gate=gate,
    )
    request = FixLoopRequest(
        task="fix it", trigger=_initial_fail_trigger(), verification_plan=_valid_verification_plan(),
        max_fix_attempts=3, pinned_paths=("src/a.py",),
    )

    report = runner.run(run.run_id, FakeWorkspace(), request)

    assert report.status is FixLoopStatus.COMPLETED
    assert worker.call_count == 3
    assert [r.render_context.classification for r in worker.requests] == [None, "code_bug", None]
    assert [r.render_context.max_fix_attempts for r in worker.requests] == [3, 3, 3]
    assert all(r.render_context.pinned_paths == ("src/a.py",) for r in worker.requests)
    assert "decision_classification" not in worker.rendered_inputs[0]
    assert "decision_classification: code_bug" in worker.rendered_inputs[1]
    assert "decision_classification" not in worker.rendered_inputs[2]


# ==================== design rule 1: gate failure degrades to "gate absent" ====================


def test_decision_gate_exception_falls_back_to_todays_behaviour(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    worker = FakeWorkerAttemptRunner(runtime, run.run_id, changes=[True, True])
    verification = FakeVerificationAttemptRunner(
        runtime, run.run_id, [VerificationStatus.FAIL, VerificationStatus.PASS]
    )
    reviewer = FakeReviewAttemptRunner(runtime, run.run_id, [ReviewVerdict.APPROVED])
    gate = ScriptedGate([RuntimeError("backend is on fire")])
    runner = FixLoopRunner(
        runtime, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=FakeChangeProvider(), decision_gate=gate,
    )
    request = FixLoopRequest(
        task="fix it", trigger=_initial_fail_trigger(), verification_plan=_valid_verification_plan(),
        max_fix_attempts=2,
    )

    report = runner.run(run.run_id, FakeWorkspace(), request)

    assert report.status is FixLoopStatus.COMPLETED
    assert worker.call_count == 2
    for rendered in worker.rendered_inputs:
        assert "decision_classification" not in rendered


# ==================== S1b safety guard through the real gate: a misleading
# remote 'unrelated_preexisting' may not skip the fix loop when the baseline
# actually passed ====================


_LABELS = (
    "code_bug", "test_needs_update", "missing_dependency",
    "environment_or_tooling", "flaky_or_timeout", "unrelated_preexisting",
)


def _misleading_remote_preexisting_result():
    probabilities = {label: (0.99 if label == "unrelated_preexisting" else 0.002) for label in _LABELS}
    return DecisionResult(
        decision_id="verification_failure_triage",
        question_set_version=QUESTION_SET_VERSION,
        answers={
            "failure_kind": ChoiceAnswer(
                choice="unrelated_preexisting", probabilities=probabilities, confidence=0.99,
            ),
            "caused_by_change": NoulAnswer(noul=0.02),
            "fixable_by_agent": ScoreAnswer(
                score=0.0, probabilities={"0": 0.9, "1": 0.1, "2": 0.0}, confidence=0.99,
            ),
        },
        backend="jev",
        model_version="jev-1.13.0",
        latency_ms=5,
        prompt_tokens=87,
        fallback_used=False,
    )


def _baseline_process_result(**overrides):
    defaults = dict(
        argv=("true",), cwd=".", exit_code=1, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
        stdout_bytes=0, stderr_bytes=0,
    )
    defaults.update(overrides)
    return ProcessResult(**defaults)


@pytest.mark.parametrize(
    ("baseline_overrides",),
    [
        pytest.param({"exit_code": 0}, id="baseline-pass"),
        pytest.param({"timed_out": True}, id="baseline-timeout"),
        pytest.param({"exit_code": None}, id="baseline-error"),
        pytest.param({}, id="baseline-unavailable"),
    ],
)
def test_misleading_remote_preexisting_never_skips_the_fix_loop(tmp_path, monkeypatch, baseline_overrides):
    """The REAL VerificationFailureGate (not a scripted fake) sits between a
    remote model that confidently claims 'unrelated_preexisting' and
    FixLoopRunner: the gate's deterministic baseline guard downgrades the
    action to CONTINUE_FIX_LOOP unless the same check ACTUALLY failed on the
    baseline, so verification/review are never skipped on a model say-so."""
    runtime, run = setup_runtime(tmp_path)
    worker = FakeWorkerAttemptRunner(runtime, run.run_id, changes=[True, True])
    verification = FakeVerificationAttemptRunner(
        runtime, run.run_id, [VerificationStatus.FAIL, VerificationStatus.PASS]
    )
    reviewer = FakeReviewAttemptRunner(runtime, run.run_id, [ReviewVerdict.APPROVED])
    if baseline_overrides:
        baseline = _baseline_process_result(**baseline_overrides)
    else:
        baseline = None  # workspace has no snapshot -> no baseline evidence
    monkeypatch.setattr(gate_module, "run_baseline_check", lambda workspace, request: baseline)
    gate = VerificationFailureGate(
        FakeDecisionBackend([_misleading_remote_preexisting_result()]), run_baseline=True,
    )
    runner = FixLoopRunner(
        runtime, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=FakeChangeProvider(), decision_gate=gate,
    )
    request = FixLoopRequest(
        task="fix it", trigger=_initial_fail_trigger(), verification_plan=_valid_verification_plan(),
        max_fix_attempts=2,
    )

    report = runner.run(run.run_id, FakeWorkspace(), request)

    assert report.status is FixLoopStatus.COMPLETED  # the fix loop ran to completion
    assert report.reason != "pre_existing_failure"
    assert worker.call_count == 2  # a second fix attempt was NOT skipped
