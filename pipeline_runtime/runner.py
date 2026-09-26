"""PipelineRunner — composes the canonical Planner/Worker/Verification/
Reviewer/FixLoop pieces into one end-to-end "plan -> initial attempt ->
verify -> review -> (fix loop) -> terminal outcome" flow.

Nothing below pipeline_runtime imports it: it sits ABOVE fix_runtime/
planner_runtime/executor_runtime/change_runtime, depending only on their
already-existing small ports (fix_runtime.ports.WorkerAttemptRunner/
VerificationAttemptRunner/ReviewAttemptRunner, change_runtime.ChangeProvider,
pipeline_runtime.ports.PlanAttemptRunner) plus RunRuntime/RunCompletionGate
for canonical Run bookkeeping — exactly the same dependency discipline
fix_runtime.runner.FixLoopRunner already follows.

Flow
----
    plan
      -> detect a deterministic VerificationPlan for the workspace
      -> initial Worker attempt (a fresh execution, InitialWorkerRequest)
      -> capture the change set
         -> no changes at all -> terminal run.completed, reason "no_changes"
         -> a VerificationPlan was detected:
              -> run it
                 -> FAIL/TIMEOUT/ERROR -> hand off to FixLoopRunner with a
                    VERIFICATION_FAIL trigger
                 -> PASS -> run the Reviewer
                    -> NEEDS_FIX -> hand off to FixLoopRunner with a
                       REVIEW_NEEDS_FIX trigger
                    -> APPROVED -> RunCompletionGate.complete_reviewed()
         -> no VerificationPlan was detected: see "No verification plan"
           below

The user has the final word, always
------------------------------------
The IDE's contract is Apply/Reject: even a Reviewer-APPROVED,
Verification-PASSed Run must still wait for the user to apply the proposed
diff to their real project (run_runtime.legacy.LegacyRunCoordinator.
record_proposal_applied/record_proposal_rejected, both of which require
RunStatus.WAITING_USER). A Run settled straight to RUN_COMPLETED
(RunStatus.SUCCEEDED) can never reach that Apply/Reject path at all — it is
already terminal. PipelineRunner therefore constructs its RunCompletionGate
with settlement="await_user" by default (see run_runtime.completion) and
hands that SAME gate instance to the FixLoopRunner it delegates to, so a
Reviewer-APPROVED outcome — whether reached on the first attempt or after
the bounded Fix Loop — always lands the Run in WAITING_USER with a
proposal.ready carrying the full verification/review provenance, never in
RUN_COMPLETED. PipelineRunner reports this uniformly as PipelineStatus.
NEEDS_USER (it inspects the Run's ACTUAL resulting status rather than
hardcoding this, so a caller who explicitly injects a "complete"-settlement
gate still gets an honest PipelineStatus.COMPLETED back).

No verification plan
---------------------
RunCompletionGate.complete_verified/complete_reviewed and FixTrigger both
*require* a VerificationReport — there is no canonical way to "complete" or
even "trigger a fix" for a Run that was never deterministically verified.
Synthesizing a fake always-PASS VerificationReport was considered and
REJECTED: it would inject a false verification fact into the canonical
history that no VerificationRunner ever produced, which is exactly the kind
of lie this system's provenance discipline exists to prevent.

Instead: the Reviewer still runs (in ADVISORY mode — ReviewRequest.
verification_report is Optional precisely for this), and its findings are
attached to the proposal. The Run is then left WAITING_USER via
run_runtime.pipeline.CanonicalPipelineRecorder.needs_user() — the SAME
recorder RunCompletionGate's await_user settlement uses — with reason
"review_advisory" and no verification fields (there is none to report).
This is an honest terminal state — "a human must decide" — rather than a
fabricated automatic PASS.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from agent_runtime.cancellation import CancellationToken, OperationCancelledError
from change_runtime.models import WorkspaceChangeSet
from change_runtime.provider import ChangeProvider
from context_runtime import load_project_rules
from fix_runtime.models import (
    DEFAULT_MAX_FIX_ATTEMPTS,
    FixLoopRequest,
    FixLoopStatus,
    FixTrigger,
    FixTriggerKind,
    InitialWorkerRequest,
    new_fix_execution_id,
)
from fix_runtime.ports import ReviewAttemptRunner, VerificationAttemptRunner, WorkerAttemptRunner
from fix_runtime.prompt import render_initial_worker_input
from fix_runtime.runner import FixLoopRunner
from planner_runtime.models import PlanReport, new_plan_id
from review_runtime.errors import ReviewInputError
from review_runtime.models import ReviewReport, ReviewRequest, ReviewVerdict, new_review_id
from run_runtime.completion import RunCompletionGate
from run_runtime.events import RunEventType
from run_runtime.models import RunStatus
from run_runtime.pipeline import CanonicalPipelineRecorder
from run_runtime.service import RunRuntime
from verification_runtime.models import VerificationPlan, VerificationReport, VerificationStatus, new_verification_id

from pipeline_runtime.errors import PipelineExecutionError, PipelineInputError
from pipeline_runtime.models import PipelineReport, PipelineStatus
from pipeline_runtime.ports import PlanAttemptRunner
from pipeline_runtime.verification_detect import detect_verification_plan

_EXECUTION_LIFECYCLE_TYPES = {
    RunEventType.EXECUTION_STARTED,
    RunEventType.EXECUTION_COMPLETED,
    RunEventType.EXECUTION_FAILED,
}

OnStage = Callable[[str, dict[str, Any]], None]


def _noop_on_stage(stage: str, info: dict[str, Any]) -> None:
    return None


class PipelineRunner:
    def __init__(
        self,
        runtime: RunRuntime,
        *,
        planner: PlanAttemptRunner,
        worker: WorkerAttemptRunner,
        verification: VerificationAttemptRunner,
        reviewer: ReviewAttemptRunner,
        change_provider: ChangeProvider,
        completion_gate: RunCompletionGate | None = None,
        max_fix_attempts: int = DEFAULT_MAX_FIX_ATTEMPTS,
    ) -> None:
        self._runtime = runtime
        self._planner = planner
        self._worker = worker
        self._verification = verification
        self._reviewer = reviewer
        self._change_provider = change_provider
        # See module docstring: the user always has the final Apply/Reject
        # word, so the default gate never settles straight to RUN_COMPLETED.
        self._completion_gate = completion_gate or RunCompletionGate(runtime, settlement="await_user")
        self._max_fix_attempts = max_fix_attempts
        self._fix_loop = FixLoopRunner(
            runtime,
            worker=worker,
            verification=verification,
            reviewer=reviewer,
            change_provider=change_provider,
            completion_gate=self._completion_gate,
        )

    def run(
        self,
        run_id: str,
        workspace,
        task: str,
        *,
        cancel_event: threading.Event | None = None,
        on_stage: OnStage | None = None,
    ) -> PipelineReport:
        if not isinstance(run_id, str) or not run_id:
            raise PipelineInputError("PipelineRunner.run requires a non-empty run_id.")
        if not isinstance(task, str) or not task.strip():
            raise PipelineInputError("PipelineRunner.run requires a non-empty task.")
        on_stage = on_stage or _noop_on_stage
        # A CancellationToken wraps `cancel_event` (or is None if no event was
        # supplied) so it can be forwarded, unchanged in meaning, down to
        # every port call (Planner/Worker/Verification/Reviewer/FixLoop) --
        # see agent_runtime.cancellation. Every `cancel_event.set()` call
        # from an existing caller (webhost/api/run.py) is observed exactly
        # as before; this is purely additive plumbing.
        cancel_token = CancellationToken.from_event(cancel_event)

        try:
            return self._run(run_id, workspace, task, cancel_token, on_stage)
        except OperationCancelledError:
            # Cancellation may now be observed EITHER between stages (see
            # _check_cancel) or from inside an in-progress Worker/
            # Verification/Reviewer/FixLoop port call (see agent_runtime.
            # session.AgentSession, process_runtime.ProcessRunner,
            # acp_runtime.client.AcpClientRuntime) -- either way, the
            # pipeline-level outcome is the same: never run the next stage
            # (reviewer/fix loop), record exactly one run.cancelled, and
            # report PipelineStatus.CANCELLED.
            CanonicalPipelineRecorder(self._runtime, run_id).cancelled()
            on_stage("done", {"status": PipelineStatus.CANCELLED.value})
            return PipelineReport(run_id=run_id, status=PipelineStatus.CANCELLED, reason="cancelled")

    def _run(
        self, run_id: str, workspace, task: str,
        cancel_token: CancellationToken | None, on_stage: OnStage,
    ) -> PipelineReport:
        self._check_cancel(cancel_token)

        # ---------------- 1. plan ----------------
        on_stage("planning", {})
        plan_id = new_plan_id()
        plan_report = self._run_planner(workspace, task, plan_id, cancel_token)

        # ---------------- 2. detect verification plan ----------------
        verification_plan = detect_verification_plan(workspace.root)

        self._check_cancel(cancel_token)

        # ---------------- 3. initial worker attempt ----------------
        on_stage("working", {"plan_id": plan_id})
        execution_id = new_fix_execution_id()
        rendered_input = self._render_initial_input(workspace, task, plan_report, verification_plan)
        worker_request = InitialWorkerRequest(task=task, rendered_input=rendered_input, plan=plan_report.summary)
        worker_result = self._run_worker(workspace, worker_request, execution_id, cancel_token)
        self._require_execution_completed(run_id, worker_result.execution_id)

        # ---------------- 4. capture the change set ----------------
        change_set = self._capture(workspace)
        if not change_set.changed_paths:
            CanonicalPipelineRecorder(self._runtime, run_id).completed_no_changes()
            on_stage("done", {"status": PipelineStatus.NO_CHANGES.value})
            return PipelineReport(
                run_id=run_id, status=PipelineStatus.NO_CHANGES, reason="no_changes",
                plan_report=plan_report, change_set=change_set,
            )

        if verification_plan is None:
            return self._run_needs_user_path(
                run_id, workspace, task, plan_report, change_set, on_stage, cancel_token,
            )

        self._check_cancel(cancel_token)

        # ---------------- 5. verification ----------------
        on_stage("verifying", {})
        verification_id = new_verification_id()
        verification_report = self._run_verification(workspace, verification_plan, verification_id, cancel_token)

        if verification_report.status is not VerificationStatus.PASS:
            trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=verification_report)
            return self._run_fix_loop(
                run_id, workspace, task, plan_report, verification_plan, trigger, on_stage, cancel_token,
            )

        self._check_cancel(cancel_token)

        # ---------------- 6. review ----------------
        on_stage("reviewing", {})
        review_changes = self._capture(workspace)
        review_request = self._build_review_request(task, plan_report, review_changes, verification_report)
        review_id = new_review_id()
        review_report = self._run_reviewer(workspace, review_request, review_id, cancel_token)

        if review_report.verdict is ReviewVerdict.NEEDS_FIX:
            trigger = FixTrigger(
                kind=FixTriggerKind.REVIEW_NEEDS_FIX,
                verification_report=verification_report, review_report=review_report,
            )
            return self._run_fix_loop(
                run_id, workspace, task, plan_report, verification_plan, trigger, on_stage, cancel_token,
            )

        if review_report.verdict is not ReviewVerdict.APPROVED:  # pragma: no cover - exhaustive above
            raise PipelineExecutionError(f"Unexpected review verdict: {review_report.verdict}")

        final_changes = self._capture(workspace)
        if final_changes.diff_sha256 != review_report.diff_sha256:
            CanonicalPipelineRecorder(self._runtime, run_id).failed(error_code="workspace_changed_after_review")
            on_stage("done", {"status": PipelineStatus.FAILED.value})
            return PipelineReport(
                run_id=run_id, status=PipelineStatus.FAILED, reason="workspace_changed_after_review",
                plan_report=plan_report, change_set=final_changes,
                verification_report=verification_report, review_report=review_report,
            )

        self._completion_gate.complete_reviewed(
            run_id, verification_id=verification_id, review_id=review_id,
            current_diff_sha256=final_changes.diff_sha256,
        )
        status = self._status_after_gate_settlement(run_id)
        on_stage("done", {"status": status.value})
        return PipelineReport(
            run_id=run_id, status=status, reason="reviewed",
            plan_report=plan_report, change_set=final_changes,
            verification_report=verification_report, review_report=review_report,
        )

    # ---------------- no-verification-plan path ----------------

    def _run_needs_user_path(
        self, run_id, workspace, task, plan_report, change_set, on_stage, cancel_token,
    ) -> PipelineReport:
        self._check_cancel(cancel_token)

        on_stage("reviewing", {"advisory": True})
        review_request = self._build_review_request(task, plan_report, change_set, None)
        review_id = new_review_id()
        review_report = self._run_reviewer(workspace, review_request, review_id, cancel_token)

        CanonicalPipelineRecorder(self._runtime, run_id).needs_user(payload={
            "reason": "review_advisory",
            "review_id": review_id,
            "review_verdict": review_report.verdict.value,
            "diff_sha256": change_set.diff_sha256,
        })
        on_stage("done", {"status": PipelineStatus.NEEDS_USER.value})
        return PipelineReport(
            run_id=run_id, status=PipelineStatus.NEEDS_USER, reason="no_verification_plan_detected",
            plan_report=plan_report, change_set=change_set, review_report=review_report,
        )

    # ---------------- fix-loop hand-off ----------------

    def _run_fix_loop(
        self, run_id, workspace, task, plan_report, verification_plan, trigger, on_stage, cancel_token,
    ) -> PipelineReport:
        on_stage("fixing", {"trigger_kind": trigger.kind.value})
        request = FixLoopRequest(
            task=task, trigger=trigger, verification_plan=verification_plan,
            plan=plan_report.summary, max_fix_attempts=self._max_fix_attempts,
        )
        try:
            fix_loop_report = self._fix_loop.run(run_id, workspace, request, cancel_token=cancel_token)
        except OperationCancelledError:
            # Never wrapped: the fix loop has already recorded its own
            # fix_attempt.interrupted/fix_loop.interrupted trail (see
            # FixLoopRunner._best_effort_interrupt) and deliberately left the
            # Run RUNNING so PipelineRunner.run's own OperationCancelledError
            # handler records the single Run-level run.cancelled.
            raise
        except Exception as exc:
            raise PipelineExecutionError(f"Fix loop failed: {exc}") from exc

        if fix_loop_report.status is FixLoopStatus.COMPLETED:
            status = self._status_after_gate_settlement(run_id)
        else:
            status = {
                FixLoopStatus.EXHAUSTED: PipelineStatus.EXHAUSTED,
                FixLoopStatus.FAILED: PipelineStatus.FAILED,
            }[fix_loop_report.status]
        on_stage("done", {"status": status.value})
        change_set = None
        if fix_loop_report.diff_sha256 is not None:
            change_set = self._safe_capture(workspace)
        return PipelineReport(
            run_id=run_id, status=status, reason=fix_loop_report.reason,
            plan_report=plan_report, change_set=change_set,
            verification_report=fix_loop_report.verification_report,
            review_report=fix_loop_report.review_report,
            fix_loop_report=fix_loop_report,
        )

    def _status_after_gate_settlement(self, run_id: str) -> PipelineStatus:
        """The gate's `settlement` mode decides what actually happened to the
        Run (await_user -> WAITING_USER, complete -> SUCCEEDED); report that
        HONESTLY rather than assuming which mode is in effect."""
        run_status = self._runtime.get_run(run_id).status
        if run_status is RunStatus.WAITING_USER:
            return PipelineStatus.NEEDS_USER
        return PipelineStatus.COMPLETED

    # ---------------- cancellation ----------------

    def _check_cancel(self, cancel_token: CancellationToken | None) -> None:
        """Cooperative cancellation checked between stages -- raises
        OperationCancelledError, caught once at the top of `run()`.

        This is now only ONE of two ways cancellation is observed: a
        Worker/Verification/Reviewer port call may ALSO raise
        OperationCancelledError from mid-execution (native AgentSession
        checks before each model turn/tool call; ProcessRunner polls while
        waiting; AcpClientRuntime watches the token during an in-flight
        prompt) -- see agent_runtime.cancellation, agent_runtime.session,
        process_runtime.runner, acp_runtime.client. Both paths converge on
        the same `run()`-level handler.
        """
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()

    # ---------------- port call wrappers ----------------

    def _run_planner(
        self, workspace, task: str, plan_id: str, cancel_token: CancellationToken | None = None,
    ) -> PlanReport:
        try:
            report = self._planner.run(workspace, task, plan_id=plan_id, cancel_token=cancel_token)
        except OperationCancelledError:
            raise
        except Exception as exc:
            raise PipelineExecutionError(f"Planner port failed: {exc}") from exc
        if not isinstance(report, PlanReport) or report.plan_id != plan_id:
            raise PipelineExecutionError("Planner port did not return the requested plan_id.")
        return report

    def _render_initial_input(self, workspace, task: str, plan_report: PlanReport, verification_plan) -> str:
        try:
            rules = load_project_rules(workspace.root)
            return render_initial_worker_input(
                task=task, plan=plan_report.summary, verification_plan=verification_plan, rules=rules,
            )
        except Exception as exc:
            raise PipelineExecutionError(f"Initial worker input could not be rendered: {exc}") from exc

    def _run_worker(
        self, workspace, worker_request: InitialWorkerRequest, execution_id: str,
        cancel_token: CancellationToken | None = None,
    ):
        try:
            result = self._worker.run(
                workspace, worker_request, execution_id=execution_id, cancel_token=cancel_token,
            )
        except OperationCancelledError:
            raise
        except Exception as exc:
            raise PipelineExecutionError(f"Worker port failed: {exc}") from exc
        if result.execution_id != execution_id:
            raise PipelineExecutionError(
                "Worker port did not return the requested execution_id."
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
            raise PipelineExecutionError(
                f"No canonical execution lifecycle evidence for execution_id={execution_id!r}."
            )
        if matching[-1].type != RunEventType.EXECUTION_COMPLETED:
            raise PipelineExecutionError(
                f"Initial Worker execution {execution_id!r} did not end in execution.completed "
                f"(found {matching[-1].type!r})."
            )

    def _capture(self, workspace) -> WorkspaceChangeSet:
        try:
            result = self._change_provider.capture(workspace)
        except Exception as exc:
            raise PipelineExecutionError(f"ChangeProvider failed: {exc}") from exc
        if not isinstance(result, WorkspaceChangeSet):
            raise PipelineExecutionError("ChangeProvider.capture() must return a WorkspaceChangeSet.")
        return result

    def _safe_capture(self, workspace) -> WorkspaceChangeSet | None:
        try:
            return self._capture(workspace)
        except PipelineExecutionError:
            return None

    def _run_verification(
        self, workspace, verification_plan: VerificationPlan, verification_id: str,
        cancel_token: CancellationToken | None = None,
    ) -> VerificationReport:
        try:
            report = self._verification.run(
                workspace, verification_plan, verification_id=verification_id, cancel_token=cancel_token,
            )
        except OperationCancelledError:
            raise
        except Exception as exc:
            raise PipelineExecutionError(f"Verification port failed: {exc}") from exc
        if not isinstance(report, VerificationReport) or report.verification_id != verification_id:
            raise PipelineExecutionError("Verification port returned an unexpected verification_id.")
        return report

    def _build_review_request(
        self, task: str, plan_report: PlanReport, change_set: WorkspaceChangeSet,
        verification_report: VerificationReport | None,
    ) -> ReviewRequest:
        try:
            return ReviewRequest(
                task=task, plan=plan_report.summary, diff=change_set.diff,
                verification_report=verification_report,
            )
        except ReviewInputError as exc:
            raise PipelineExecutionError(f"Change set could not be reviewed: {exc}") from exc

    def _run_reviewer(
        self, workspace, review_request: ReviewRequest, review_id: str,
        cancel_token: CancellationToken | None = None,
    ) -> ReviewReport:
        try:
            result = self._reviewer.run(
                workspace, review_request, review_id=review_id, cancel_token=cancel_token,
            )
        except OperationCancelledError:
            raise
        except Exception as exc:
            raise PipelineExecutionError(f"Reviewer port failed: {exc}") from exc
        if not isinstance(result, ReviewReport):
            raise PipelineExecutionError("Reviewer port must return a ReviewReport.")
        return result
