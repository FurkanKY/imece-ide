"""Provider-neutral model backend/session protocols."""

from __future__ import annotations

from typing import Protocol

from agent_runtime.models import (
    ModelInputItem,
    ModelToolDefinition,
    ModelTurn,
)


class ModelSession(Protocol):
    def respond(self, input_items: tuple[ModelInputItem, ...]) -> ModelTurn:
        """Continue the provider-owned conversation with new input items."""


class ModelBackend(Protocol):
    def open_session(
        self,
        *,
        instructions: str,
        tools: tuple[ModelToolDefinition, ...],
        allow_parallel_tool_calls: bool,
    ) -> ModelSession:
        """Open provider conversation state without executing any tools."""
