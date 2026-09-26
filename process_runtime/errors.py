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
    """A CancellationToken was observed as cancelled while waiting on the
    process; the process tree has already been terminated by the time this
    is raised."""
