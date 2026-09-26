"""Typed failures for deterministic verification."""


class VerificationRuntimeError(Exception):
    """Base class for verification runtime failures."""


class VerificationInputError(VerificationRuntimeError):
    """The caller supplied an invalid verification contract."""


class VerificationRecordingError(VerificationRuntimeError):
    """A required verification lifecycle event could not be recorded."""


class VerificationExecutionError(VerificationRuntimeError):
    """An unexpected verification runtime failure occurred."""


from agent_runtime.cancellation import OperationCancelledError


class VerificationCancelledError(VerificationExecutionError, OperationCancelledError):
    """A CancellationToken was observed as cancelled mid-verification; the
    in-flight check's process tree has already been terminated."""
