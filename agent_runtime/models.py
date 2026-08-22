"""Immutable provider-neutral models for the native agent harness."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from tool_runtime.models import PermissionRequest
from tool_runtime.schema import canonical_json

from agent_runtime.errors import AgentInputError


def _strict_object(value: Any, *, field: str) -> dict[str, Any]:
    try:
        raw = canonical_json(value, error_type=AgentInputError)
    except AgentInputError as exc:
        raise AgentInputError(f"{field} strict JSON nesnesi olmalı: {exc}") from exc
    if not isinstance(value, Mapping):
        raise AgentInputError(f"{field} üst düzeyde JSON nesnesi olmalı.")
    import json

    decoded = json.loads(raw)
    if not isinstance(decoded, dict):  # pragma: no cover - canonical input check above
        raise AgentInputError(f"{field} üst düzeyde JSON nesnesi olmalı.")
    return decoded


def _require_nonempty(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AgentInputError(f"{field} boş olmayan bir string olmalı.")


class ModelStopReason(StrEnum):
    COMPLETED = "completed"
    TOOL_USE = "tool_use"
    LENGTH = "length"
    REFUSAL = "refusal"
    ERROR = "error"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ModelToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]

    def __post_init__(self) -> None:
        _require_nonempty(self.name, "ModelToolDefinition.name")
        _require_nonempty(self.description, "ModelToolDefinition.description")
        schema = _strict_object(self.input_schema, field="ModelToolDefinition.input_schema")
        object.__setattr__(self, "input_schema", schema)


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        _require_nonempty(self.call_id, "ModelToolCall.call_id")
        _require_nonempty(self.name, "ModelToolCall.name")
        object.__setattr__(self, "arguments", _strict_object(self.arguments, field="ModelToolCall.arguments"))


@dataclass(frozen=True, slots=True)
class ModelToolResult:
    call_id: str
    content: str
    is_error: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty(self.call_id, "ModelToolResult.call_id")
        if not isinstance(self.content, str):
            raise AgentInputError("ModelToolResult.content string olmalı.")
        if type(self.is_error) is not bool:
            raise AgentInputError("ModelToolResult.is_error boolean olmalı.")
        object.__setattr__(self, "metadata", _strict_object(self.metadata, field="ModelToolResult.metadata"))


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise AgentInputError(f"ModelUsage.{name} non-negative integer olmalı.")
        if self.cost_usd is not None:
            if isinstance(self.cost_usd, bool) or not isinstance(self.cost_usd, (int, float)):
                raise AgentInputError("ModelUsage.cost_usd sonlu sayı veya None olmalı.")
            if not math.isfinite(float(self.cost_usd)) or self.cost_usd < 0:
                raise AgentInputError("ModelUsage.cost_usd sonlu ve non-negative olmalı.")


@dataclass(frozen=True, slots=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ModelToolCall, ...]
    stop_reason: ModelStopReason
    usage: ModelUsage

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise AgentInputError("ModelTurn.text string olmalı.")
        if not isinstance(self.stop_reason, ModelStopReason):
            raise AgentInputError("ModelTurn.stop_reason ModelStopReason olmalı.")
        if not isinstance(self.usage, ModelUsage):
            raise AgentInputError("ModelTurn.usage ModelUsage olmalı.")
        calls = tuple(self.tool_calls)
        if any(not isinstance(call, ModelToolCall) for call in calls):
            raise AgentInputError("ModelTurn.tool_calls yalnızca ModelToolCall içermeli.")
        object.__setattr__(self, "tool_calls", calls)


@dataclass(frozen=True, slots=True)
class UserInput:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise AgentInputError("UserInput.text string olmalı.")


@dataclass(frozen=True, slots=True)
class ToolResultInput:
    result: ModelToolResult

    def __post_init__(self) -> None:
        if not isinstance(self.result, ModelToolResult):
            raise AgentInputError("ToolResultInput.result ModelToolResult olmalı.")


ModelInputItem = UserInput | ToolResultInput


@dataclass(frozen=True, slots=True)
class AgentLimits:
    max_model_turns: int = 20
    max_tool_calls: int = 50
    max_consecutive_tool_errors: int = 5

    def __post_init__(self) -> None:
        for name in ("max_model_turns", "max_tool_calls", "max_consecutive_tool_errors"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise AgentInputError(f"AgentLimits.{name} pozitif integer olmalı.")


@dataclass(frozen=True, slots=True)
class ApprovalPause:
    call_id: str
    tool_name: str
    approval_fingerprint: str
    permission_requests: tuple[PermissionRequest, ...]
    session_id: str

    def __post_init__(self) -> None:
        _require_nonempty(self.call_id, "ApprovalPause.call_id")
        _require_nonempty(self.tool_name, "ApprovalPause.tool_name")
        _require_nonempty(self.approval_fingerprint, "ApprovalPause.approval_fingerprint")
        _require_nonempty(self.session_id, "ApprovalPause.session_id")
        requests = tuple(self.permission_requests)
        if any(not isinstance(request, PermissionRequest) for request in requests):
            raise AgentInputError("ApprovalPause.permission_requests geçersiz.")
        object.__setattr__(self, "permission_requests", requests)


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    call_id: str
    fingerprint: str
    approved: bool
    session_id: str

    def __post_init__(self) -> None:
        _require_nonempty(self.call_id, "ApprovalDecision.call_id")
        _require_nonempty(self.fingerprint, "ApprovalDecision.fingerprint")
        _require_nonempty(self.session_id, "ApprovalDecision.session_id")
        if type(self.approved) is not bool:
            raise AgentInputError("ApprovalDecision.approved boolean olmalı.")


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    final_text: str
    model_turns: int
    tool_calls: int
    tool_errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None
