"""PipelineRunner algorithm tests using fake Planner/Worker/Verification/
Reviewer ports and a fake ChangeProvider (unit-level orchestration tests;
the real adapters are covered in tests/test_pipeline_integration.py)."""

import hashlib
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

    def run(self, workspace, task, *, plan_id):
        self.calls.append(plan_id)
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

    def run(self, workspace: FakeWorkspace, request, *, execution_id: str) -> WorkerAttemptResult:
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

    def run(self, workspace, plan, *, verification_id):
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

    def run(self, workspace, request, *, review_id):
        self.calls.append(review_id)
        self.requests.append(request)
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
        def run(self, workspace, request, *, execution_id):
            raise RuntimeError("boom")

    runner, planner, worker, verification, reviewer = _make_runner(runtime, run, worker=ThrowingWorker())

    with pytest.raises(PipelineExecutionError):
        runner.run(run.run_id, workspace, "Implement X")
