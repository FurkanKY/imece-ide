"""Typed failures for the native provider-independent agent harness."""

from agent_runtime.cancellation import OperationCancelledError


class AgentRuntimeError(Exception):
    """Base class for expected native agent-runtime failures."""


class AgentInputError(AgentRuntimeError):
    """The caller supplied an invalid session input."""


class AgentLifecycleError(AgentRuntimeError):
    """An operation is invalid for the session's current lifecycle state."""


class AgentProtocolError(AgentRuntimeError):
    """The backend returned a structurally or semantically invalid turn."""


class AgentBackendError(AgentRuntimeError):
    """The model backend or model session failed unexpectedly."""


class AgentIncompleteError(AgentRuntimeError):
    """The model stopped because its output length was exhausted."""


class AgentRefusalError(AgentRuntimeError):
    """The model refused to complete the requested task."""


class AgentToolRuntimeError(AgentRuntimeError):
    """An internal Dispatcher/provenance invariant failed."""


class AgentApprovalError(AgentRuntimeError):
    """An approval decision is stale or does not match the pending call."""


class AgentLimitError(AgentRuntimeError):
    """A session-local model/tool/error limit was exceeded."""


class AgentRecordingError(AgentRuntimeError):
    """A required canonical lifecycle event could not be recorded."""


class AgentCancelledError(AgentRuntimeError, OperationCancelledError):
    """A CancellationToken was observed as cancelled before a model turn or
    a tool execution began. Cancellation is only ever honored at those
    cooperative check points -- an in-flight model HTTP call or a running
    tool cannot be interrupted mid-call."""
