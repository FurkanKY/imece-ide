"""Typed failures for the provider-neutral ACP Client Core."""


class AcpRuntimeError(Exception):
    """Base class for acp_runtime failures."""


class AcpInputError(AcpRuntimeError):
    """A launch spec/prompt request/limits/event sink violates the contract,
    or the target cwd does not exist as a directory. Raised before any
    subprocess is spawned."""


class AcpSpawnError(AcpRuntimeError):
    """The ACP agent subprocess could not be launched."""


class AcpProtocolError(AcpRuntimeError):
    """A non-auth ACP request/schema/connection failure, or an update
    arrived for a session_id other than the active one."""


class AcpAuthenticationRequiredError(AcpRuntimeError):
    """The agent reported auth_required (RequestError code -32000)."""


class AcpLimitError(AcpRuntimeError):
    """A bounded update budget (per-update size, count, or total size) was
    exceeded."""


class AcpTimeoutError(AcpRuntimeError):
    """The prompt did not complete within prompt_timeout_ms."""


class AcpEventSinkError(AcpRuntimeError):
    """The caller-supplied AcpEventSink.emit() raised."""


class AcpCleanupError(AcpRuntimeError):
    """The ACP subprocess tree could not be fully terminated."""
