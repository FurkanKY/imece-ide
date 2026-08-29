"""Transient ACP runtime observations.

These are NOT run_runtime canonical events: no RunEvent, no execution_id, no
persistence. They exist only for the duration of one AcpClientRuntime.run()
call and are handed to an AcpEventSink as they occur.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence


@dataclass(frozen=True, slots=True)
class AcpSessionUpdateObserved:
    session_id: str
    update: Any
    serialized_chars: int


@dataclass(frozen=True, slots=True)
class AcpPermissionRequested:
    session_id: str
    tool_call_id: str
    title: str
    option_ids: Sequence[str]


@dataclass(frozen=True, slots=True)
class AcpPermissionResolved:
    session_id: str
    tool_call_id: str
    outcome: str


AcpRuntimeEvent = AcpSessionUpdateObserved | AcpPermissionRequested | AcpPermissionResolved


class AcpEventSink(Protocol):
    def emit(self, event: AcpRuntimeEvent) -> None: ...


class NullAcpEventSink:
    """Default no-op sink."""

    def emit(self, event: AcpRuntimeEvent) -> None:
        return None
