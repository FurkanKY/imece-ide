"""FixLoopRunner — bounded orchestration for the native Fix Loop (3H).

FixLoopRunner is ORCHESTRATION ("who/when"), never a second Agent harness
("how"). It depends only on small ports (WorkerAttemptRunner,
VerificationAttemptRunner, ReviewAttemptRunner, ChangeProvider) plus
RunRuntime/RunCompletionGate/CanonicalFixLoopRecorder for canonical Run
bookkeeping — never on ModelBackend, AgentSession internals, ToolRegistry
construction, or ProcessRunner internals directly.
"""

from __future__ import annotations

from change_runtime.models import WorkspaceChangeSet
from change_runtime.provider import ChangeProvider
from review_runtime.errors import ReviewInputError
from review_runtime.models import ReviewReport, ReviewRequest, ReviewVerdict, new_review_id
from run_runtime.completion import RunCompletionGate
from run_runtime.errors import EventSequenceError
from run_runtime.events import RunEventType
from run_runtime.fix_loop import CanonicalFixLoopRecorder
from run_runtime.service import RunRuntime
from verification_runtime.models import VerificationReport, VerificationStatus, new_verification_id

from fix_runtime.errors import FixLoopExecutionError, FixLoopInputError
from fix_runtime.models import (
    FixLoopReport,
    FixLoopRequest,
    FixLoopStatus,
    FixTrigger,
    FixTriggerKind,
    FixWorkerRequest,
    new_fix_attempt_id,
    new_fix_execution_id,
    new_fix_loop_id,
    validate_fix_loop_id,
)
from fix_runtime.ports import ReviewAttemptRunner, VerificationAttemptRunner, WorkerAttemptResult, WorkerAttemptRunner
from fix_runtime.prompt import render_fix_worker_input

_EXECUTION_LIFECYCLE_TYPES = {
    RunEventType.EXECUTION_STARTED,
    RunEventType.EXECUTION_COMPLETED,
    RunEventType.EXECUTION_FAILED,
}


class FixLoopRunner:
    def __init__(
        self,
        runtime: RunRuntime,
        *,
        worker: WorkerAttemptRunner,
        verification: VerificationAttemptRunner,
        reviewer: ReviewAttemptRunner,
        change_provider: ChangeProvider,
        completion_gate: RunCompletionGate | None = None,
    ) -> None:
        self._runtime = runtime
        self._worker = worker
        self._verification = verification
        self._reviewer = reviewer
        self._change_provider = change_provider
        self._completion_gate = completion_gate or RunCompletionGate(runtime)

    def run(
        self,
        run_id: str,
        workspace,
        request: FixLoopRequest,
        *,
        fix_loop_id: str | None = None,
    ) -> FixLoopReport:
        if not isinstance(request, FixLoopRequest):
            raise FixLoopInputError("FixLoopRunner.run requires a FixLoopRequest.")
        fix_loop_id = validate_fix_loop_id(fix_loop_id) if fix_loop_id is not None else new_fix_loop_id()

        # B. capture current cumulative change set (before the loop starts).
        current_change = self._capture(workspace)

        # C. a REVIEW_NEEDS_FIX trigger must reference the CURRENT cumulative
        # diff, never a stale one — validated BEFORE fix_loop.started so we
        # never act on known-stale reviewer feedback.
        trigger = request.trigger
        if trigger.kind is FixTriggerKind.REVIEW_NEEDS_FIX:
            if current_change.diff_sha256 != trigger.review_report.diff_sha256:
                raise FixLoopInputError(
                    "REVIEW_NEEDS_FIX trigger's review diff_sha256 does not match the "
                    "current workspace change set; refusing to act on stale reviewer feedback."
                )

        recorder = CanonicalFixLoopRecorder(self._runtime, run_id, fix_loop_id=fix_loop_id)
        recorder.start()

        try:
            return self._run_attempts(run_id, workspace, request, fix_loop_id, recorder, trigger)
        except FixLoopExecutionError as exc:
            self._best_effort_fail(run_id, fix_loop_id, recorder, exc)
            raise

    # ---------------- main attempt loop ----------------

    def _run_attempts(
        self, run_id, workspace, request: FixLoopRequest, fix_loop_id: str,
        recorder: CanonicalFixLoopRecorder, trigger: FixTrigger,
    ) -> FixLoopReport:
        current_trigger = trigger
        attempts_used = 0
        final_execution_id: str | None = None
        last_verification_report = None
        last_review_report = None

        for attempt_index in range(1, request.max_fix_attempts + 1):
            attempts_used = attempt_index
            before = self._capture(workspace)

            fix_attempt_id = new_fix_attempt_id()
            worker_execution_id = new_fix_execution_id()

            # fix_attempt.started MUST be committed before the Worker's side
            # effect begins (see milestone spec section 17).
            recorder.attempt_started(
                fix_attempt_id=fix_attempt_id,
                attempt_index=attempt_index,
                trigger_kind=current_trigger.kind.value,
                worker_execution_id=worker_execution_id,
                before_diff_sha256=before.diff_sha256,
            )

            rendered_input = self._render_worker_input(request, current_trigger, attempt_index)
            worker_request = FixWorkerRequest(
                task=request.task, trigger=current_trigger, attempt_index=attempt_index, plan=request.plan,
                rendered_input=rendered_input,
            )
            worker_result = self._run_worker(workspace, worker_request, worker_execution_id)
            self._require_execution_completed(run_id, worker_result.execution_id)

            after = self._capture(workspace)
            changed = after.diff_sha256 != before.diff_sha256

            recorder.attempt_completed(
                fix_attempt_id=fix_attempt_id,
                attempt_index=attempt_index,
                worker_execution_id=worker_execution_id,
                before_diff_sha256=before.diff_sha256,
                after_diff_sha256=after.diff_sha256,
                changed=changed,
            )
            final_execution_id = worker_execution_id

            if not changed:
                recorder.exhausted(
                    reason="stalled", attempts_used=attempts_used, max_fix_attempts=request.max_fix_attempts,
                )
                self._completion_gate.fail_fix_loop(run_id, fix_loop_id=fix_loop_id)
                return FixLoopReport(
                    fix_loop_id=fix_loop_id, status=FixLoopStatus.EXHAUSTED, attempts_used=attempts_used,
                    reason="stalled", final_execution_id=final_execution_id, diff_sha256=after.diff_sha256,
                )

            verification_id = new_verification_id()
            verification_report = self._run_verification(workspace, request.verification_plan, verification_id)
            last_verification_report = verification_report
            status = verification_report.status

            if status is VerificationStatus.ERROR:
                recorder.failed(reason="verification_error")
                self._completion_gate.fail_fix_loop(run_id, fix_loop_id=fix_loop_id)
                return FixLoopReport(
                    fix_loop_id=fix_loop_id, status=FixLoopStatus.FAILED, attempts_used=attempts_used,
                    reason="verification_error", final_execution_id=final_execution_id,
                    verification_report=verification_report, diff_sha256=after.diff_sha256,
                )
            if status is VerificationStatus.TIMEOUT:
                recorder.failed(reason="verification_timeout")
                self._completion_gate.fail_fix_loop(run_id, fix_loop_id=fix_loop_id)
                return FixLoopReport(
                    fix_loop_id=fix_loop_id, status=FixLoopStatus.FAILED, attempts_used=attempts_used,
                    reason="verification_timeout", final_execution_id=final_execution_id,
                    verification_report=verification_report, diff_sha256=after.diff_sha256,
                )
            if status is VerificationStatus.FAIL:
                if attempt_index == request.max_fix_attempts:
                    recorder.exhausted(
                        reason="budget_exhausted", attempts_used=attempts_used,
                        max_fix_attempts=request.max_fix_attempts,
                    )
                    self._completion_gate.fail_fix_loop(run_id, fix_loop_id=fix_loop_id)
                    return FixLoopReport(
                        fix_loop_id=fix_loop_id, status=FixLoopStatus.EXHAUSTED, attempts_used=attempts_used,
                        reason="budget_exhausted", final_execution_id=final_execution_id,
                        verification_report=verification_report, diff_sha256=after.diff_sha256,
                    )
                current_trigger = FixTrigger(
                    kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=verification_report,
                )
                continue

            if status is not VerificationStatus.PASS:  # pragma: no cover - exhaustive above
                raise FixLoopExecutionError(f"Unexpected verification status: {status}")

            # Reviewer only runs after PASS; capture the cumulative diff again
            # (attempts don't review only their own local delta).
            review_changes = self._capture(workspace)
            review_request = self._build_review_request(request, review_changes, verification_report)
            review_id = new_review_id()
            review_report = self._run_reviewer(workspace, review_request, review_id)
            self._validate_review_provenance(review_report, review_id, verification_id, review_changes)
            last_review_report = review_report

            if review_report.verdict is ReviewVerdict.NEEDS_FIX:
                if attempt_index == request.max_fix_attempts:
                    recorder.exhausted(
                        reason="budget_exhausted", attempts_used=attempts_used,
                        max_fix_attempts=request.max_fix_attempts,
                    )
                    self._completion_gate.fail_fix_loop(run_id, fix_loop_id=fix_loop_id)
                    return FixLoopReport(
                        fix_loop_id=fix_loop_id, status=FixLoopStatus.EXHAUSTED, attempts_used=attempts_used,
                        reason="budget_exhausted", final_execution_id=final_execution_id,
                        verification_report=verification_report, review_report=review_report,
                        diff_sha256=review_changes.diff_sha256,
                    )
                current_trigger = FixTrigger(
                    kind=FixTriggerKind.REVIEW_NEEDS_FIX,
                    verification_report=verification_report, review_report=review_report,
                )
                continue

            if review_report.verdict is not ReviewVerdict.APPROVED:  # pragma: no cover - exhaustive above
                raise FixLoopExecutionError(f"Unexpected review verdict: {review_report.verdict}")

            # Reviewer is read-only, but the workspace could still have
            # mutated between context capture and its return: re-capture and
            # require the SHA it actually approved is still current.
            final_changes = self._capture(workspace)
            if final_changes.diff_sha256 != review_report.diff_sha256:
                recorder.failed(reason="workspace_changed_after_review")
                self._completion_gate.fail_fix_loop(run_id, fix_loop_id=fix_loop_id)
                return FixLoopReport(
                    fix_loop_id=fix_loop_id, status=FixLoopStatus.FAILED, attempts_used=attempts_used,
                    reason="workspace_changed_after_review", final_execution_id=final_execution_id,
                    verification_report=verification_report, review_report=review_report,
                    diff_sha256=final_changes.diff_sha256,
                )

            recorder.completed(
                attempts_used=attempts_used, final_execution_id=final_execution_id,
                verification_id=verification_id, review_id=review_id, diff_sha256=final_changes.diff_sha256,
            )
            self._completion_gate.complete_reviewed(
                run_id, verification_id=verification_id, review_id=review_id,
                current_diff_sha256=final_changes.diff_sha256,
            )
            return FixLoopReport(
                fix_loop_id=fix_loop_id, status=FixLoopStatus.COMPLETED, attempts_used=attempts_used,
                reason="reviewed", final_execution_id=final_execution_id,
                verification_report=verification_report, review_report=review_report,
                diff_sha256=final_changes.diff_sha256,
            )

        raise FixLoopExecutionError(  # pragma: no cover - every branch above returns
            "Fix loop attempt loop exited without a terminal outcome."
        )

    # ---------------- port call wrappers (translate infra failures) ----------------

    def _capture(self, workspace) -> WorkspaceChangeSet:
        try:
            result = self._change_provider.capture(workspace)
        except FixLoopExecutionError:
            raise
        except Exception as exc:
            raise FixLoopExecutionError(f"ChangeProvider failed: {exc}") from exc
        if not isinstance(result, WorkspaceChangeSet):
            raise FixLoopExecutionError(
                "ChangeProvider.capture() must return a WorkspaceChangeSet."
            )
        return result

    def _render_worker_input(self, request: FixLoopRequest, trigger: FixTrigger, attempt_index: int) -> str:
        try:
            return render_fix_worker_input(
                task=request.task, plan=request.plan, trigger=trigger,
                attempt_index=attempt_index, max_fix_attempts=request.max_fix_attempts,
            )
        except Exception as exc:
            raise FixLoopExecutionError(f"Fix worker input could not be rendered: {exc}") from exc

    def _run_worker(self, workspace, worker_request: FixWorkerRequest, execution_id: str) -> WorkerAttemptResult:
        try:
            result = self._worker.run(workspace, worker_request, execution_id=execution_id)
        except Exception as exc:
            raise FixLoopExecutionError(f"Worker port failed: {exc}") from exc
        if not isinstance(result, WorkerAttemptResult) or result.execution_id != execution_id:
            raise FixLoopExecutionError(
                "Worker port did not return the requested execution_id; each fix "
                "attempt must use a fresh execution_id and confirm it."
            )
        return result

    def _require_execution_completed(self, run_id: str, execution_id: str) -> None:
        matching = []
        after_seq = 0
        while True:
            page = self._runtime.events(run_id, after_seq=after_seq, limit=200)
            for event in page.events:
                if event.execution_id == execution_id and event.type in _EXECUTION_LIFECYCLE_TYPES:
                    matching.append(event)
            if not page.has_more:
                break
            after_seq = page.events[-1].seq
        if not matching:
            raise FixLoopExecutionError(
                f"No canonical execution lifecycle evidence for execution_id={execution_id!r}."
            )
        if matching[-1].type != RunEventType.EXECUTION_COMPLETED:
            raise FixLoopExecutionError(
                f"Worker execution {execution_id!r} did not end in execution.completed "
                f"(found {matching[-1].type!r})."
            )

    def _run_verification(self, workspace, verification_plan, verification_id: str):
        try:
            report = self._verification.run(workspace, verification_plan, verification_id=verification_id)
        except Exception as exc:
            raise FixLoopExecutionError(f"Verification port failed: {exc}") from exc
        if not isinstance(report, VerificationReport):
            raise FixLoopExecutionError("Verification port must return a VerificationReport.")
        if report.verification_id != verification_id:
            raise FixLoopExecutionError("Verification port returned an unexpected verification_id.")
        return report

    def _build_review_request(
        self, request: FixLoopRequest, review_changes: WorkspaceChangeSet, verification_report,
    ) -> ReviewRequest:
        try:
            return ReviewRequest(
                task=request.task, plan=request.plan, diff=review_changes.diff,
                verification_report=verification_report,
            )
        except ReviewInputError as exc:
            raise FixLoopExecutionError(f"Cumulative change set could not be reviewed: {exc}") from exc

    def _run_reviewer(self, workspace, review_request: ReviewRequest, review_id: str):
        try:
            result = self._reviewer.run(workspace, review_request, review_id=review_id)
        except Exception as exc:
            raise FixLoopExecutionError(f"Reviewer port failed: {exc}") from exc
        if not isinstance(result, ReviewReport):
            raise FixLoopExecutionError("Reviewer port must return a ReviewReport.")
        return result

    @staticmethod
    def _validate_review_provenance(
        review_report, review_id: str, verification_id: str, review_changes: WorkspaceChangeSet,
    ) -> None:
        if (
            review_report.review_id != review_id
            or review_report.verification_id != verification_id
            or review_report.verification_status != "pass"
            or review_report.diff_sha256 != review_changes.diff_sha256
        ):
            raise FixLoopExecutionError(
                "Reviewer port returned evidence that violates the fix loop's provenance contract."
            )

    # ---------------- infrastructure-failure best effort ----------------

    def _best_effort_fail(
        self, run_id: str, fix_loop_id: str, recorder: CanonicalFixLoopRecorder, exc: Exception,
    ) -> None:
        try:
            if recorder.has_active_attempt:
                recorder.attempt_interrupted(
                    reason="infrastructure_error",
                    error_type=type(exc).__name__,
                    error_message=str(exc).replace("\x00", "")[:2000],
                )
        except EventSequenceError:
            raise
        except Exception:
            return
        try:
            recorder.failed(
                reason="infrastructure_error",
                error_type=type(exc).__name__,
                error_message=str(exc).replace("\x00", "")[:2000],
            )
        except EventSequenceError:
            raise
        except Exception:
            return
        try:
            self._completion_gate.fail_fix_loop(run_id, fix_loop_id=fix_loop_id)
        except EventSequenceError:
            raise
        except Exception:
            return
