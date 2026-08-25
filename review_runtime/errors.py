"""Typed failures for the native provider-independent semantic Reviewer."""


class ReviewRuntimeError(Exception):
    """Base class for expected review_runtime failures."""


class ReviewInputError(ReviewRuntimeError):
    """The caller supplied an invalid ReviewRequest or domain value."""


class ReviewProtocolError(ReviewRuntimeError):
    """The model's final output did not satisfy the strict JSON review contract."""


class ReviewExecutionError(ReviewRuntimeError):
    """The Reviewer's AgentSession or backend failed unexpectedly."""


class ReviewRecordingError(ReviewRuntimeError):
    """A required canonical review lifecycle event could not be recorded."""
