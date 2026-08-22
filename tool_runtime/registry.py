"""Tool specification and executor registry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable

from tool_runtime.errors import ToolNotFoundError, ToolRegistrationError
from tool_runtime.models import ToolAnnotations, ToolExecutionContext, ToolExecutor
from tool_runtime.schema import validate_input_schema


PermissionResolver = Callable[[Mapping[str, Any], ToolExecutionContext], Sequence[Any]]


def _freeze_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_schema(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_schema(child) for child in value)
    return value


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    annotations: ToolAnnotations
    permission_resolver: PermissionResolver

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ToolRegistrationError("ToolSpec.name boş olmayan bir string olmalı.")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ToolRegistrationError("ToolSpec.description boş olmayan bir string olmalı.")
        if not isinstance(self.annotations, ToolAnnotations):
            raise ToolRegistrationError("ToolSpec.annotations ToolAnnotations olmalı.")
        if not callable(self.permission_resolver):
            raise ToolRegistrationError("ToolSpec.permission_resolver çağrılabilir olmalı.")
        schema = validate_input_schema(self.input_schema)
        object.__setattr__(self, "input_schema", _freeze_schema(schema))


class ToolRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[ToolSpec, ToolExecutor]] = {}

    def register(self, spec: ToolSpec, executor: ToolExecutor) -> None:
        if not isinstance(spec, ToolSpec):
            raise ToolRegistrationError("Kayıt için ToolSpec gerekli.")
        if not callable(getattr(executor, "execute", None)):
            raise ToolRegistrationError("ToolExecutor.execute çağrılabilir olmalı.")
        if spec.name in self._entries:
            raise ToolRegistrationError(f"Tool zaten kayıtlı: {spec.name}")
        self._entries[spec.name] = (spec, executor)

    def get(self, name: str) -> ToolSpec:
        if name not in self._entries:
            raise ToolNotFoundError(f"Tool bulunamadı: {name}")
        return self._entries[name][0]

    def list_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(spec for spec, _ in self._entries.values())

    def _executor_for(self, name: str) -> ToolExecutor:
        if name not in self._entries:
            raise ToolNotFoundError(f"Tool bulunamadı: {name}")
        return self._entries[name][1]
