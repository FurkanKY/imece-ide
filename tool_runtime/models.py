"""Provider-independent models for tool registration, policy, and execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol

from tool_runtime.errors import ToolInputValidationError, ToolObservationError
from tool_runtime.schema import canonical_object, validate_observation_metadata
from workspace.base import Workspace


class PermissionEffect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ToolAnnotations:
    read_only: bool = False
    destructive: bool = False
    idempotent: bool | None = None
    open_world: bool = False

    def __post_init__(self) -> None:
        for name in ("read_only", "destructive", "open_world"):
            if type(getattr(self, name)) is not bool:
                raise ToolInputValidationError(f"ToolAnnotations.{name} boolean olmalı.")
        if self.idempotent is not None and type(self.idempotent) is not bool:
            raise ToolInputValidationError("ToolAnnotations.idempotent boolean veya None olmalı.")


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    action: str
    resource: str

    def __post_init__(self) -> None:
        if not isinstance(self.action, str) or not self.action.strip():
            raise ToolInputValidationError("PermissionRequest.action boş olmayan bir string olmalı.")
        if not isinstance(self.resource, str) or not self.resource.strip():
            raise ToolInputValidationError("PermissionRequest.resource boş olmayan bir string olmalı.")


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    workspace: Workspace
    run_id: str | None = None
    execution_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ToolInputValidationError("ToolExecutionContext.workspace Workspace olmalı.")
        for name in ("run_id", "execution_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ToolInputValidationError(
                    f"ToolExecutionContext.{name} None veya boş olmayan bir string olmalı."
                )


@dataclass(frozen=True, slots=True)
class ToolObservation:
    content: str
    metadata: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ToolObservationError("ToolObservation.content string olmalı.")
        object.__setattr__(self, "metadata", validate_observation_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id.strip():
            raise ToolInputValidationError("ToolCall.call_id boş olmayan bir string olmalı.")
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ToolInputValidationError("ToolCall.tool_name boş olmayan bir string olmalı.")
        _, copied = canonical_object(self.arguments)
        object.__setattr__(self, "arguments", copied)


class ToolExecutor(Protocol):
    def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolObservation:
        """Execute an already-authorized call."""
