"""OpenAI-compatible Chat Completions adapter for the native provider-neutral agent loop.

Targets any endpoint that speaks ``POST {base_url}/chat/completions`` with OpenAI-style
function/tool calling: DeepSeek, Gemini's OpenAI-compat endpoint, OpenRouter, Groq,
Mistral, xAI, Ollama, custom OpenAI-compatible servers, and OpenAI itself.

Unlike the Responses API, Chat Completions is stateless per request: this session
resends the full message history (system + user + assistant + tool) on every call
instead of relying on a ``previous_response_id``.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Mapping, Sequence
from typing import Any

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


class ChatCompletionsError(Exception):
    """Base error for the Chat Completions adapter."""


class ChatCompletionsProtocolError(ChatCompletionsError):
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
        raise ChatCompletionsProtocolError(f"{label} is not strict JSON: {exc}") from exc


def _copy_json(value: Any, label: str) -> Any:
    return json.loads(_strict_json(value, label))


def _chat_tools(tools: tuple[ModelToolDefinition, ...]) -> tuple[dict[str, Any], ...]:
    converted = []
    for tool in tools:
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": _copy_json(tool.input_schema, f"tool {tool.name} schema"),
                },
            }
        )
    return tuple(converted)


def _usage(response: Any) -> ModelUsage:
    raw_usage = _field(response, "usage")
    if raw_usage is None:
        return ModelUsage()

    values = {}
    for output_name, provider_name in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
    ):
        raw = _field(raw_usage, provider_name, 0)
        if raw is None:
            raw = 0
        if type(raw) is not int or raw < 0:
            raise ChatCompletionsProtocolError(
                f"Response usage {output_name} must be a non-negative integer"
            )
        values[output_name] = raw
    # Note: some providers (e.g. OpenAI) report cached prompt tokens as a subset of
    # prompt_tokens via usage.prompt_tokens_details.cached_tokens. ModelUsage has no
    # dedicated field for this, and cached tokens are already included in
    # prompt_tokens, so no separate accounting is needed or possible here.
    return ModelUsage(**values)


class ChatCompletionsBackend:
    """Synchronous OpenAI-compatible Chat Completions backend with injectable SDK client."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client: Any = None,
        max_tokens: int | None = None,
    ) -> None:
        self.model = _require_nonempty(model, "model")
        if api_key is not None:
            _require_nonempty(api_key, "api_key")
        if base_url is not None:
            _require_nonempty(base_url, "base_url")
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
            raise ValueError("max_tokens must be a positive integer or None")
        self.base_url = base_url
        self.max_tokens = max_tokens
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - dependency is installed in production
                raise ChatCompletionsError("The openai package is required for the default client") from exc
            kwargs: dict[str, Any] = {}
            if api_key is not None:
                kwargs["api_key"] = api_key
            if base_url is not None:
                kwargs["base_url"] = base_url
            self.client = OpenAI(**kwargs)
        else:
            self.client = client

    def open_session(
        self,
        *,
        instructions: str,
        tools: tuple[ModelToolDefinition, ...],
        allow_parallel_tool_calls: bool,
    ) -> ChatCompletionsSession:
        if not isinstance(instructions, str):
            raise ValueError("instructions must be a string")
        if type(allow_parallel_tool_calls) is not bool:
            raise ValueError("allow_parallel_tool_calls must be a bool")
        return ChatCompletionsSession(
            client=self.client,
            model=self.model,
            instructions=instructions,
            tools=tools,
            allow_parallel_tool_calls=allow_parallel_tool_calls,
            max_tokens=self.max_tokens,
        )


class ChatCompletionsSession:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        instructions: str,
        tools: tuple[ModelToolDefinition, ...],
        allow_parallel_tool_calls: bool,
        max_tokens: int | None,
    ) -> None:
        self._client = client
        self._model = model
        self._tools = _chat_tools(tuple(tools))
        self._allow_parallel_tool_calls = allow_parallel_tool_calls
        self._max_tokens = max_tokens
        self._messages: list[dict[str, Any]] = []
        if instructions:
            self._messages.append({"role": "system", "content": instructions})
        self._started = False
        self._auto_id_seq = itertools.count(1)

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
            for item in input_items:
                self._messages.append(self._tool_message(item.result))

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": _copy_json(self._messages, "chat messages"),
        }
        if self._tools:
            # parallel_tool_calls is only accepted by (most) OpenAI-compatible servers
            # when tools are present; sending it with an empty tools list is rejected
            # by OpenAI itself and tolerated inconsistently elsewhere, so it is only
            # included when there is at least one tool.
            payload["tools"] = list(self._tools)
            payload["parallel_tool_calls"] = self._allow_parallel_tool_calls
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens

        response = self._client.chat.completions.create(**payload)
        turn, assistant_message = self._parse_response(response)
        self._messages.append(assistant_message)
        self._started = True
        return turn

    @staticmethod
    def _tool_message(result: ModelToolResult) -> dict[str, Any]:
        envelope = {
            "ok": not result.is_error,
            "content": result.content,
            "metadata": result.metadata,
        }
        return {
            "role": "tool",
            "tool_call_id": result.call_id,
            "content": _strict_json(envelope, "tool result"),
        }

    def _next_auto_call_id(self) -> str:
        return f"call_auto_{next(self._auto_id_seq)}"

    def _parse_response(self, response: Any) -> tuple[ModelTurn, dict[str, Any]]:
        choices = _field(response, "choices")
        if (
            choices is None
            or isinstance(choices, (str, bytes, bytearray))
            or not isinstance(choices, Sequence)
            or len(choices) == 0
        ):
            raise ChatCompletionsProtocolError("chat.completions response.choices must be a non-empty sequence")
        choice = choices[0]
        message = _field(choice, "message")
        if message is None:
            raise ChatCompletionsProtocolError("chat.completions choice is missing a message")
        finish_reason = _field(choice, "finish_reason")

        raw_content = _field(message, "content")
        text = raw_content if isinstance(raw_content, str) else ""

        calls, stored_tool_calls = self._parse_tool_calls(_field(message, "tool_calls"))

        refusal = finish_reason == "content_filter"
        raw_refusal = _field(message, "refusal")
        if isinstance(raw_refusal, str) and raw_refusal.strip():
            refusal = True
            if not text:
                text = raw_refusal

        # DeepSeek's reasoning models (deepseek-reasoner) attach a separate
        # message.reasoning_content field. It is intentionally never read here, so
        # it never enters `text` and is never echoed back in a later request.

        if calls:
            stop_reason = ModelStopReason.TOOL_USE
        elif refusal:
            stop_reason = ModelStopReason.REFUSAL
        elif finish_reason == "length":
            stop_reason = ModelStopReason.LENGTH
        elif finish_reason == "stop":
            stop_reason = ModelStopReason.COMPLETED
        else:
            stop_reason = ModelStopReason.OTHER

        assistant_message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if stored_tool_calls:
            assistant_message["tool_calls"] = stored_tool_calls

        turn = ModelTurn(text, tuple(calls), stop_reason, _usage(response))
        return turn, assistant_message

    def _parse_tool_calls(
        self, raw_tool_calls: Any
    ) -> tuple[list[ModelToolCall], list[dict[str, Any]]]:
        calls: list[ModelToolCall] = []
        stored: list[dict[str, Any]] = []
        if raw_tool_calls is None:
            return calls, stored
        if isinstance(raw_tool_calls, (str, bytes, bytearray)) or not isinstance(raw_tool_calls, Sequence):
            raise ChatCompletionsProtocolError("message.tool_calls must be a sequence")

        for tool_call in raw_tool_calls:
            call_id = _field(tool_call, "id")
            if not isinstance(call_id, str) or not call_id.strip():
                call_id = self._next_auto_call_id()

            function = _field(tool_call, "function")
            if function is None:
                raise ChatCompletionsProtocolError("tool_call.function is required")
            name = _field(function, "name")
            if not isinstance(name, str) or not name.strip():
                raise ChatCompletionsProtocolError("tool_call.function.name must be a non-empty string")
            arguments = _field(function, "arguments")
            if not isinstance(arguments, str):
                raise ChatCompletionsProtocolError("tool_call.function.arguments must be a JSON string")
            try:
                decoded = json.loads(
                    arguments,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"unsupported JSON constant: {value}")
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise ChatCompletionsProtocolError("tool_call.function.arguments is invalid JSON") from exc
            if not isinstance(decoded, dict):
                raise ChatCompletionsProtocolError("tool_call.function.arguments must decode to an object")
            try:
                calls.append(ModelToolCall(call_id, name, decoded))
            except Exception as exc:
                raise ChatCompletionsProtocolError(f"Invalid tool_call: {exc}") from exc

            stored.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
        return calls, stored
