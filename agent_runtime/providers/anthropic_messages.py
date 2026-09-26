"""Native Anthropic Messages API adapter for the provider-neutral agent loop.

Like ``chat_completions.py``, the Messages API is stateless per request: this
session resends the full message history on every call. Unlike Chat
Completions, the assistant's full ``response.content`` (including any
``thinking``/``redacted_thinking`` blocks) is stored and replayed **unchanged**
on later turns — Anthropic's "preserved thinking" contract requires the
provider-returned blocks to come back byte-for-byte, so this adapter never
reconstructs them from scratch the way it reconstructs OpenAI-style
``tool_calls`` dicts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from agent_runtime.models import (
    ModelInputItem,
    ModelStopReason,
    ModelToolCall,
    ModelToolDefinition,
    ModelToolResult,
    ModelTurn,
    ModelUsage,
    ToolResultInput,
    UserInput,
)

# Keeps non-streaming requests comfortably under the SDK's default HTTP
# timeout even for long agentic responses; callers may override via the
# backend's `max_tokens` constructor argument.
_DEFAULT_MAX_TOKENS = 16000

# Server-side tool loops (code execution, web search, ...) can legitimately
# pause with stop_reason="pause_turn"; per the API docs this is resumed by
# resending the same messages (no new user turn) and the server continues
# automatically. This adapter declares no server-side tools today, so this
# path is defensive: it bounds the number of silent internal resends before
# giving up and surfacing the turn as ModelStopReason.OTHER.
_MAX_PAUSE_TURN_RESUMES = 5

# Adaptive thinking is not supported on the Haiku family (see the claude-api
# skill docs: "Haiku 4.5 does not support adaptive: omit thinking for
# claude-haiku-*"). Matched by prefix so future haiku point releases keep
# working without a code change.
_NO_ADAPTIVE_THINKING_PREFIX = "claude-haiku"


class AnthropicMessagesError(Exception):
    """Base error for the Anthropic Messages adapter."""


class AnthropicMessagesProtocolError(AnthropicMessagesError):
    """The provider returned an unsupported or malformed response."""


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _require_nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _strict_json(value: Any, label: str) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AnthropicMessagesProtocolError(f"{label} is not strict JSON: {exc}") from exc


def _copy_json(value: Any, label: str) -> Any:
    return json.loads(_strict_json(value, label))


def _anthropic_tools(tools: tuple[ModelToolDefinition, ...]) -> tuple[dict[str, Any], ...]:
    converted = []
    for tool in tools:
        converted.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": _copy_json(tool.input_schema, f"tool {tool.name} schema"),
            }
        )
    return tuple(converted)


def _nonneg_int(raw_usage: Any, provider_name: str) -> int:
    raw = _field(raw_usage, provider_name, 0)
    if raw is None:
        raw = 0
    if type(raw) is not int or raw < 0:
        raise AnthropicMessagesProtocolError(
            f"Response usage {provider_name} must be a non-negative integer"
        )
    return raw


def _usage(response: Any) -> ModelUsage:
    raw_usage = _field(response, "usage")
    if raw_usage is None:
        return ModelUsage()

    input_tokens = _nonneg_int(raw_usage, "input_tokens")
    output_tokens = _nonneg_int(raw_usage, "output_tokens")
    # Anthropic reports cache reads/writes as separate counters, not as a
    # subset of input_tokens (unlike OpenAI's prompt_tokens_details, see the
    # note in chat_completions.py). ModelUsage has no dedicated cache field,
    # so cache tokens are folded into input_tokens here: they were still
    # tokens the request paid to send, and dropping them would silently
    # undercount usage for cached multi-turn agent loops.
    input_tokens += _nonneg_int(raw_usage, "cache_read_input_tokens")
    input_tokens += _nonneg_int(raw_usage, "cache_creation_input_tokens")
    return ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens)


def _sum_usage(first: ModelUsage, second: ModelUsage) -> ModelUsage:
    return ModelUsage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
    )


class _ParsedResponse(NamedTuple):
    text: str
    tool_calls: tuple[ModelToolCall, ...]
    raw_stop_reason: Any
    usage: ModelUsage
    content: Any


def _parse_response(response: Any) -> _ParsedResponse:
    content = _field(response, "content")
    if content is None or isinstance(content, (str, bytes, bytearray)) or not isinstance(content, Sequence):
        raise AnthropicMessagesProtocolError("Messages API response.content must be a sequence")

    text_parts: list[str] = []
    calls: list[ModelToolCall] = []
    for block in content:
        block_type = _field(block, "type")
        if block_type == "text":
            text = _field(block, "text")
            if isinstance(text, str):
                text_parts.append(text)
        elif block_type == "tool_use":
            call_id = _field(block, "id")
            name = _field(block, "name")
            arguments = _field(block, "input")
            if not isinstance(arguments, dict):
                raise AnthropicMessagesProtocolError("tool_use.input must decode to an object")
            try:
                calls.append(ModelToolCall(call_id, name, arguments))
            except Exception as exc:
                raise AnthropicMessagesProtocolError(f"Invalid tool_use block: {exc}") from exc
        # "thinking", "redacted_thinking", "server_tool_use" and other
        # provider-internal blocks are intentionally not surfaced as visible
        # text — they are preserved verbatim in `content` for history replay
        # (see the session's respond()), just never read here.

    raw_stop_reason = _field(response, "stop_reason")
    return _ParsedResponse(
        "".join(text_parts),
        tuple(calls),
        raw_stop_reason,
        _usage(response),
        content,
    )


def _map_stop_reason(raw_stop_reason: Any, *, has_tool_calls: bool) -> ModelStopReason:
    if has_tool_calls or raw_stop_reason == "tool_use":
        return ModelStopReason.TOOL_USE
    if raw_stop_reason == "end_turn":
        return ModelStopReason.COMPLETED
    if raw_stop_reason == "max_tokens":
        return ModelStopReason.LENGTH
    if raw_stop_reason == "refusal":
        return ModelStopReason.REFUSAL
    if raw_stop_reason == "stop_sequence":
        # A configured stop sequence was hit; this adapter never sets
        # `stop_sequences`, but if a caller ever adds one, treat it the same
        # way the other two backends treat their "clean stop" signal.
        return ModelStopReason.COMPLETED
    # "pause_turn" only reaches here after _MAX_PAUSE_TURN_RESUMES internal
    # resumes were exhausted (see AnthropicMessagesSession.respond) — surface
    # it as OTHER rather than silently completing with partial text.
    return ModelStopReason.OTHER


class AnthropicMessagesBackend:
    """Synchronous Anthropic Messages backend with injectable SDK client."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        client: Any = None,
        max_tokens: int | None = None,
    ) -> None:
        self.model = _require_nonempty(model, "model")
        if api_key is not None:
            _require_nonempty(api_key, "api_key")
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
            raise ValueError("max_tokens must be a positive integer or None")
        self.max_tokens = max_tokens or _DEFAULT_MAX_TOKENS
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - dependency is installed in production
                raise AnthropicMessagesError("The anthropic package is required for the default client") from exc
            self.client = anthropic.Anthropic() if api_key is None else anthropic.Anthropic(api_key=api_key)
        else:
            self.client = client

    def open_session(
        self,
        *,
        instructions: str,
        tools: tuple[ModelToolDefinition, ...],
        allow_parallel_tool_calls: bool,
    ) -> AnthropicMessagesSession:
        if not isinstance(instructions, str):
            raise ValueError("instructions must be a string")
        if type(allow_parallel_tool_calls) is not bool:
            raise ValueError("allow_parallel_tool_calls must be a bool")
        return AnthropicMessagesSession(
            client=self.client,
            model=self.model,
            instructions=instructions,
            tools=tools,
            allow_parallel_tool_calls=allow_parallel_tool_calls,
            max_tokens=self.max_tokens,
        )


class AnthropicMessagesSession:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        instructions: str,
        tools: tuple[ModelToolDefinition, ...],
        allow_parallel_tool_calls: bool,
        max_tokens: int,
    ) -> None:
        self._client = client
        self._model = model
        self._instructions = instructions
        self._tools = _anthropic_tools(tuple(tools))
        self._allow_parallel_tool_calls = allow_parallel_tool_calls
        self._max_tokens = max_tokens
        self._use_thinking = not model.startswith(_NO_ADAPTIVE_THINKING_PREFIX)
        self._messages: list[dict[str, Any]] = []
        self._started = False

    def respond(self, input_items: tuple[ModelInputItem, ...]) -> ModelTurn:
        if not input_items:
            raise ValueError("input_items must not be empty")
        if not isinstance(input_items, tuple):
            raise TypeError("input_items must be a tuple")
        if not self._started:
            if len(input_items) != 1 or not isinstance(input_items[0], UserInput):
                raise TypeError("the first response input must contain exactly one UserInput")
            self._messages.append({"role": "user", "content": input_items[0].text})
        else:
            if any(not isinstance(item, ToolResultInput) for item in input_items):
                raise TypeError("continuation response input must contain only ToolResultInput items")
            blocks = [self._tool_result_block(item.result) for item in input_items]
            self._messages.append({"role": "user", "content": blocks})

        total_usage = ModelUsage()
        resumes = 0
        while True:
            response = self._client.messages.create(**self._build_payload())
            parsed = _parse_response(response)
            total_usage = _sum_usage(total_usage, parsed.usage)
            # Store the full response.content object unchanged — including
            # any thinking/redacted_thinking blocks — per Anthropic's
            # preserved-thinking contract. Never reconstruct these blocks.
            self._messages.append({"role": "assistant", "content": parsed.content})
            self._started = True
            if parsed.raw_stop_reason == "pause_turn" and resumes < _MAX_PAUSE_TURN_RESUMES:
                # Server-side tool loop paused mid-turn: per the API docs,
                # resend without adding a new user message and the server
                # resumes automatically from the trailing content.
                resumes += 1
                continue
            stop_reason = _map_stop_reason(parsed.raw_stop_reason, has_tool_calls=bool(parsed.tool_calls))
            return ModelTurn(parsed.text, parsed.tool_calls, stop_reason, total_usage)

    @staticmethod
    def _tool_result_block(result: ModelToolResult) -> dict[str, Any]:
        # Same {ok, content, metadata} envelope as the other backends: metadata
        # carries facts the model needs (e.g. truncation / line ranges of a read).
        envelope = {
            "ok": not result.is_error,
            "content": result.content,
            "metadata": result.metadata,
        }
        return {
            "type": "tool_result",
            "tool_use_id": result.call_id,
            "content": json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            "is_error": result.is_error,
        }

    def _build_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": list(self._messages),
            # Top-level auto-caching: cheap win for multi-turn agent loops,
            # caches the last cacheable prefix block (system + tools here).
            "cache_control": {"type": "ephemeral"},
        }
        if self._instructions:
            payload["system"] = self._instructions
        if self._tools:
            payload["tools"] = list(self._tools)
            if not self._allow_parallel_tool_calls:
                payload["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        if self._use_thinking:
            # Adaptive thinking has no budget_tokens knob on current models
            # (see the claude-api skill docs); never set temperature/top_p
            # alongside it either — both are rejected on adaptive-thinking
            # requests.
            payload["thinking"] = {"type": "adaptive"}
        return payload
