"""Typed errors exposed by the provider-independent tool runtime."""


class ToolRuntimeError(Exception):
    """Base class for all expected tool-runtime failures."""


class ToolRegistrationError(ToolRuntimeError):
    """A tool specification or executor cannot be registered."""


class ToolNotFoundError(ToolRuntimeError):
    """The registry has no tool with the requested name."""


class ToolInputValidationError(ToolRuntimeError):
    """A tool call or its arguments do not satisfy the input contract."""


class ToolPolicyError(ToolRuntimeError):
    """A permission request could not be resolved or evaluated."""


class ToolDeniedError(ToolPolicyError):
    """Policy explicitly denies a tool call."""


class ToolApprovalRequiredError(ToolPolicyError):
    """Policy requires an exact approval grant before execution."""


class ToolApprovalMismatchError(ToolPolicyError):
    """An approval grant does not match the prepared call."""


class ToolExecutionError(ToolRuntimeError):
    """Execution failed or returned an invalid observation."""


class ToolPreparedCallError(ToolRuntimeError):
    """A prepared call is foreign, forged, or bound to another context."""


class ToolCallConsumedError(ToolExecutionError):
    """A prepared tool call has already been attempted."""


class ToolObservationError(ToolRuntimeError):
    """An observation contains data outside the strict JSON contract."""
