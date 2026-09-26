"""NativePlanAttemptRunner — thin adapter binding pipeline_runtime.ports.
PlanAttemptRunner to the existing native PlannerRunner + canonical
CanonicalPlannerEventSink, mirroring executor_runtime's Worker/Verification/
Reviewer adapters (see tests/test_native_attempt_adapters_integration.py).
"""

from __future__ import annotations

from agent_runtime.backend import ModelBackend
from agent_runtime.models import AgentLimits
from context_runtime import ContextEngine
from planner_runtime.models import PlanReport
from planner_runtime.runner import PlannerRunner
from run_runtime.planner import CanonicalPlannerEventSink
from run_runtime.service import RunRuntime

from pipeline_runtime.errors import PipelineExecutionError, PipelineInputError


class NativePlanAttemptRunner:
    """Runs exactly one fresh Planner AgentSession attempt.

    Concrete production implementation of pipeline_runtime.ports.PlanAttemptRunner.
    """

    def __init__(
        self,
        runtime: RunRuntime,
        run_id: str,
        backend: ModelBackend,
        *,
        context_engine: ContextEngine | None = None,
        limits: AgentLimits | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise PipelineInputError("NativePlanAttemptRunner.run_id must be a non-empty string.")
        self._runtime = runtime
        self._run_id = run_id
        self._planner = PlannerRunner(backend, context_engine=context_engine, limits=limits)

    @property
    def run_id(self) -> str:
        return self._run_id

    def run(self, workspace, task: str, *, plan_id: str) -> PlanReport:
        try:
            sink = CanonicalPlannerEventSink(self._runtime, self._run_id, plan_id=plan_id)
        except ValueError as exc:
            raise PipelineInputError(f"Cannot construct canonical Planner sink: {exc}") from exc

        try:
            report = self._planner.run(workspace, task, recorder=sink, plan_id=plan_id)
        except Exception as exc:
            raise PipelineExecutionError(f"Planner port failed: {exc}") from exc

        if not isinstance(report, PlanReport) or report.plan_id != plan_id:
            raise PipelineExecutionError("PlannerRunner did not return the requested plan_id.")
        return report
