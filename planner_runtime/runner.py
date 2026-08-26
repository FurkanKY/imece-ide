"""Provider-neutral PlannerRunner — reuses AgentSession, never a second harness.

3I stops at PlanReport: PlannerRunner never orchestrates a Worker,
Verification, Reviewer, or FixLoop, and never selects a provider/model/
executor. See docs/superpowers/specs/2026-08-26-native-planner-design.md.
"""

from __future__ import annotations

import hashlib

from agent_runtime.backend import ModelBackend
from agent_runtime.errors import AgentRuntimeError
from agent_runtime.models import AgentLimits, ApprovalPause
from agent_runtime.session import AgentSession
from context_runtime import ContextEngine
from context_runtime.models import ContextBudget
from context_runtime.ranking import MAX_QUERY_CHARS
from tool_runtime.models import PermissionEffect, ToolExecutionContext
from tool_runtime.policy import PermissionRule, PolicyEvaluator
from tool_runtime.registry import ToolRegistry
from tool_runtime.tools.repository import register_repository_tools
from tool_runtime.tools.workspace_files import register_workspace_read_tools

from planner_runtime.errors import PlannerExecutionError, PlannerInputError, PlannerProtocolError
from planner_runtime.models import MAX_TASK_CHARS, PlanReport, new_plan_id, validate_plan_id
from planner_runtime.parser import parse_plan_decision
from planner_runtime.prompt import PLANNER_SYSTEM_INSTRUCTIONS, render_initial_planner_input
from planner_runtime.recording import NullPlanRecorder, PlanRecorder

_PLANNER_CONTEXT_BUDGET = ContextBudget(total_chars=24_000, map_chars=6_000, max_segment_chars=6_000)

_DEFAULT_PLANNER_LIMITS = AgentLimits(
    max_model_turns=8,
    max_tool_calls=20,
    max_consecutive_tool_errors=4,
)


def _planner_policy() -> PolicyEvaluator:
    return PolicyEvaluator(
        [
            PermissionRule("read", "*", PermissionEffect.ALLOW),
            PermissionRule("list", "*", PermissionEffect.ALLOW),
            PermissionRule("search", "*", PermissionEffect.ALLOW),
        ],
        default_effect=PermissionEffect.DENY,
    )


def _planner_registry(context_engine: ContextEngine) -> ToolRegistry:
    registry = ToolRegistry()
    register_workspace_read_tools(registry)
    register_repository_tools(registry, engine=context_engine)
    return registry


def _validate_task(task: object) -> str:
    if not isinstance(task, str):
        raise PlannerInputError("PlannerRunner.run requires task to be a string.")
    if "\x00" in task:
        raise PlannerInputError("task must not contain NUL characters.")
    if not task.strip():
        raise PlannerInputError("task must be non-empty.")
    if len(task) > MAX_TASK_CHARS:
        raise PlannerInputError(f"task exceeds the maximum of {MAX_TASK_CHARS} characters.")
    return task


class PlannerRunner:
    """Runs one semantic planning attempt on top of the native AgentSession."""

    def __init__(
        self,
        backend: ModelBackend,
        *,
        context_engine: ContextEngine | None = None,
        limits: AgentLimits | None = None,
    ) -> None:
        self._backend = backend
        self._context_engine = context_engine or ContextEngine()
        self._limits = limits or _DEFAULT_PLANNER_LIMITS

    def run(
        self,
        workspace,
        task: str,
        *,
        recorder: PlanRecorder | None = None,
        plan_id: str | None = None,
    ) -> PlanReport:
        # A. Validate task before any Agent side effect.
        task = _validate_task(task)
        # B. Generate/validate plan_id.
        recorder = recorder or NullPlanRecorder()
        plan_id = validate_plan_id(plan_id) if plan_id is not None else new_plan_id()
        task_sha256 = hashlib.sha256(task.encode("utf-8")).hexdigest()

        # C. Build ContextPack.
        query = task[:MAX_QUERY_CHARS]
        context_pack = self._context_engine.build(workspace, query, _PLANNER_CONTEXT_BUDGET)

        # D. Render bounded planner input.
        rendered_input = render_initial_planner_input(task=task, context_pack=context_pack)

        # E. Build read-only registry/policy/context.
        registry = _planner_registry(self._context_engine)
        policy = _planner_policy()
        context = ToolExecutionContext(workspace)

        # F. Create ONE fresh AgentSession (transient execution_id is internal
        # to the harness — never confused with a canonical execution.*).
        session = AgentSession(
            backend=self._backend,
            registry=registry,
            policy=policy,
            context=context,
            instructions=PLANNER_SYSTEM_INSTRUCTIONS,
            limits=self._limits,
            event_sink=recorder,
            execution_id=f"planner_exec_{plan_id}",
        )

        # G. Start AgentSession with rendered planner input.
        try:
            outcome = session.start(rendered_input)
        except AgentRuntimeError as exc:
            raise PlannerExecutionError(f"Planner AgentSession failed: {exc}") from exc

        # H. ApprovalPause is a configuration failure: the intended read-only
        # ALLOW policy should never need ASK.
        if isinstance(outcome, ApprovalPause):
            message = "Planner received an unexpected approval pause; this is a configuration failure."
            recorder.fail(plan_id, "PlannerApprovalError", message)
            raise PlannerExecutionError(message)

        # I/J. Strictly parse the final answer. AgentSession.ExecutionCompleted
        # != plan.completed: only a successfully-parsed decision may become one.
        try:
            decision = parse_plan_decision(outcome.final_text)
        except PlannerProtocolError as exc:
            recorder.fail(plan_id, type(exc).__name__, str(exc))
            raise

        # K/L. Compute runtime-owned provenance and construct the PlanReport.
        report = PlanReport(
            plan_id=plan_id,
            summary=decision.summary,
            steps=decision.steps,
            acceptance_criteria=decision.acceptance_criteria,
            risks=decision.risks,
            task_profile=decision.task_profile,
            repository_fingerprint=context_pack.repository_fingerprint,
            task_sha256=task_sha256,
        )
        # M. Record the terminal outcome.
        recorder.complete(report)
        # N. Return the PlanReport.
        return report
