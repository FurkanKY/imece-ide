"""Recording port so planner_runtime does not depend directly on run_runtime."""

from __future__ import annotations

from typing import Protocol

from agent_runtime.events import AgentLifecycleEvent
from planner_runtime.models import PlanReport


class PlanRecorder(Protocol):
    def emit(self, event: AgentLifecycleEvent) -> None:
        """Record one transient AgentSession lifecycle event for this plan attempt."""

    def complete(self, report: PlanReport) -> None:
        """Record the terminal outcome of a successfully parsed plan."""

    def fail(self, plan_id: str, error_type: str, message: str) -> None:
        """Record a Planner infrastructure/protocol failure (not a semantic outcome)."""


class NullPlanRecorder:
    """A no-op PlanRecorder for standalone Planner use outside RunRuntime."""

    def emit(self, event: AgentLifecycleEvent) -> None:
        return None

    def complete(self, report: PlanReport) -> None:
        return None

    def fail(self, plan_id: str, error_type: str, message: str) -> None:
        return None
