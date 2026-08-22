"""Transient, provider-independent lifecycle events emitted by AgentSession."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from agent_runtime.models import ModelToolCall, ModelUsage
from tool_runtime.models import PermissionRequest


@dataclass(frozen=True, slots=True)
class AgentEvent:
    execution_id: str


@dataclass(frozen=True, slots=True)
class ExecutionStarted(AgentEvent):
    task: str


@dataclass(frozen=True, slots=True)
class TurnStarted(AgentEvent):
    turn_index: int
    turn_id: str


@dataclass(frozen=True, slots=True)
class ModelStarted(AgentEvent):
    turn_index: int
    turn_id: str
    item_id: str


@dataclass(frozen=True, slots=True)
class ModelCompleted(AgentEvent):
    turn_index: int
    turn_id: str
    item_id: str
    text: str
    tool_calls: tuple[ModelToolCall, ...]
    stop_reason: str
    usage: ModelUsage


@dataclass(frozen=True, slots=True)
class ModelFailed(AgentEvent):
    turn_index: int
    turn_id: str
    item_id: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class ToolRequested(AgentEvent):
    turn_index: int
    turn_id: str
    item_id: str
    call_id: str
    tool_name: str
    arguments: dict


@dataclass(frozen=True, slots=True)
class ToolStarted(ToolRequested):
    pass


@dataclass(frozen=True, slots=True)
class ToolCompleted(AgentEvent):
    turn_index: int
    turn_id: str
    item_id: str
    call_id: str
    tool_name: str
    content: str
    metadata: dict


@dataclass(frozen=True, slots=True)
class ToolFailed(AgentEvent):
    turn_index: int
    turn_id: str
    item_id: str
    call_id: str
    tool_name: str
    stage: str
    recoverable: bool
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class ApprovalRequested(AgentEvent):
    turn_index: int
    turn_id: str
    item_id: str
    call_id: str
    tool_name: str
    fingerprint: str
    permission_requests: tuple[PermissionRequest, ...]


@dataclass(frozen=True, slots=True)
class ApprovalResolved(AgentEvent):
    turn_index: int
    turn_id: str
    item_id: str
    call_id: str
    tool_name: str
    fingerprint: str
    approved: bool


@dataclass(frozen=True, slots=True)
class TurnCompleted(AgentEvent):
    turn_index: int
    turn_id: str


@dataclass(frozen=True, slots=True)
class ExecutionCompleted(AgentEvent):
    final_text: str
    model_turns: int
    tool_calls: int
    tool_errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


@dataclass(frozen=True, slots=True)
class ExecutionFailed(AgentEvent):
    error_type: str
    message: str


AgentLifecycleEvent: TypeAlias = (
    ExecutionStarted | TurnStarted | ModelStarted | ModelCompleted | ModelFailed
    | ToolRequested | ToolStarted | ToolCompleted | ToolFailed | ApprovalRequested
    | ApprovalResolved | TurnCompleted | ExecutionCompleted | ExecutionFailed
)


class AgentEventSink(Protocol):
    def emit(self, event: AgentLifecycleEvent) -> None:
        """Synchronously record one transient lifecycle event."""


class NullAgentEventSink:
    def emit(self, event: AgentLifecycleEvent) -> None:
        return None
