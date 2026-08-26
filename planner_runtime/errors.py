"""Typed failures for the native provider-independent semantic Planner."""


class PlannerError(Exception):
    """Base class for expected planner_runtime failures."""


class PlannerInputError(PlannerError):
    """The caller supplied an invalid task/plan ID or domain value."""


class PlannerExecutionError(PlannerError):
    """The Planner's AgentSession or backend failed unexpectedly."""


class PlannerProtocolError(PlannerError):
    """The model's final output did not satisfy the strict JSON plan contract."""


class PlannerRecordingError(PlannerError):
    """A required canonical planner lifecycle event could not be recorded."""
