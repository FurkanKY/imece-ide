"""Typed process-runtime failures."""


class ProcessRuntimeError(Exception):
    """Base class for process infrastructure failures."""


class ProcessInputError(ProcessRuntimeError):
    """A process request violates the provider-independent contract."""


class ProcessSpawnError(ProcessRuntimeError):
    """The requested executable could not be resolved or spawned."""


class ProcessCleanupError(ProcessRuntimeError):
    """A timed-out process tree could not be fully cleaned up."""


from agent_runtime.cancellation import OperationCancelledError


class ProcessCancelledError(ProcessRuntimeError, OperationCancelledError):
    """Cancellation after termination; quiescence is true only after receipt validation."""

    def __init__(self, message: str, *, producer_quiescent: bool = False) -> None:
        super().__init__(message)
        self.producer_quiescent = producer_quiescent
