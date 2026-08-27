"""Typed failures for the native production attempt adapters."""


class ExecutorAdapterError(Exception):
    """Base class for expected executor_runtime failures."""


class ExecutorAdapterInputError(ExecutorAdapterError):
    """The caller supplied an invalid request/workspace/id, or the adapter's
    bound Run is not in a state that can accept a new canonical attempt."""


class ExecutorAdapterExecutionError(ExecutorAdapterError):
    """The underlying Agent/Verification/Reviewer infrastructure failed, or
    returned evidence that violates the attempt port's provenance contract."""
