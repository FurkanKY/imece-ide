"""FixLoopRunner — bounded orchestration for the native Fix Loop (3H).

FixLoopRunner is ORCHESTRATION ("who/when"), never a second Agent harness
("how"). It depends only on small ports (WorkerAttemptRunner,
VerificationAttemptRunner, ReviewAttemptRunner, ChangeProvider) plus
RunRuntime/RunCompletionGate/CanonicalFixLoopRecorder for canonical Run
bookkeeping — never on ModelBackend, AgentSession internals, ToolRegistry
construction, or ProcessRunner internals directly.
"""

from __future__ import annotations

from agent_runtime.cancellation import CancellationToken, OperationCancelledError
from change_runtime.models import WorkspaceChangeSet
from change_runtime.provider import ChangeProvider
from context_runtime import load_project_rules
from decision_runtime.gate import VerificationFailureGate
from decision_runtime.triage import TriageAction, TriageOutcome
from review_runtime.errors import ReviewInputError
from review_runtime.models import ReviewReport, ReviewRequest, ReviewVerdict, new_review_id
from run_runtime.completion import RunCompletionGate
from run_runtime.errors import EventSequenceError
from run_runtime.events import RunEventType
from run_runtime.fix_loop import CanonicalFixLoopRecorder
from run_runtime.service import RunRuntime
from verification_runtime.models import VerificationReport, VerificationStatus, new_verification_id

from fix_runtime.errors import FixLoopCancelledError, FixLoopExecutionError, FixLoopInputError
from fix_runtime.models import (
    FixLoopReport,
    FixLoopRequest,
    FixLoopStatus,
    FixTrigger,
    FixTriggerKind,
    FixWorkerRequest,
    _capture_fix_worker_render_context,
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
        decision_gate: VerificationFailureGate | None = None,
    ) -> None:
        self._runtime = runtime
        self._worker = worker
        self._verification = verification
        self._reviewer = reviewer
        self._change_provider = change_provider
        self._completion_gate = completion_gate or RunCompletionGate(runtime)
        # docs/JEV-DESIGN.md Spike S1: None (the default, "decision_layer":
        # "off" -- see engine_factory.build_verification_failure_gate) means
        # every verification FAIL is handled exactly as before this slice; no
        # decision_runtime import side effect happens on that path at all.
        self._decision_gate = decision_gate

    def run(
        self,
        run_id: str,
        workspace,
        request: FixLoopRequest,
        *,
        fix_loop_id: str | None = None,
        cancel_token: CancellationToken | None = None,
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
        elif trigger.kind is FixTriggerKind.USER_FEEDBACK:
            if current_change.diff_sha256 != trigger.diff_sha256:
                raise FixLoopInputError(
                    "USER_FEEDBACK trigger's diff_sha256 does not match the current "
                    "workspace change set; refusing to act on a stale follow-up instruction."
                )

        recorder = CanonicalFixLoopRecorder(self._runtime, run_id, fix_loop_id=fix_loop_id)
        recorder.start()

        try:
            return self._run_attempts(run_id, workspace, request, fix_loop_id, recorder, trigger, cancel_token)
        except OperationCancelledError as exc:
            self._best_effort_interrupt(fix_loop_id, recorder, exc)
            if isinstance(exc, FixLoopCancelledError):
                raise
            raise FixLoopCancelledError(f"Fix loop cancelled: {exc}") from exc
        except FixLoopExecutionError as exc:
            self._best_effort_fail(run_id, fix_loop_id, recorder, exc)
            raise

    # ---------------- main attempt loop ----------------

    def _run_attempts(
        self, run_id, workspace, request: FixLoopRequest, fix_loop_id: str,
        recorder: CanonicalFixLoopRecorder, trigger: FixTrigger,
        cancel_token: CancellationToken | None,
    ) -> FixLoopReport:
        current_trigger = trigger
        current_classification: str | None = None
        attempts_used = 0
        final_execution_id: str | None = None
        last_verification_report = None
        last_review_report = None

        for attempt_index in range(1, request.max_fix_attempts + 1):
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
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

            rendered_input = self._render_worker_input(
                workspace, request, current_trigger, attempt_index, current_classification,
            )
            attempt_classification = current_classification
            current_classification = None  # consumed: only applies to the attempt it was set for
            worker_request = FixWorkerRequest(
                task=request.task, trigger=current_trigger, attempt_index=attempt_index, plan=request.plan,
                rendered_input=rendered_input,
                render_context=_capture_fix_worker_render_context(
                    request.max_fix_attempts, request.pinned_paths, attempt_classification,
                ),
            )
            worker_result = self._run_worker(workspace, worker_request, worker_execution_id, cancel_token)
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
            verification_report = self._run_verification(
                workspace, request.verification_plan, verification_id, cancel_token,
            )
            last_verification_report = verification_report
            status = verification_report.status

            # docs/JEV-DESIGN.md Spike S1: triage a real FAIL BEFORE deciding
            # whether to start a fix attempt. RERUN_VERIFICATION_ONCE is
            # applied right here -- it replaces verification_report/status
            # with the rerun's own result and then falls through the SAME
            # ERROR/TIMEOUT/FAIL/PASS handling below unconditionally (only
            # ONE rerun ever happens per triage; see the design doc's
            # "re-run the check once before deciding"). Any other outcome is
            # consumed only in the FAIL branch below (never for ERROR/TIMEOUT).
            decision_outcome: TriageOutcome | None = None
            if status is VerificationStatus.FAIL and self._decision_gate is not None:
                decision_outcome = self._evaluate_decision_gate(workspace, request, verification_report, after)
                if decision_outcome is not None and decision_outcome.action is TriageAction.RERUN_VERIFICATION_ONCE:
                    rerun_id = new_verification_id()
                    verification_report = self._run_verification(
                        workspace, request.verification_plan, rerun_id, cancel_token,
                    )
                    verification_id = rerun_id
                    last_verification_report = verification_report
                    status = verification_report.status
                    decision_outcome = None

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
                if decision_outcome is not None and decision_outcome.action is TriageAction.NEEDS_USER:
                    # Deliberately NOT completion_gate.fail_fix_loop(): the Run
                    # is left RUNNING so pipeline_runtime.PipelineRunner can
                    # run an advisory review and settle WAITING_USER itself
                    # (the proposal stays viewable) -- see FixLoopStatus.NEEDS_USER.
                    recorder.exhausted(
                        reason="needs_user_environment", attempts_used=attempts_used,
                        max_fix_attempts=request.max_fix_attempts,
                        decision_failure_kind=decision_outcome.failure_kind,
                    )
                    return FixLoopReport(
                        fix_loop_id=fix_loop_id, status=FixLoopStatus.NEEDS_USER, attempts_used=attempts_used,
                        reason="needs_user_environment", final_execution_id=final_execution_id,
                        verification_report=verification_report, diff_sha256=after.diff_sha256,
                        needs_user_message=decision_outcome.needs_user_message,
                    )
                if decision_outcome is not None and decision_outcome.action is TriageAction.MARK_PRE_EXISTING:
                    # Same "leave it to the caller" contract as NEEDS_USER
                    # above (see FixLoopStatus.NEEDS_USER's docstring).
                    recorder.exhausted(
                        reason="pre_existing_failure", attempts_used=attempts_used,
                        max_fix_attempts=request.max_fix_attempts,
                        decision_failure_kind=decision_outcome.failure_kind,
                    )
                    return FixLoopReport(
                        fix_loop_id=fix_loop_id, status=FixLoopStatus.NEEDS_USER, attempts_used=attempts_used,
                        reason="pre_existing_failure", final_execution_id=final_execution_id,
                        verification_report=verification_report, diff_sha256=after.diff_sha256,
                    )
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
                # CODE_BUG/TEST_NEEDS_UPDATE (or the gate off/low-confidence/
                # no-strong-signal case, where decision_outcome is None or
                # CONTINUE_FIX_LOOP): today's behaviour, optionally annotated
                # with the decision layer's classification for the next
                # attempt's prompt (docs/JEV-DESIGN.md action table).
                if decision_outcome is not None and decision_outcome.action is TriageAction.CONTINUE_FIX_LOOP:
                    current_classification = decision_outcome.failure_kind
                continue

            if status is not VerificationStatus.PASS:  # pragma: no cover - exhaustive above
                raise FixLoopExecutionError(f"Unexpected verification status: {status}")

            # Reviewer only runs after PASS; capture the cumulative diff again
            # (attempts don't review only their own local delta).
            review_changes = self._capture(workspace)
            review_request = self._build_review_request(request, review_changes, verification_report)
            review_id = new_review_id()
            review_report = self._run_reviewer(
                workspace, review_request, review_id, cancel_token, pinned_paths=request.pinned_paths,
            )
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

    def _render_worker_input(
        self, workspace, request: FixLoopRequest, trigger: FixTrigger, attempt_index: int,
        classification: str | None = None,
    ) -> str:
        try:
            rules = load_project_rules(workspace.root)
            return render_fix_worker_input(
                task=request.task, plan=request.plan, trigger=trigger,
                attempt_index=attempt_index, max_fix_attempts=request.max_fix_attempts,
                rules=rules, pinned_paths=request.pinned_paths, classification=classification,
            )
        except Exception as exc:
            raise FixLoopExecutionError(f"Fix worker input could not be rendered: {exc}") from exc

    def _evaluate_decision_gate(
        self, workspace, request: FixLoopRequest, verification_report, after: WorkspaceChangeSet,
    ) -> TriageOutcome | None:
        """design rule 1: the decision layer is an accelerator, never a hard
        dependency -- ANY failure evaluating it (baseline rerun, a backend
        error the gate itself didn't already catch, a bug) must degrade to
        None ("no triage evidence"), handled identically to the gate being
        off: today's fix-loop behaviour, never a pipeline crash."""
        try:
            return self._decision_gate.evaluate(
                workspace=workspace, verification_plan=request.verification_plan,
                verification_report=verification_report, changed_paths=after.changed_paths,
            )
        except Exception:
            return None

    def _run_worker(
        self, workspace, worker_request: FixWorkerRequest, execution_id: str,
        cancel_token: CancellationToken | None = None,
    ) -> WorkerAttemptResult:
        try:
            result = self._worker.run(workspace, worker_request, execution_id=execution_id, cancel_token=cancel_token)
        except OperationCancelledError:
            raise
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

    def _run_verification(
        self, workspace, verification_plan, verification_id: str,
        cancel_token: CancellationToken | None = None,
    ):
        try:
            report = self._verification.run(
                workspace, verification_plan, verification_id=verification_id, cancel_token=cancel_token,
            )
        except OperationCancelledError:
            raise
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
            task_for_review = request.review_task if request.review_task is not None else request.task
            return ReviewRequest(
                task=task_for_review, plan=request.plan, diff=review_changes.diff,
                verification_report=verification_report,
            )
        except ReviewInputError as exc:
            raise FixLoopExecutionError(f"Cumulative change set could not be reviewed: {exc}") from exc

    def _run_reviewer(
        self, workspace, review_request: ReviewRequest, review_id: str,
        cancel_token: CancellationToken | None = None, pinned_paths=(),
    ):
        try:
            result = self._reviewer.run(
                workspace, review_request, review_id=review_id, cancel_token=cancel_token,
                pinned_paths=pinned_paths,
            )
        except OperationCancelledError:
            raise
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

    # ---------------- cancellation best effort ----------------

    def _best_effort_interrupt(
        self, fix_loop_id: str, recorder: CanonicalFixLoopRecorder, exc: Exception,
    ) -> None:
        """Settle the fix loop's own canonical trail on cancellation, WITHOUT
        touching the completion gate: the Run itself is deliberately left
        RUNNING here so pipeline_runtime.PipelineRunner (the only caller that
        hands FixLoopRunner a cancel_token) can record the Run-level
        run.cancelled outcome itself -- recording fail_fix_loop() here would
        settle RUN_FAILED and make that subsequent run.cancelled impossible."""
        try:
            if recorder.has_active_attempt:
                recorder.attempt_interrupted(reason="cancelled", error_type=type(exc).__name__)
        except EventSequenceError:
            raise
        except Exception:
            return
        try:
            recorder.interrupted(reason="cancelled")
        except EventSequenceError:
            raise
        except Exception:
            return

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
