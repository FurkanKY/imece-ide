"""AcpReviewAttemptRunner -- drives a read-only account-authenticated ACP
agent (Claude Code / Codex over the Agent Client Protocol) through exactly
one fresh Reviewer attempt, implementing fix_runtime.ports.ReviewAttemptRunner.

This is the ACP counterpart of executor_runtime.native_reviewer.
NativeReviewAttemptAdapter (itself a thin adapter over review_runtime.
ReviewerRunner): SAME prompt construction (review_runtime's
render_initial_review_input + REVIEWER_SYSTEM_INSTRUCTIONS), SAME strict
parser (review_runtime.parser.parse_review_decision), SAME ContextEngine
budget, and SAME canonical recorder (run_runtime.reviewer.
CanonicalReviewEventSink) -- only the transport differs (ACP subprocess
instead of a ModelBackend-driven AgentSession).

See executor_runtime.acp_semantic and pipeline_runtime.acp_planner (its
Planner sibling, which shares that module) for the read-only enforcement
layers, the final-text recovery strategy, and the rationale for dropping
intermediate ACP session updates instead of mapping them into synthetic
agent_runtime lifecycle events. Reviewer activity here, exactly as in the
native path, NEVER produces execution.* events and NEVER sets
RunEvent.execution_id -- only review.* (see run_runtime.reviewer).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from agent_runtime.cancellation import CancellationToken, OperationCancelledError
from agent_runtime.events import ExecutionCompleted, ExecutionStarted
from context_runtime import ContextEngine, load_project_rules
from context_runtime.ranking import MAX_QUERY_CHARS
from review_runtime.errors import ReviewProtocolError
from review_runtime.models import ReviewReport, ReviewRequest, validate_review_id
from review_runtime.parser import parse_review_decision
from review_runtime.prompt import REVIEWER_SYSTEM_INSTRUCTIONS, render_initial_review_input
from review_runtime.runner import _REVIEW_CONTEXT_BUDGET
from run_runtime.agent_activity import record_agent_activity
from run_runtime.reviewer import CanonicalReviewEventSink
from run_runtime.service import RunRuntime

from acp_runtime.models import AcpClientLimits
from executor_runtime.acp_semantic import run_acp_semantic_prompt, wrap_system_instructions_for_acp_prompt
from executor_runtime.acp_worker import AcpWorkerLaunchProfile, resolve_acp_worker_launch
from executor_runtime.errors import (
    ExecutorAdapterCancelledError,
    ExecutorAdapterExecutionError,
    ExecutorAdapterInputError,
)


class AcpReviewAttemptRunner:
    """Runs exactly one fresh semantic review attempt via a local ACP agent
    process.

    Concrete production implementation of fix_runtime.ports.ReviewAttemptRunner.
    """

    def __init__(
        self,
        runtime: RunRuntime,
        run_id: str,
        launch_profile: AcpWorkerLaunchProfile,
        acp_client,
        *,
        context_engine: ContextEngine | None = None,
        limits: AcpClientLimits | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ExecutorAdapterInputError("AcpReviewAttemptRunner.run_id must be a non-empty string.")
        if not isinstance(launch_profile, AcpWorkerLaunchProfile):
            raise ExecutorAdapterInputError("AcpReviewAttemptRunner.launch_profile must be an AcpWorkerLaunchProfile.")
        if not callable(getattr(acp_client, "run", None)):
            raise ExecutorAdapterInputError("AcpReviewAttemptRunner.acp_client must expose a callable run().")
        if limits is not None and not isinstance(limits, AcpClientLimits):
            raise ExecutorAdapterInputError("AcpReviewAttemptRunner.limits must be an AcpClientLimits.")
        self._runtime = runtime
        self._run_id = run_id
        self._launch_profile = launch_profile
        self._acp_client = acp_client
        self._context_engine = context_engine or ContextEngine()
        self._limits = limits or AcpClientLimits()

    @property
    def run_id(self) -> str:
        return self._run_id

    def run(
        self, workspace, request: ReviewRequest, *, review_id: str,
        cancel_token: CancellationToken | None = None, pinned_paths: Sequence[str] = (),
    ) -> ReviewReport:
        if not isinstance(request, ReviewRequest):
            raise ExecutorAdapterInputError("AcpReviewAttemptRunner.run requires a ReviewRequest.")
        review_id = validate_review_id(review_id)

        rules = load_project_rules(workspace.root)
        try:
            sink = CanonicalReviewEventSink(
                self._runtime, self._run_id, review_id=review_id,
                rules_sha256=rules.sha256 if rules is not None else None,
            )
        except ValueError as exc:
            raise ExecutorAdapterInputError(f"Cannot construct canonical Reviewer sink: {exc}") from exc

        query = request.task[:MAX_QUERY_CHARS]
        context_pack = self._context_engine.build(workspace, query, _REVIEW_CONTEXT_BUDGET, pinned_paths=pinned_paths)
        rendered_task_input = render_initial_review_input(
            task=request.task,
            plan=request.plan,
            diff=request.diff,
            verification_report=request.verification_report,
            context_pack=context_pack,
            rules=rules,
        )
        acp_prompt = wrap_system_instructions_for_acp_prompt(REVIEWER_SYSTEM_INSTRUCTIONS, rendered_task_input)

        launch_spec = resolve_acp_worker_launch(self._launch_profile)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ExecutorAdapterExecutionError(
                "AcpReviewAttemptRunner.run cannot execute inside a running event loop."
            )

        transient_execution_id = f"acp_review_exec_{review_id}"
        sink.emit(ExecutionStarted(execution_id=transient_execution_id, task=request.task))

        def _activity(**kwargs) -> None:
            # F1 (live agent activity): best-effort, advisory only -- never
            # allowed to fail the Reviewer attempt itself (see
            # run_runtime.agent_activity's module docstring).
            try:
                event = record_agent_activity(
                    self._runtime, self._run_id, execution_id=transient_execution_id, **kwargs,
                )
                # See pipeline_runtime.acp_planner's identical comment: keeps
                # sink's own optimistic cursor from going stale because of
                # this interleaved, independently-appended notice.
                sink.note_external_append(event.seq)
            except Exception:
                pass

        try:
            final_text, _acp_result = run_acp_semantic_prompt(
                acp_client=self._acp_client,
                launch_spec=launch_spec,
                workspace=workspace,
                prompt=acp_prompt,
                limits=self._limits,
                cancel_token=cancel_token,
                role="reviewer",
                activity_recorder=_activity,
            )
        except OperationCancelledError as cancellation:
            self._fail(sink, review_id, cancellation)
            raise ExecutorAdapterCancelledError("Reviewer ACP session cancelled.") from cancellation
        except Exception as original_failure:
            self._fail(sink, review_id, original_failure)
            raise ExecutorAdapterExecutionError("Reviewer ACP session failed.") from original_failure

        sink.emit(
            ExecutionCompleted(
                execution_id=transient_execution_id,
                final_text=final_text,
                model_turns=0,
                tool_calls=0,
                tool_errors=0,
                input_tokens=0,
                output_tokens=0,
                cost_usd=None,
            )
        )

        try:
            decision = parse_review_decision(final_text)
        except ReviewProtocolError as exc:
            self._fail(sink, review_id, exc)
            raise ExecutorAdapterExecutionError(f"Reviewer ACP output failed protocol parsing: {exc}") from exc

        verification_id = None
        verification_status = None
        if request.verification_report is not None:
            verification_id = request.verification_report.verification_id
            verification_status = request.verification_report.status.value

        report = ReviewReport(
            review_id=review_id,
            verdict=decision.verdict,
            summary=decision.summary,
            findings=decision.findings,
            repository_fingerprint=context_pack.repository_fingerprint,
            diff_sha256=request.diff_sha256,
            verification_id=verification_id,
            verification_status=verification_status,
        )
        sink.complete(report)
        return report

    @staticmethod
    def _fail(sink: CanonicalReviewEventSink, review_id: str, error: Exception) -> None:
        try:
            sink.fail(review_id, type(error).__name__, str(error))
        except Exception as terminal_failure:
            raise ExecutorAdapterExecutionError(
                "Reviewer ACP execution and terminal failure recording both failed."
            ) from terminal_failure
