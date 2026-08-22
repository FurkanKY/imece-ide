"""Typed process-runtime failures."""


class ProcessRuntimeError(Exception):
    """Base class for process infrastructure failures."""


class ProcessInputError(ProcessRuntimeError):
    """A process request violates the provider-independent contract."""


class ProcessSpawnError(ProcessRuntimeError):
    """The requested executable could not be resolved or spawned."""


class ProcessCleanupError(ProcessRuntimeError):
    """A timed-out process tree could not be fully cleaned up."""
