"""Typed errors for provider-independent repository context selection."""


class ContextRuntimeError(Exception):
    """Base error for context runtime failures."""


class ContextValidationError(ContextRuntimeError):
    """A public context contract was invalid."""


class ContextScanError(ContextRuntimeError):
    """Repository scanning could not establish a usable index."""
