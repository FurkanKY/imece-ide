"""Factory from a resolved provider/model choice to an agent_runtime
AnthropicMessagesBackend.

This module intentionally lives outside `agent_runtime`, mirroring the seam
established by `chat_completions_catalog.py`: the new agent engine
(agent_runtime/*) stays provider-neutral and must not import legacy root
modules, but something has to know how to turn "the user picked Anthropic" —
plus an API key and an optional model override — into a concrete backend.

Unlike `chat_completions_catalog.py`, there is no legacy `providers.py` catalog
entry to bridge from: the legacy catalog only lists OpenAI-compatible ("kind":
"openai") endpoints and CLI agents, never a native Anthropic Messages API
entry. So this factory resolves credentials/model directly instead of
delegating to `providers.get(...)`.

Not wired into any call path yet — same status as chat_completions_catalog.py.
"""

from __future__ import annotations

import os

from agent_runtime.providers.anthropic_messages import AnthropicMessagesBackend

# Matches the claude-api skill's documented default: "The factory default
# must be claude-opus-5; model must be overridable."
DEFAULT_MODEL = "claude-opus-5"

_API_KEY_ENV = "ANTHROPIC_API_KEY"


class AnthropicKeyMissingError(Exception):
    """Raised when no explicit api_key was given and none was found in the environment."""


def anthropic_backend_from_config(
    *,
    api_key: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
) -> AnthropicMessagesBackend:
    """Build an AnthropicMessagesBackend from explicit config and/or the environment.

    Resolution order:
      - api_key: explicit `api_key` arg -> `ANTHROPIC_API_KEY` env var -> error
        (the Anthropic API always requires a key; there is no keyless mode).
      - model: explicit `model` arg -> DEFAULT_MODEL ("claude-opus-5").
    """
    resolved_key = api_key
    if resolved_key is None:
        env_value = os.getenv(_API_KEY_ENV, "").strip()
        resolved_key = env_value or None
    if resolved_key is None:
        raise AnthropicKeyMissingError(
            f"Anthropic backend requires an API key (env {_API_KEY_ENV})"
        )

    resolved_model = model or DEFAULT_MODEL
    return AnthropicMessagesBackend(
        resolved_model,
        api_key=resolved_key,
        max_tokens=max_tokens,
    )
