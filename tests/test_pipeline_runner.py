"""PipelineRunner algorithm tests using fake Planner/Worker/Verification/
Reviewer ports and a fake ChangeProvider (unit-level orchestration tests;
the real adapters are covered in tests/test_pipeline_integration.py)."""

import hashlib
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.cancellation import CancellationToken, OperationCancelledError  # noqa: E402
from change_runtime.models import WorkspaceChangeSet  # noqa: E402
from fix_runtime.models import InitialWorkerRequest  # noqa: E402
from fix_runtime.ports import WorkerAttemptResult  # noqa: E402
from pipeline_runtime.errors import PipelineExecutionError  # noqa: E402
from pipeline_runtime.models import PipelineStatus  # noqa: E402
from pipeline_runtime.runner import PipelineRunner  # noqa: E402
from planner_runtime.models import PlanReport, PlanStep, TaskComplexity, TaskProfile, TaskScope  # noqa: E402
from process_runtime import ProcessRequest, ProcessResult  # noqa: E402
from review_runtime.models import ReviewFinding, ReviewReport, ReviewSeverity, ReviewVerdict  # noqa: E402
from run_runtime import RunEventSpec, RunEventType, RunRuntime, RunStatus, RunStore  # noqa: E402
from verification_runtime import VerificationCheck, VerificationPlan  # noqa: E402
from verification_runtime.models import VerificationCheckResult, VerificationReport, VerificationStatus  # noqa: E402


# ---------------- fakes ----------------


class FakeWorkspace:
    """`.root` is a real tmp_path directory so verification_detect can run
    against it for real; `.content` models the worker's cumulative change."""

    def __init__(self, root: Path, content: str = ""):
        self.root = root
        self.content = content


class FakeChangeProvider:
    def capture(self, workspace: FakeWorkspace) -> WorkspaceChangeSet:
        if not workspace.content:
            return WorkspaceChangeSet(diff="", changed_paths=())
        return WorkspaceChangeSet(diff=workspace.content, changed_paths=("file.txt",))


class FakePlanAttemptRunner:
    def __init__(self):
        self.calls: list[str] = []
        self.pinned_paths_seen: list[tuple] = []

    def run(self, workspace, task, *, plan_id, cancel_token=None, pinned_paths=()):
        self.calls.append(plan_id)
        self.pinned_paths_seen.append(tuple(pinned_paths))
        return PlanReport(
            plan_id=plan_id, summary="Do the thing.",
            steps=(PlanStep(title="Step 1", objective="Do it."),),
            acceptance_criteria=("tests pass",), risks=(),
            task_profile=TaskProfile(complexity=TaskComplexity.LOW, scope=TaskScope.LOCAL),
            repository_fingerprint="a" * 64,
            task_sha256=hashlib.sha256(task.encode("utf-8")).hexdigest(),
        )


class FakeWorkerAttemptRunner:
    def __init__(self, runtime, run_id, *, changes=True, record_completion=True):
        self._runtime = runtime
        self._run_id = run_id
        self._changes = changes
        self._record_completion = record_completion
        self.requests: list = []

    def run(self, workspace: FakeWorkspace, request, *, execution_id: str, cancel_token=None) -> WorkerAttemptResult:
        self.requests.append(request)
        self._runtime.record(
            run_id=self._run_id, type=RunEventType.EXECUTION_STARTED, payload={"task": request.task},
            execution_id=execution_id, correlation_id=execution_id, source="native_agent",
        )
        if self._changes:
            workspace.content += "change\n"
        if self._record_completion:
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
        self.pinned_paths_seen: list[tuple] = []

    def run(self, workspace, request, *, review_id, cancel_token=None, pinned_paths=()):
        self.calls.append(review_id)
        self.requests.append(request)
        self.pinned_paths_seen.append(tuple(pinned_paths))
        verdict = self._verdicts.pop(0)
        findings = () if verdict is ReviewVerdict.APPROVED else (ReviewFinding(ReviewSeverity.MAJOR, "bug"),)
        verification_report = request.verification_report
        report = ReviewReport(
            review_id=review_id, verdict=verdict, summary="s", findings=findings,
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
                    "review_id": review_id, "verdict": verdict.value, "summary": report.summary,
                    "findings": [
                        {"severity": f.severity.value, "message": f.message, "path": f.path,
                         "start_line": f.start_line, "end_line": f.end_line}
                        for f in report.findings
                    ],
                    "repository_fingerprint": report.repository_fingerprint, "diff_sha256": report.diff_sha256,
                    "verification_id": report.verification_id, "verification_status": report.verification_status,
                },
                correlation_id=review_id, source="reviewer",
            ),
        ))
        return report


def _valid_verification_plan():
    return VerificationPlan("plan-1", (VerificationCheck("c1", "Check", ProcessRequest(("true",))),))


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _make_runner(runtime, run, *, worker=None, verification_statuses=None, review_verdicts=None, max_fix_attempts=2):
    planner = FakePlanAttemptRunner()
    worker = worker or FakeWorkerAttemptRunner(runtime, run.run_id)
    verification = FakeVerificationAttemptRunner(runtime, run.run_id, verification_statuses or [VerificationStatus.PASS])
    reviewer = FakeReviewAttemptRunner(runtime, run.run_id, review_verdicts or [ReviewVerdict.APPROVED])
    change_provider = FakeChangeProvider()
    runner = PipelineRunner(
        runtime, planner=planner, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=change_provider, max_fix_attempts=max_fix_attempts,
    )
    return runner, planner, worker, verification, reviewer


# ---------------- happy path: approved on first try ----------------


def test_approved_first_try_completes_run(tmp_path):
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    workspace = FakeWorkspace(root)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(runtime, run)

    report = runner.run(run.run_id, workspace, "Implement X")

    assert report.status is PipelineStatus.NEEDS_USER
    assert report.reason == "reviewed"
    assert report.plan_report is not None
    assert report.verification_report.status is VerificationStatus.PASS
    assert report.review_report.verdict is ReviewVerdict.APPROVED
    # The user still has the final Apply/Reject word: the Run is left
    # WAITING_USER, never settled straight to SUCCEEDED.
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER
    assert planner.calls and worker.requests and isinstance(worker.requests[0], InitialWorkerRequest)


# ---------------- verification fail -> fix loop -> pass ----------------


def test_verification_fail_then_fix_loop_completes(tmp_path):
    (tmp_path / "tests").mkdir()
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    worker = FakeWorkerAttemptRunner(runtime, run.run_id)
    runner, planner, worker, verification, reviewer = _make_runner(
        runtime, run, worker=worker,
        verification_statuses=[VerificationStatus.FAIL, VerificationStatus.PASS],
        review_verdicts=[ReviewVerdict.APPROVED],
    )

    report = runner.run(run.run_id, workspace, "Implement X")

    assert report.status is PipelineStatus.NEEDS_USER
    assert report.fix_loop_report is not None
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER


# ---------------- review needs_fix -> fix loop -> approved ----------------


def test_review_needs_fix_then_fix_loop_completes(tmp_path):
    (tmp_path / "tests").mkdir()
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(
        runtime, run,
        verification_statuses=[VerificationStatus.PASS, VerificationStatus.PASS],
        review_verdicts=[ReviewVerdict.NEEDS_FIX, ReviewVerdict.APPROVED],
    )

    report = runner.run(run.run_id, workspace, "Implement X")

    assert report.status is PipelineStatus.NEEDS_USER
    assert report.fix_loop_report is not None
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER


# ---------------- exhausted ----------------


def test_fix_loop_exhausted_reports_exhausted(tmp_path):
    (tmp_path / "tests").mkdir()
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(
        runtime, run,
        verification_statuses=[VerificationStatus.FAIL, VerificationStatus.FAIL],
        max_fix_attempts=1,
    )

    report = runner.run(run.run_id, workspace, "Implement X")

    assert report.status is PipelineStatus.EXHAUSTED
    assert runtime.get_run(run.run_id).status is RunStatus.FAILED


# ---------------- no changes ----------------


def test_initial_worker_no_changes_reports_no_changes(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    worker = FakeWorkerAttemptRunner(runtime, run.run_id, changes=False)
    runner, planner, worker, verification, reviewer = _make_runner(runtime, run, worker=worker)

    report = runner.run(run.run_id, workspace, "Implement X")

    assert report.status is PipelineStatus.NO_CHANGES
    assert report.reason == "no_changes"
    # Nothing was verified/reviewed.
    assert verification.calls == []
    assert reviewer.calls == []
    # NO_CHANGES is terminal: run.completed, reason "no_changes".
    run_record = runtime.get_run(run.run_id)
    assert run_record.status is RunStatus.SUCCEEDED
    last_event = runtime.events(run.run_id, limit=200).events[-1]
    assert last_event.type == RunEventType.RUN_COMPLETED
    assert last_event.payload["reason"] == "no_changes"


# ---------------- no verification plan detected ----------------


def test_no_verification_plan_detected_leaves_run_waiting_user(tmp_path):
    # tmp_path has no tests/ dir, no package.json/Cargo.toml/go.mod/.imece.
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(
        runtime, run, review_verdicts=[ReviewVerdict.APPROVED],
    )

    report = runner.run(run.run_id, workspace, "Implement X")

    assert report.status is PipelineStatus.NEEDS_USER
    assert report.reason == "no_verification_plan_detected"
    assert verification.calls == []
    assert reviewer.calls
    # Reviewer ran in advisory mode: no verification_report was attached.
    assert reviewer.requests[0].verification_report is None
    run_record = runtime.get_run(run.run_id)
    assert run_record.status is RunStatus.WAITING_USER
    events = [e.type for e in runtime.events(run.run_id, limit=200).events]
    assert RunEventType.PROPOSAL_READY in events
    assert RunEventType.RUN_WAITING_USER in events


# ---------------- cancellation ----------------


def test_cancel_before_planning_returns_cancelled_without_running_anything(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(runtime, run)
    cancel_event = threading.Event()
    cancel_event.set()

    report = runner.run(run.run_id, workspace, "Implement X", cancel_event=cancel_event)

    assert report.status is PipelineStatus.CANCELLED
    assert not planner.calls
    assert runtime.get_run(run.run_id).status is RunStatus.CANCELLED


class _CancellingWorkerAttemptRunner(FakeWorkerAttemptRunner):
    """Models a real adapter (native AgentSession / ProcessRunner /
    AcpClientRuntime) that observed cancellation MID-EXECUTION and raised
    OperationCancelledError instead of returning normally."""

    def run(self, workspace, request, *, execution_id, cancel_token=None):
        raise OperationCancelledError("worker cancelled mid-execution")


class _CancellingVerificationAttemptRunner(FakeVerificationAttemptRunner):
    def run(self, workspace, plan, *, verification_id, cancel_token=None):
        raise OperationCancelledError("verification cancelled mid-execution")


class _CancellingReviewAttemptRunner(FakeReviewAttemptRunner):
    def run(self, workspace, request, *, review_id, cancel_token=None, pinned_paths=()):
        raise OperationCancelledError("review cancelled mid-execution")


def test_cancel_mid_worker_reports_cancelled_and_never_reaches_verification(tmp_path):
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    workspace = FakeWorkspace(root)
    runtime, run = setup_runtime(tmp_path)
    cancelling_worker = _CancellingWorkerAttemptRunner(runtime, run.run_id)
    runner, planner, worker, verification, reviewer = _make_runner(runtime, run, worker=cancelling_worker)

    report = runner.run(run.run_id, workspace, "Implement X", cancel_event=threading.Event())

    assert report.status is PipelineStatus.CANCELLED
    assert not verification.calls
    assert not reviewer.calls
    assert runtime.get_run(run.run_id).status is RunStatus.CANCELLED
    events = [e.type for e in runtime.events(run.run_id, after_seq=0, limit=200).events]
    assert RunEventType.RUN_CANCELLED in events
    assert events.count(RunEventType.RUN_CANCELLED) == 1


def test_cancel_mid_verification_reports_cancelled_and_never_reaches_review(tmp_path):
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    workspace = FakeWorkspace(root)
    runtime, run = setup_runtime(tmp_path)
    planner = FakePlanAttemptRunner()
    worker = FakeWorkerAttemptRunner(runtime, run.run_id)
    verification = _CancellingVerificationAttemptRunner(runtime, run.run_id, [VerificationStatus.PASS])
    reviewer = FakeReviewAttemptRunner(runtime, run.run_id, [ReviewVerdict.APPROVED])
    change_provider = FakeChangeProvider()
    runner = PipelineRunner(
        runtime, planner=planner, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=change_provider,
    )

    report = runner.run(run.run_id, workspace, "Implement X")

    assert report.status is PipelineStatus.CANCELLED
    assert not reviewer.calls
    assert runtime.get_run(run.run_id).status is RunStatus.CANCELLED


def test_cancel_mid_review_reports_cancelled_and_never_settles_the_gate(tmp_path):
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    workspace = FakeWorkspace(root)
    runtime, run = setup_runtime(tmp_path)
    reviewer = _CancellingReviewAttemptRunner(runtime, run.run_id, [ReviewVerdict.APPROVED])
    runner, planner, worker, verification, _ = _make_runner(runtime, run)
    runner = PipelineRunner(
        runtime, planner=planner, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=FakeChangeProvider(),
    )

    report = runner.run(run.run_id, workspace, "Implement X")

    assert report.status is PipelineStatus.CANCELLED
    run_after = runtime.get_run(run.run_id)
    assert run_after.status is RunStatus.CANCELLED


def test_on_stage_callback_invoked_at_boundaries(tmp_path):
    (tmp_path / "tests").mkdir()
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(runtime, run)
    stages = []

    runner.run(run.run_id, workspace, "Implement X", on_stage=lambda stage, info: stages.append(stage))

    assert stages[0] == "planning"
    assert "working" in stages
    assert "verifying" in stages
    assert "reviewing" in stages
    assert stages[-1] == "done"


# ---------------- infrastructure failure propagation ----------------


def test_worker_port_failure_raises_pipeline_execution_error(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)

    class ThrowingWorker:
        def run(self, workspace, request, *, execution_id, cancel_token=None):
            raise RuntimeError("boom")

    runner, planner, worker, verification, reviewer = _make_runner(runtime, run, worker=ThrowingWorker())

    with pytest.raises(PipelineExecutionError):
        runner.run(run.run_id, workspace, "Implement X")


# ==================== F2 (follow-up on a proposal): continue_with_feedback ====================


def test_continue_with_feedback_happy_path_produces_second_proposal(tmp_path):
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    workspace = FakeWorkspace(root)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(
        runtime, run,
        verification_statuses=[VerificationStatus.PASS, VerificationStatus.PASS],
        review_verdicts=[ReviewVerdict.APPROVED, ReviewVerdict.APPROVED],
    )

    first = runner.run(run.run_id, workspace, "Implement X")
    assert first.status is PipelineStatus.NEEDS_USER
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER

    second = runner.continue_with_feedback(
        run.run_id, workspace, "also handle negative numbers",
        task="Implement X", plan_report_or_text=first.plan_report,
    )

    assert second.status is PipelineStatus.NEEDS_USER
    assert second.reason == "reviewed"
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER
    # Both the worker and the reviewer ran a SECOND time.
    assert len(worker.requests) == 2
    assert len(reviewer.requests) == 2
    # The reviewer's second call saw the follow-up in its task context.
    assert "also handle negative numbers" in reviewer.requests[1].task
    # The canonical evidence chain: run.resumed sits between the two
    # WAITING_USER episodes, and the gate settled again with fresh evidence.
    types = [e.type for e in runtime.events(run.run_id, limit=500).events]
    assert types.count(RunEventType.RUN_RESUMED) == 1
    assert types.count(RunEventType.RUN_WAITING_USER) == 2
    assert types.count(RunEventType.PROPOSAL_READY) == 2


def test_continue_with_feedback_verification_fail_then_fix(tmp_path):
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    workspace = FakeWorkspace(root)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(
        runtime, run,
        verification_statuses=[VerificationStatus.PASS, VerificationStatus.FAIL, VerificationStatus.PASS],
        review_verdicts=[ReviewVerdict.APPROVED, ReviewVerdict.APPROVED],
    )

    first = runner.run(run.run_id, workspace, "Implement X")
    assert first.status is PipelineStatus.NEEDS_USER

    second = runner.continue_with_feedback(
        run.run_id, workspace, "also handle negative numbers", task="Implement X",
    )

    assert second.status is PipelineStatus.NEEDS_USER
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER
    assert len(worker.requests) == 3  # initial + follow-up attempt + fix attempt
    assert len(verification.calls) == 3


def test_continue_with_feedback_advisory_path_when_no_verification_plan(tmp_path):
    # tmp_path has no tests/ dir -> no verification plan detected, for BOTH
    # the initial run and the follow-up.
    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(
        runtime, run, review_verdicts=[ReviewVerdict.APPROVED, ReviewVerdict.APPROVED],
    )

    first = runner.run(run.run_id, workspace, "Implement X")
    assert first.reason == "no_verification_plan_detected"

    second = runner.continue_with_feedback(
        run.run_id, workspace, "also handle negative numbers", task="Implement X",
    )

    assert second.status is PipelineStatus.NEEDS_USER
    assert second.reason == "no_verification_plan_detected"
    assert verification.calls == []
    assert len(reviewer.requests) == 2
    # Advisory review is still verification-less for the follow-up too.
    assert reviewer.requests[1].verification_report is None
    assert "also handle negative numbers" in reviewer.requests[1].task
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER


def test_continue_with_feedback_cancel_reports_cancelled(tmp_path):
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    workspace = FakeWorkspace(root)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(
        runtime, run,
        verification_statuses=[VerificationStatus.PASS, VerificationStatus.PASS],
        review_verdicts=[ReviewVerdict.APPROVED, ReviewVerdict.APPROVED],
    )
    first = runner.run(run.run_id, workspace, "Implement X")
    assert first.status is PipelineStatus.NEEDS_USER

    cancel_event = threading.Event()
    cancel_event.set()  # already cancelled before the follow-up even starts

    second = runner.continue_with_feedback(
        run.run_id, workspace, "also handle negative numbers", task="Implement X",
        cancel_event=cancel_event,
    )

    assert second.status is PipelineStatus.CANCELLED
    # Cancelled BEFORE run.resumed was ever recorded (the cooperative check
    # at the top of continue_with_feedback) -- nothing was mutated, so the
    # Run legitimately stays WAITING_USER (CanonicalPipelineRecorder.
    # cancelled() is an idempotent no-op unless the Run is RUNNING).
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER
    assert len(worker.requests) == 1  # only the initial run's attempt; no follow-up attempt was made


def test_continue_with_feedback_cancel_mid_worker_reports_cancelled(tmp_path):
    """Cancellation observed AFTER run.resumed (mid follow-up worker
    attempt) -- unlike the pre-resume case above, this DOES mutate the Run
    to CANCELLED (see fix_runtime.runner.FixLoopRunner._best_effort_interrupt
    + PipelineRunner.run's own OperationCancelledError handler)."""
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    workspace = FakeWorkspace(root)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(
        runtime, run,
        verification_statuses=[VerificationStatus.PASS],
        review_verdicts=[ReviewVerdict.APPROVED],
    )
    first = runner.run(run.run_id, workspace, "Implement X")
    assert first.status is PipelineStatus.NEEDS_USER

    cancelling_worker = _CancellingWorkerAttemptRunner(runtime, run.run_id)
    runner_2 = PipelineRunner(
        runtime, planner=planner, worker=cancelling_worker, verification=verification, reviewer=reviewer,
        change_provider=FakeChangeProvider(),
    )

    second = runner_2.continue_with_feedback(
        run.run_id, workspace, "also handle negative numbers", task="Implement X",
        cancel_event=threading.Event(),
    )

    assert second.status is PipelineStatus.CANCELLED
    assert runtime.get_run(run.run_id).status is RunStatus.CANCELLED
    events = [e.type for e in runtime.events(run.run_id, limit=500).events]
    assert events.count(RunEventType.RUN_RESUMED) == 1
    assert events.count(RunEventType.RUN_CANCELLED) == 1


def test_continue_with_feedback_twice_in_a_row(tmp_path):
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    workspace = FakeWorkspace(root)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(
        runtime, run,
        verification_statuses=[VerificationStatus.PASS] * 3,
        review_verdicts=[ReviewVerdict.APPROVED] * 3,
    )

    first = runner.run(run.run_id, workspace, "Implement X")
    second = runner.continue_with_feedback(
        run.run_id, workspace, "also handle negative numbers", task="Implement X",
    )
    third = runner.continue_with_feedback(
        run.run_id, workspace, "now rename x to y", task="Implement X",
    )

    assert first.status is second.status is third.status is PipelineStatus.NEEDS_USER
    assert runtime.get_run(run.run_id).status is RunStatus.WAITING_USER
    assert len(worker.requests) == 3
    types = [e.type for e in runtime.events(run.run_id, limit=500).events]
    assert types.count(RunEventType.RUN_RESUMED) == 2
    assert types.count(RunEventType.PROPOSAL_READY) == 3


def test_continue_with_feedback_requires_waiting_user(tmp_path):
    from run_runtime.errors import InvalidRunStateError

    workspace = FakeWorkspace(tmp_path)
    runtime, run = setup_runtime(tmp_path)
    runner, planner, worker, verification, reviewer = _make_runner(runtime, run)
    # Run is still RUNNING (never called run() to reach WAITING_USER).

    with pytest.raises(InvalidRunStateError):
        runner.continue_with_feedback(run.run_id, workspace, "feedback", task="Implement X")
