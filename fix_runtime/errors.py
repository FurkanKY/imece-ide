"""Typed failures for the bounded native Fix Loop."""


class FixLoopRuntimeError(Exception):
    """Base class for expected fix_runtime failures."""


class FixLoopInputError(FixLoopRuntimeError):
    """The caller supplied an invalid FixTrigger/FixLoopRequest value."""


class FixLoopExecutionError(FixLoopRuntimeError):
    """A Worker/Verification/Reviewer/ChangeProvider port failed unexpectedly,
    or returned evidence that violates the loop's provenance contract."""


class FixLoopRecordingError(FixLoopRuntimeError):
    """A required canonical fix-loop lifecycle event could not be recorded."""
