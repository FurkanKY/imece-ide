"""PipelineRunner <-> decision_runtime wiring, at the pipeline level
(docs/JEV-DESIGN.md Spike S1): once FixLoopRunner stops with
FixLoopStatus.NEEDS_USER (see tests/test_fix_loop_decision_gate.py for the
fix_runtime-level contract), PipelineRunner is the one responsible for
finishing settlement -- it runs an advisory Reviewer pass over the diff and
settles the Run WAITING_USER via CanonicalPipelineRecorder.needs_user(),
exactly mirroring its own existing "no verification plan detected" advisory
path. The proposal therefore stays viewable/Apply-Reject-able instead of the
Run terminating FAILED, for BOTH `run()` and `continue_with_feedback()`.

Uses a scripted fake VerificationFailureGate (not the real
RuleDecisionBackend) so these are pure control-flow tests of the
PipelineRunner <-> FixLoopRunner <-> decision-gate wiring.
"""

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from change_runtime.models import WorkspaceChangeSet  # noqa: E402
from decision_runtime.triage import (  # noqa: E402
    RuleDecisionBackend,
    TriageAction,
    TriageFacts,
    TriageOutcome,
    build_triage_spec,
    build_triage_state,
)
from fix_runtime.models import FixLoopStatus  # noqa: E402
from fix_runtime.ports import WorkerAttemptResult  # noqa: E402
from pipeline_runtime.models import PipelineStatus  # noqa: E402
from pipeline_runtime.runner import PipelineRunner  # noqa: E402
from planner_runtime.models import PlanReport, PlanStep, TaskComplexity, TaskProfile, TaskScope  # noqa: E402
from process_runtime import ProcessRequest, ProcessResult  # noqa: E402
from review_runtime.models import ReviewReport, ReviewVerdict  # noqa: E402
from run_runtime import RunEventSpec, RunEventType, RunRuntime, RunStatus, RunStore  # noqa: E402
from verification_runtime import VerificationCheck, VerificationPlan  # noqa: E402
from verification_runtime.models import VerificationCheckResult, VerificationReport, VerificationStatus  # noqa: E402


# ---------------- fakes (mirrors tests/test_pipeline_runner.py's own fakes) ----------------


class FakeWorkspace:
    def __init__(self, root: Path, content: str = ""):
        self.root = root
        self.content = content


class FakeChangeProvider:
    def capture(self, workspace: FakeWorkspace) -> WorkspaceChangeSet:
        if not workspace.content:
            return WorkspaceChangeSet(diff="", changed_paths=())
        return WorkspaceChangeSet(diff=workspace.content, changed_paths=("file.txt",))


class FakePlanAttemptRunner:
    def run(self, workspace, task, *, plan_id, cancel_token=None, pinned_paths=()):
        return PlanReport(
            plan_id=plan_id, summary="Do the thing.",
            steps=(PlanStep(title="Step 1", objective="Do it."),),
            acceptance_criteria=("tests pass",), risks=(),
            task_profile=TaskProfile(complexity=TaskComplexity.LOW, scope=TaskScope.LOCAL),
            repository_fingerprint="a" * 64,
            task_sha256=hashlib.sha256(task.encode("utf-8")).hexdigest(),
        )


class FakeWorkerAttemptRunner:
    def __init__(self, runtime, run_id):
        self._runtime = runtime
        self._run_id = run_id
        self.call_count = 0

    def run(self, workspace: FakeWorkspace, request, *, execution_id: str, cancel_token=None) -> WorkerAttemptResult:
        self.call_count += 1
        self._runtime.record(
            run_id=self._run_id, type=RunEventType.EXECUTION_STARTED, payload={"task": request.task},
            execution_id=execution_id, correlation_id=execution_id, source="native_agent",
        )
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
    result = VerificationCheckResult("c1", "Check", status, _process_result(0 if status is VerificationStatus.PASS else 1))
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
        self.requests: list = []

    def run(self, workspace, request, *, review_id, cancel_token=None, pinned_paths=()):
        self.calls.append(review_id)
        self.requests.append(request)
        verdict = self._verdicts.pop(0)
        verification_report = request.verification_report
        report = ReviewReport(
            review_id=review_id, verdict=verdict, summary="s", findings=(),
            repository_fingerprint="a" * 64, diff_sha256=request.diff_sha256,
            verification_id=verification_report.verification_id if verification_report else None,
            verification_status=verification_report.status.value if verification_report else None,
        )
        self._runtime.record_many(run_id=self._run_id, specs=(
            RunEventSpec(
                type=RunEventType.REVIEW_STARTED, payload={"review_id": review_id},
                correlation_id=review_id, source="reviewer",
            ),
            RunEventSpec(
                type=RunEventType.REVIEW_COMPLETED,
                payload={
                    "review_id": review_id, "verdict": verdict.value, "summary": "s", "findings": [],
                    "repository_fingerprint": report.repository_fingerprint, "diff_sha256": report.diff_sha256,
                    "verification_id": report.verification_id, "verification_status": report.verification_status,
                },
                correlation_id=review_id, source="reviewer",
            ),
        ))
        return report


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _decision_result():
    spec = build_triage_spec("d1")
    state = build_triage_state(
        TriageFacts(check_id="c1", command=("true",), exit_code=1, timed_out=False,
                    error_block="boom", changed_paths=())
    )
    return RuleDecisionBackend().decide(spec, state)


class ScriptedGate:
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


# ==================== run(): verification FAIL -> decision gate NEEDS_USER ====================


def test_run_needs_user_environment_settles_waiting_user_with_advisory_review(tmp_path):
    (tmp_path / "tests").mkdir()
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    message = "Doğrulama adımı gerekli bir bağımlılık eksik olduğu için başarısız oldu."
    gate = ScriptedGate([_outcome(TriageAction.NEEDS_USER, failure_kind="missing_dependency", needs_user_message=message)])
    runner = PipelineRunner(
        runtime, planner=FakePlanAttemptRunner(), worker=FakeWorkerAttemptRunner(runtime, run.run_id),
        verification=FakeVerificationAttemptRunner(
            runtime, run.run_id, [VerificationStatus.FAIL, VerificationStatus.FAIL],
        ),
        reviewer=FakeReviewAttemptRunner(runtime, run.run_id, [ReviewVerdict.APPROVED]),
        change_provider=FakeChangeProvider(), decision_gate=gate,
    )

    report = runner.run(run.run_id, workspace, "Implement X")

    assert report.status is PipelineStatus.NEEDS_USER
    assert report.reason == "needs_user_environment"
    assert report.fix_loop_report is not None
    assert report.fix_loop_report.status is FixLoopStatus.NEEDS_USER
    assert report.review_report is not None  # the advisory review actually ran
    # The proposal is genuinely viewable: WAITING_USER with proposal.ready,
    # never a terminal RUN_FAILED (see pipeline_runtime.runner.
    # PipelineRunner._settle_decision_needs_user's docstring).
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER
    types = [e.type for e in runtime.events(run.run_id, limit=100).events]
    assert RunEventType.PROPOSAL_READY in types
    assert RunEventType.RUN_WAITING_USER in types
    assert RunEventType.RUN_FAILED not in types
    proposal_event = next(e for e in runtime.events(run.run_id, limit=100).events if e.type == RunEventType.PROPOSAL_READY)
    assert proposal_event.payload["reason"] == "needs_user_environment"
    assert proposal_event.payload["message"] == message


def test_run_pre_existing_settles_waiting_user_with_advisory_review(tmp_path):
    (tmp_path / "tests").mkdir()
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    gate = ScriptedGate([_outcome(TriageAction.MARK_PRE_EXISTING, failure_kind="unrelated_preexisting")])
    runner = PipelineRunner(
        runtime, planner=FakePlanAttemptRunner(), worker=FakeWorkerAttemptRunner(runtime, run.run_id),
        verification=FakeVerificationAttemptRunner(
            runtime, run.run_id, [VerificationStatus.FAIL, VerificationStatus.FAIL],
        ),
        reviewer=FakeReviewAttemptRunner(runtime, run.run_id, [ReviewVerdict.APPROVED]),
        change_provider=FakeChangeProvider(), decision_gate=gate,
    )

    report = runner.run(run.run_id, workspace, "Implement X")

    assert report.status is PipelineStatus.NEEDS_USER
    assert report.reason == "pre_existing_failure"
    assert report.review_report is not None
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER
    types = [e.type for e in runtime.events(run.run_id, limit=100).events]
    assert RunEventType.RUN_FAILED not in types


# ==================== continue_with_feedback(): same NEEDS_USER settlement ====================


def test_continue_with_feedback_needs_user_settles_waiting_user(tmp_path):
    (tmp_path / "tests").mkdir()
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    # continue_with_feedback() resumes a WAITING_USER run.
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_WAITING_USER, payload={})
    message = "Doğrulama adımı ortam/araç sorunu nedeniyle çalıştırılamadı."
    gate = ScriptedGate([_outcome(TriageAction.NEEDS_USER, failure_kind="environment_or_tooling", needs_user_message=message)])
    runner = PipelineRunner(
        runtime, planner=FakePlanAttemptRunner(), worker=FakeWorkerAttemptRunner(runtime, run.run_id),
        verification=FakeVerificationAttemptRunner(
            runtime, run.run_id, [VerificationStatus.FAIL, VerificationStatus.FAIL],
        ),
        reviewer=FakeReviewAttemptRunner(runtime, run.run_id, [ReviewVerdict.APPROVED]),
        change_provider=FakeChangeProvider(), decision_gate=gate,
    )

    report = runner.continue_with_feedback(
        run.run_id, workspace, "please also handle the edge case", task="Implement X",
    )

    assert report.status is PipelineStatus.NEEDS_USER
    assert report.reason == "needs_user_environment"
    assert report.review_report is not None
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER
    types = [e.type for e in runtime.events(run.run_id, limit=100).events]
    assert RunEventType.RUN_FAILED not in types


# ==================== decision_gate=None (off): unchanged behaviour ====================


def test_no_decision_gate_verification_fail_behaves_exactly_as_before(tmp_path):
    (tmp_path / "tests").mkdir()
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    runner = PipelineRunner(
        runtime, planner=FakePlanAttemptRunner(), worker=FakeWorkerAttemptRunner(runtime, run.run_id),
        verification=FakeVerificationAttemptRunner(
            runtime, run.run_id, [VerificationStatus.FAIL, VerificationStatus.PASS],
        ),
        reviewer=FakeReviewAttemptRunner(runtime, run.run_id, [ReviewVerdict.APPROVED]),
        change_provider=FakeChangeProvider(),
    )

    report = runner.run(run.run_id, workspace, "Implement X")

    assert report.status is PipelineStatus.NEEDS_USER
    assert report.reason == "reviewed"
    assert report.fix_loop_report.status is FixLoopStatus.COMPLETED
