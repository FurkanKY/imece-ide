"""acp_runtime — provider-neutral, asynchronous ACP Client Core.

Launches one local stdio ACP agent subprocess (via the official
agent-client-protocol SDK), runs exactly one prompt through it, observes
bounded streaming updates, always denies permission requests, and
deterministically tears the process tree down. Does not connect to
fix_runtime/executor_runtime/run_runtime — see
docs/superpowers/specs/2026-08-29-acp-client-core-design.md.
"""

from acp_runtime.errors import (
    AcpAuthenticationRequiredError,
    AcpCleanupError,
    AcpEventSinkError,
    AcpInputError,
    AcpLimitError,
    AcpProtocolError,
    AcpRuntimeError,
    AcpSpawnError,
    AcpTimeoutError,
)
from acp_runtime.events import (
    AcpEventSink,
    AcpPermissionRequested,
    AcpPermissionResolved,
    AcpRuntimeEvent,
    AcpSessionUpdateObserved,
    NullAcpEventSink,
)
from acp_runtime.models import AcpClientLimits, AcpLaunchSpec, AcpPromptRequest, AcpRunResult
from acp_runtime.client import AcpClientRuntime

__all__ = [
    "AcpRuntimeError",
    "AcpInputError",
    "AcpSpawnError",
    "AcpProtocolError",
    "AcpAuthenticationRequiredError",
    "AcpLimitError",
    "AcpTimeoutError",
    "AcpEventSinkError",
    "AcpCleanupError",
    "AcpLaunchSpec",
    "AcpPromptRequest",
    "AcpClientLimits",
    "AcpRunResult",
    "AcpRuntimeEvent",
    "AcpSessionUpdateObserved",
    "AcpPermissionRequested",
    "AcpPermissionResolved",
    "AcpEventSink",
    "NullAcpEventSink",
    "AcpClientRuntime",
]
