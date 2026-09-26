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


from agent_runtime.cancellation import OperationCancelledError


class FixLoopCancelledError(FixLoopExecutionError, OperationCancelledError):
    """The fix loop was cancelled via a CancellationToken mid-attempt or
    between attempts; the active attempt (if any) has already been recorded
    as fix_attempt.interrupted and the loop as fix_loop.interrupted."""
