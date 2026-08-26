"""Typed failures for the workspace change-capture runtime."""


class ChangeRuntimeError(Exception):
    """Base class for expected change_runtime failures."""


class ChangeInputError(ChangeRuntimeError):
    """The caller supplied an invalid value or an unsupported workspace type."""


class ChangeCaptureError(ChangeRuntimeError):
    """Capturing the workspace change set failed unexpectedly."""
