"""Bridge from the legacy provider catalog (providers.py) to the new agent_runtime
Chat Completions backend.

This module intentionally lives outside `agent_runtime`: the new agent engine
(agent_runtime/*) is provider-neutral and must not import legacy root modules
(providers.py, adapters.py, requests, ...). This thin module is the other side of
that seam — it knows about both the legacy catalog and agent_runtime.providers, so
neither side has to know about the other.

Not wired into any call path yet (webhost/, agents.py and friends still use the
legacy adapters.PROVIDERS path). When the new agent engine is adopted by the
webhost/CLI layer, this is the intended place to resolve "provider id chosen in
Settings" -> "a ModelBackend instance" for any OpenAI-compatible provider in the
catalog.
"""

from __future__ import annotations

import os

import providers as _catalog
from agent_runtime.providers.chat_completions import ChatCompletionsBackend


class UnknownProviderError(Exception):
    """Raised when `provider_id` is not present in the legacy catalog."""


class ProviderNotOpenAICompatibleError(Exception):
    """Raised when `provider_id` resolves to a non-"openai"-kind entry (e.g. a CLI)."""


class ProviderKeyMissingError(Exception):
    """Raised when the provider requires an API key and none was found."""


def chat_completions_backend_from_catalog(
    provider_id: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
) -> ChatCompletionsBackend:
    """Build a ChatCompletionsBackend for a catalog entry (built-in or custom).

    Resolution order:
      - api_key: explicit `api_key` arg -> `entry["key_env"]` env var -> None
        (None is only valid for keyless local endpoints such as Ollama).
      - model: explicit `model` arg -> providers.selected_model(entry), which
        itself prefers the user's saved choice, then `entry["model_env"]`, then
        `entry["default_model"]`.
    """
    entry = _catalog.get(provider_id)
    if entry is None:
        raise UnknownProviderError(f"Unknown provider: {provider_id}")
    if entry.get("kind") != "openai":
        raise ProviderNotOpenAICompatibleError(
            f"Provider {provider_id!r} is kind={entry.get('kind')!r}, not an OpenAI-compatible endpoint"
        )

    resolved_key = api_key
    if resolved_key is None and entry.get("key_env"):
        env_value = os.getenv(entry["key_env"], "").strip()
        resolved_key = env_value or None
    if resolved_key is None and entry.get("key_env"):
        raise ProviderKeyMissingError(
            f"Provider {provider_id!r} requires an API key (env {entry['key_env']})"
        )
    if resolved_key is None:
        # Keyless local endpoints (e.g. Ollama) still need a non-empty string:
        # the openai SDK's client constructor requires *some* api_key value even
        # when the server itself ignores auth entirely.
        resolved_key = "not-needed"

    resolved_model = model or _catalog.selected_model(entry)
    return ChatCompletionsBackend(
        resolved_model,
        api_key=resolved_key,
        base_url=entry["base_url"],
        max_tokens=max_tokens,
    )
