"""AcpPlanAttemptRunner -- drives a read-only account-authenticated ACP agent
(Claude Code / Codex over the Agent Client Protocol) through exactly one
fresh Planner attempt, implementing pipeline_runtime.ports.PlanAttemptRunner.

This is the ACP counterpart of pipeline_runtime.native_planner.
NativePlanAttemptRunner: SAME prompt construction (planner_runtime's
render_initial_planner_input + PLANNER_SYSTEM_INSTRUCTIONS), SAME strict
parser (planner_runtime.parser.parse_plan_decision), SAME ContextEngine
budget, and SAME canonical recorder (run_runtime.planner.
CanonicalPlannerEventSink) -- only the transport differs (ACP subprocess
instead of a ModelBackend-driven AgentSession).

ACP has no separate system-prompt slot, so the stable system instructions
are prepended to the single rendered user prompt (see
executor_runtime.acp_semantic.wrap_system_instructions_for_acp_prompt).

Canonical recording: CanonicalPlannerEventSink only understands
agent_runtime.events.AgentLifecycleEvent values. This runner constructs the
two synthetic lifecycle transitions the sink actually requires --
ExecutionStarted (to persist plan.started) and ExecutionCompleted (to arm
sink.complete()) -- from the ACP session's outcome. Every OTHER ACP session
update (thought chunks, tool_call updates, plan updates, permission
request/resolved) is deliberately dropped rather than mapped into a
synthetic ModelCompleted/ToolStarted/etc.: see
executor_runtime.acp_semantic._AgentMessageTextSink's docstring for why.
Planner activity here, exactly as in the native path, NEVER produces
execution.* events and NEVER sets RunEvent.execution_id -- only plan.*.

Unlike NativePlanAttemptRunner (which lets an ordinary AgentRuntimeError
propagate WITHOUT failing the sink, since PlannerRunner.run only calls
recorder.fail() for the two model-output failure modes it explicitly
recognizes), this runner fails the sink on every post-plan.started
exception, mirroring executor_runtime.acp_worker.AcpWorkerAttemptAdapter's
convention for the ACP transport: an ACP subprocess/protocol failure is
infrastructure, not a distinguishable model-output failure mode, and
leaving plan.started dangling with no terminal event for it would be worse
than an extra plan.failed.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence

from agent_runtime.cancellation import CancellationToken, OperationCancelledError
from agent_runtime.events import ExecutionCompleted, ExecutionStarted
from context_runtime import ContextEngine, load_project_rules
from context_runtime.ranking import MAX_QUERY_CHARS
from planner_runtime.errors import PlannerProtocolError
from planner_runtime.models import PlanReport, validate_plan_id
from planner_runtime.parser import parse_plan_decision
from planner_runtime.prompt import PLANNER_SYSTEM_INSTRUCTIONS, render_initial_planner_input
from planner_runtime.runner import _PLANNER_CONTEXT_BUDGET, _validate_task
from run_runtime.agent_activity import record_agent_activity
from run_runtime.planner import CanonicalPlannerEventSink
from run_runtime.service import RunRuntime

from acp_runtime.models import AcpClientLimits
from executor_runtime.acp_semantic import run_acp_semantic_prompt, wrap_system_instructions_for_acp_prompt
from executor_runtime.acp_worker import AcpWorkerLaunchProfile, resolve_acp_worker_launch

from pipeline_runtime.errors import PipelineCancelledError, PipelineExecutionError, PipelineInputError


class AcpPlanAttemptRunner:
    """Runs exactly one fresh Planner attempt via a local ACP agent process.

    Concrete production implementation of pipeline_runtime.ports.PlanAttemptRunner.
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
            raise PipelineInputError("AcpPlanAttemptRunner.run_id must be a non-empty string.")
        if not isinstance(launch_profile, AcpWorkerLaunchProfile):
            raise PipelineInputError("AcpPlanAttemptRunner.launch_profile must be an AcpWorkerLaunchProfile.")
        if not callable(getattr(acp_client, "run", None)):
            raise PipelineInputError("AcpPlanAttemptRunner.acp_client must expose a callable run().")
        if limits is not None and not isinstance(limits, AcpClientLimits):
            raise PipelineInputError("AcpPlanAttemptRunner.limits must be an AcpClientLimits.")
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
        self, workspace, task: str, *, plan_id: str, cancel_token: CancellationToken | None = None,
        pinned_paths: Sequence[str] = (),
    ) -> PlanReport:
        task = _validate_task(task)
        plan_id = validate_plan_id(plan_id)

        rules = load_project_rules(workspace.root)
        try:
            sink = CanonicalPlannerEventSink(
                self._runtime, self._run_id, plan_id=plan_id,
                rules_sha256=rules.sha256 if rules is not None else None,
                pinned_paths=pinned_paths,
            )
        except ValueError as exc:
            raise PipelineInputError(f"Cannot construct canonical Planner sink: {exc}") from exc

        task_sha256 = hashlib.sha256(task.encode("utf-8")).hexdigest()

        query = task[:MAX_QUERY_CHARS]
        context_pack = self._context_engine.build(workspace, query, _PLANNER_CONTEXT_BUDGET, pinned_paths=pinned_paths)
        rendered_task_input = render_initial_planner_input(task=task, context_pack=context_pack, rules=rules)
        acp_prompt = wrap_system_instructions_for_acp_prompt(PLANNER_SYSTEM_INSTRUCTIONS, rendered_task_input)

        launch_spec = resolve_acp_worker_launch(self._launch_profile)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise PipelineExecutionError("AcpPlanAttemptRunner.run cannot execute inside a running event loop.")

        transient_execution_id = f"acp_planner_exec_{plan_id}"
        sink.emit(ExecutionStarted(execution_id=transient_execution_id, task=task))

        def _activity(**kwargs) -> None:
            # F1 (live agent activity): best-effort, advisory only -- never
            # allowed to fail the Planner attempt itself (see
            # run_runtime.agent_activity's module docstring).
            try:
                event = record_agent_activity(
                    self._runtime, self._run_id, execution_id=transient_execution_id, **kwargs,
                )
                # This append happens BETWEEN sink.emit(ExecutionStarted)
                # (already committed) and sink.complete()/sink.fail() (not
                # yet committed) -- sink's own optimistic cursor must be
                # told about it, or its next commit uses a stale
                # expected_last_event_seq and raises EventSequenceError
                # even though nothing is actually wrong (see
                # CanonicalPlannerEventSink.note_external_append).
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
                role="planner",
                activity_recorder=_activity,
            )
        except OperationCancelledError as cancellation:
            self._fail(sink, plan_id, cancellation)
            raise PipelineCancelledError("Planner ACP session cancelled.") from cancellation
        except Exception as original_failure:
            self._fail(sink, plan_id, original_failure)
            raise PipelineExecutionError("Planner ACP session failed.") from original_failure

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
            decision = parse_plan_decision(final_text)
        except PlannerProtocolError as exc:
            self._fail(sink, plan_id, exc)
            raise PipelineExecutionError(f"Planner ACP output failed protocol parsing: {exc}") from exc

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
        sink.complete(report)
        return report

    @staticmethod
    def _fail(sink: CanonicalPlannerEventSink, plan_id: str, error: Exception) -> None:
        try:
            sink.fail(plan_id, type(error).__name__, str(error))
        except Exception as terminal_failure:
            raise PipelineExecutionError(
                "Planner ACP execution and terminal failure recording both failed."
            ) from terminal_failure
