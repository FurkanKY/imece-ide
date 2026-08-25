"""Provider-neutral ReviewerRunner — reuses AgentSession, never a second harness."""

from __future__ import annotations

from agent_runtime.backend import ModelBackend
from agent_runtime.errors import AgentRuntimeError
from agent_runtime.models import AgentLimits, ApprovalPause
from agent_runtime.session import AgentSession
from context_runtime import ContextEngine
from context_runtime.models import ContextBudget
from context_runtime.ranking import MAX_QUERY_CHARS
from tool_runtime.models import ToolExecutionContext
from tool_runtime.policy import PermissionRule, PolicyEvaluator
from tool_runtime.models import PermissionEffect
from tool_runtime.registry import ToolRegistry
from tool_runtime.tools.repository import register_repository_tools
from tool_runtime.tools.workspace_files import register_workspace_read_tools

from review_runtime.errors import ReviewExecutionError, ReviewInputError, ReviewProtocolError
from review_runtime.models import ReviewReport, ReviewRequest, new_review_id, validate_review_id
from review_runtime.parser import parse_review_decision
from review_runtime.prompt import REVIEWER_SYSTEM_INSTRUCTIONS, render_initial_review_input
from review_runtime.recording import NullReviewRecorder, ReviewRecorder

_REVIEW_CONTEXT_BUDGET = ContextBudget(total_chars=24_000, map_chars=6_000, max_segment_chars=6_000)

_DEFAULT_REVIEWER_LIMITS = AgentLimits(
    max_model_turns=8,
    max_tool_calls=20,
    max_consecutive_tool_errors=4,
)


def _reviewer_policy() -> PolicyEvaluator:
    return PolicyEvaluator(
        [
            PermissionRule("read", "*", PermissionEffect.ALLOW),
            PermissionRule("list", "*", PermissionEffect.ALLOW),
            PermissionRule("search", "*", PermissionEffect.ALLOW),
        ],
        default_effect=PermissionEffect.DENY,
    )


def _reviewer_registry(context_engine: ContextEngine) -> ToolRegistry:
    registry = ToolRegistry()
    register_workspace_read_tools(registry)
    register_repository_tools(registry, engine=context_engine)
    return registry


class ReviewerRunner:
    """Runs one semantic review attempt on top of the native AgentSession."""

    def __init__(
        self,
        backend: ModelBackend,
        *,
        context_engine: ContextEngine | None = None,
        limits: AgentLimits | None = None,
    ) -> None:
        self._backend = backend
        self._context_engine = context_engine or ContextEngine()
        self._limits = limits or _DEFAULT_REVIEWER_LIMITS

    def run(
        self,
        workspace,
        request: ReviewRequest,
        *,
        recorder: ReviewRecorder | None = None,
        review_id: str | None = None,
    ) -> ReviewReport:
        if not isinstance(request, ReviewRequest):
            raise ReviewInputError("ReviewerRunner.run requires a ReviewRequest.")
        recorder = recorder or NullReviewRecorder()
        review_id = validate_review_id(review_id) if review_id is not None else new_review_id()

        query = request.task[:MAX_QUERY_CHARS]
        context_pack = self._context_engine.build(workspace, query, _REVIEW_CONTEXT_BUDGET)

        rendered_input = render_initial_review_input(
            task=request.task,
            plan=request.plan,
            diff=request.diff,
            verification_report=request.verification_report,
            context_pack=context_pack,
        )

        registry = _reviewer_registry(self._context_engine)
        policy = _reviewer_policy()
        context = ToolExecutionContext(workspace)

        session = AgentSession(
            backend=self._backend,
            registry=registry,
            policy=policy,
            context=context,
            instructions=REVIEWER_SYSTEM_INSTRUCTIONS,
            limits=self._limits,
            event_sink=recorder,
            execution_id=f"review_exec_{review_id}",
        )

        try:
            outcome = session.start(rendered_input)
        except AgentRuntimeError as exc:
            raise ReviewExecutionError(f"Reviewer AgentSession failed: {exc}") from exc

        if isinstance(outcome, ApprovalPause):
            message = "Reviewer received an unexpected approval pause; this is a configuration failure."
            recorder.fail(review_id, "ReviewApprovalError", message)
            raise ReviewExecutionError(message)

        try:
            decision = parse_review_decision(outcome.final_text)
        except ReviewProtocolError as exc:
            recorder.fail(review_id, type(exc).__name__, str(exc))
            raise

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
        recorder.complete(report)
        return report
