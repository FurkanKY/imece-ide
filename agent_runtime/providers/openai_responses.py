"""OpenAI Responses API adapter for the native provider-neutral agent loop."""

from __future__ import annotations

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


class OpenAIResponsesError(Exception):
    """Base error for the OpenAI Responses adapter."""


class OpenAIResponsesProtocolError(OpenAIResponsesError):
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
        raise OpenAIResponsesProtocolError(f"{label} is not strict JSON: {exc}") from exc


def _copy_json(value: Any, label: str) -> Any:
    return json.loads(_strict_json(value, label))


def _response_tools(tools: tuple[ModelToolDefinition, ...]) -> tuple[dict[str, Any], ...]:
    converted = []
    for tool in tools:
        converted.append(
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": _copy_json(tool.input_schema, f"tool {tool.name} schema"),
                "strict": False,
            }
        )
    return tuple(converted)


def _usage(response: Any) -> ModelUsage:
    raw_usage = _field(response, "usage")
    if raw_usage is None:
        return ModelUsage()

    values = {}
    for output_name, provider_name in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
    ):
        raw = _field(raw_usage, provider_name, 0)
        if raw is None:
            raw = 0
        if type(raw) is not int or raw < 0:
            raise OpenAIResponsesProtocolError(
                f"Response usage {output_name} must be a non-negative integer"
            )
        values[output_name] = raw
    return ModelUsage(**values)


def _text_and_refusal(item: Any) -> tuple[list[str], bool]:
    texts: list[str] = []
    refusal = _field(item, "type") == "refusal"
    if refusal:
        refusal_text = _field(item, "refusal", _field(item, "text"))
        if isinstance(refusal_text, str):
            texts.append(refusal_text)
    content = _field(item, "content", ())
    if isinstance(content, (str, bytes, bytearray)) or not isinstance(content, Sequence):
        content = (content,) if content else ()
    for part in content:
        part_type = _field(part, "type")
        if part_type in ("output_text", "text"):
            text = _field(part, "text")
            if isinstance(text, str):
                texts.append(text)
        elif part_type in ("refusal", "refusal_text"):
            refusal = True
            text = _field(part, "refusal", _field(part, "text"))
            if isinstance(text, str):
                texts.append(text)
    return texts, refusal


def _parse_response(response: Any) -> ModelTurn:
    response_id = _field(response, "id")
    if not isinstance(response_id, str) or not response_id.strip():
        raise OpenAIResponsesProtocolError("Responses API response is missing a non-empty id")

    output = _field(response, "output")
    if output is None or isinstance(output, (str, bytes, bytearray)) or not isinstance(output, Sequence):
        raise OpenAIResponsesProtocolError("Responses API response.output must be a sequence")

    calls: list[ModelToolCall] = []
    text_parts: list[str] = []
    refusal = False
    for item in output:
        item_type = _field(item, "type")
        if item_type == "function_call":
            call_id = _field(item, "call_id")
            name = _field(item, "name")
            arguments = _field(item, "arguments")
            if not isinstance(arguments, str):
                raise OpenAIResponsesProtocolError("function_call.arguments must be a JSON string")
            try:
                decoded = json.loads(
                    arguments,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"unsupported JSON constant: {value}")
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise OpenAIResponsesProtocolError("function_call.arguments is invalid JSON") from exc
            if not isinstance(decoded, dict):
                raise OpenAIResponsesProtocolError("function_call.arguments must decode to an object")
            try:
                calls.append(ModelToolCall(call_id, name, decoded))
            except Exception as exc:
                raise OpenAIResponsesProtocolError(f"Invalid function_call: {exc}") from exc
        elif item_type == "message":
            parts, item_refusal = _text_and_refusal(item)
            text_parts.extend(parts)
            refusal = refusal or item_refusal
        elif item_type in ("refusal", "refusal_message"):
            parts, item_refusal = _text_and_refusal(item)
            text_parts.extend(parts)
            refusal = True
        # Reasoning and other provider-internal output items are intentionally ignored.

    status = _field(response, "status")
    if calls:
        stop_reason = ModelStopReason.TOOL_USE
    elif refusal:
        stop_reason = ModelStopReason.REFUSAL
    elif status == "incomplete":
        details = _field(response, "incomplete_details")
        reason = _field(details, "reason")
        stop_reason = (
            ModelStopReason.LENGTH
            if reason == "max_output_tokens"
            else ModelStopReason.OTHER
        )
    elif status in ("failed", "error"):
        stop_reason = ModelStopReason.ERROR
    elif status == "completed":
        stop_reason = ModelStopReason.COMPLETED
    else:
        stop_reason = ModelStopReason.OTHER

    return ModelTurn(
        "".join(text_parts),
        tuple(calls),
        stop_reason,
        _usage(response),
    )


class OpenAIResponsesBackend:
    """Synchronous OpenAI Responses backend with injectable SDK client."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        client: Any = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.model = _require_nonempty(model, "model")
        if api_key is not None:
            _require_nonempty(api_key, "api_key")
        if max_output_tokens is not None and (
            type(max_output_tokens) is not int or max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer or None")
        if reasoning_effort is not None:
            _require_nonempty(reasoning_effort, "reasoning_effort")
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - dependency is installed in production
                raise OpenAIResponsesError("The openai package is required for the default client") from exc
            self.client = OpenAI() if api_key is None else OpenAI(api_key=api_key)
        else:
            self.client = client

    def open_session(
        self,
        *,
        instructions: str,
        tools: tuple[ModelToolDefinition, ...],
        allow_parallel_tool_calls: bool,
    ) -> OpenAIResponsesSession:
        if not isinstance(instructions, str):
            raise ValueError("instructions must be a string")
        if type(allow_parallel_tool_calls) is not bool:
            raise ValueError("allow_parallel_tool_calls must be a bool")
        return OpenAIResponsesSession(
            client=self.client,
            model=self.model,
            instructions=instructions,
            tools=tools,
            allow_parallel_tool_calls=allow_parallel_tool_calls,
            max_output_tokens=self.max_output_tokens,
            reasoning_effort=self.reasoning_effort,
        )


class OpenAIResponsesSession:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        instructions: str,
        tools: tuple[ModelToolDefinition, ...],
        allow_parallel_tool_calls: bool,
        max_output_tokens: int | None,
        reasoning_effort: str | None,
    ) -> None:
        self._client = client
        self._model = model
        self._instructions = instructions
        self._tools = _response_tools(tuple(tools))
        self._allow_parallel_tool_calls = allow_parallel_tool_calls
        self._max_output_tokens = max_output_tokens
        self._reasoning_effort = reasoning_effort
        self._previous_response_id: str | None = None
        self._started = False

    def respond(self, input_items: tuple[ModelInputItem, ...]) -> ModelTurn:
        if not input_items:
            raise ValueError("input_items must not be empty")
        if not isinstance(input_items, tuple):
            raise TypeError("input_items must be a tuple")
        if not self._started:
            if len(input_items) != 1 or not isinstance(input_items[0], UserInput):
                raise TypeError("the first response input must contain exactly one UserInput")
        elif any(not isinstance(item, ToolResultInput) for item in input_items):
            raise TypeError("continuation response input must contain only ToolResultInput items")

        request_input = self._convert_inputs(input_items)
        payload: dict[str, Any] = {
            "model": self._model,
            "instructions": self._instructions,
            "input": request_input,
            "tools": _copy_json(self._tools, "Responses tools"),
            "parallel_tool_calls": self._allow_parallel_tool_calls,
        }
        if self._previous_response_id is not None:
            payload["previous_response_id"] = self._previous_response_id
        if self._max_output_tokens is not None:
            payload["max_output_tokens"] = self._max_output_tokens
        if self._reasoning_effort is not None:
            payload["reasoning"] = {"effort": self._reasoning_effort}

        response = self._client.responses.create(**payload)
        response_id = _field(response, "id")
        if not isinstance(response_id, str) or not response_id.strip():
            raise OpenAIResponsesProtocolError("Responses API response is missing a non-empty id")
        turn = _parse_response(response)
        self._previous_response_id = response_id
        self._started = True
        return turn

    @staticmethod
    def _convert_inputs(input_items: tuple[ModelInputItem, ...]) -> list[dict[str, Any]]:
        converted = []
        for item in input_items:
            if isinstance(item, UserInput):
                converted.append({"role": "user", "content": item.text})
            elif isinstance(item, ToolResultInput):
                envelope = {
                    "ok": not item.result.is_error,
                    "content": item.result.content,
                    "metadata": item.result.metadata,
                }
                converted.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.result.call_id,
                        "output": _strict_json(envelope, "function_call_output"),
                    }
                )
            else:
                raise TypeError(f"unsupported model input item: {type(item).__name__}")
        return converted
