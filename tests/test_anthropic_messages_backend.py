import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import (  # noqa: E402
    AgentBackendError,
    AgentSession,
    ModelStopReason,
    ModelToolCall,
    ModelToolDefinition,
    ModelToolResult,
    ToolResultInput,
    UserInput,
)
from agent_runtime.providers.anthropic_messages import (  # noqa: E402
    AnthropicMessagesBackend,
    AnthropicMessagesProtocolError,
)
from tool_runtime import (  # noqa: E402
    PermissionEffect,
    PermissionRule,
    PolicyEvaluator,
    ToolExecutionContext,
    ToolRegistry,
)
from tool_runtime.tools.workspace_files import register_workspace_tools  # noqa: E402
from workspace.local import LocalWorkspace  # noqa: E402


SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}



def _envelope(content, *, ok=True, metadata=None):
    return json.dumps({"ok": ok, "content": content, "metadata": metadata or {}}, ensure_ascii=False, sort_keys=True)

def obj(**values):
    return SimpleNamespace(**values)


def usage(input_tokens=0, output_tokens=0, **extra):
    return obj(input_tokens=input_tokens, output_tokens=output_tokens, **extra)


def text_block(text):
    return obj(type="text", text=text)


def thinking_block(thinking="chain of thought", signature="sig-123"):
    return obj(type="thinking", thinking=thinking, signature=signature)


def tool_use_block(call_id, name="read_file", input=None):
    return obj(type="tool_use", id=call_id, name=name, input=input or {"path": "a.py"})


def response(content, stop_reason="end_turn", usage_obj=None, response_id="msg_1"):
    return obj(id=response_id, content=list(content), stop_reason=stop_reason, usage=usage_obj or usage())


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("fake response script exhausted")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def tool_definition(name="read_file"):
    return ModelToolDefinition(name, f"{name} description", SCHEMA)


def provider_session(client, tools=(tool_definition(),), model="claude-opus-5", **kwargs):
    backend = AnthropicMessagesBackend(model, client=client, **kwargs)
    return backend.open_session(
        instructions="system prompt",
        tools=tuple(tools),
        allow_parallel_tool_calls=False,
    )


def test_request_construction_and_message_history_accumulation():
    client = FakeClient([
        response([text_block("first")]),
        response([text_block("second")]),
        response([text_block("third")]),
    ])
    session = provider_session(client, max_tokens=123)
    assert session.respond((UserInput("task\nexact"),)).text == "first"
    session.respond((ToolResultInput(ModelToolResult("call", "output", False, {"x": 1})),))
    session.respond((ToolResultInput(ModelToolResult("call-2", "output", True)),))

    assert len(client.messages.calls) == 3
    first, second, third = client.messages.calls
    assert first["model"] == "claude-opus-5"
    assert first["max_tokens"] == 123
    assert first["system"] == "system prompt"
    assert first["messages"][0] == {"role": "user", "content": "task\nexact"}
    assert first["cache_control"] == {"type": "ephemeral"}
    assert all(call["thinking"] == {"type": "adaptive"} for call in client.messages.calls)

    # message history accumulates: user, assistant(1), tool(1), assistant(2), tool(2)
    assert second["messages"][1]["role"] == "assistant"
    tool_msg = second["messages"][2]
    assert tool_msg["role"] == "user"
    assert tool_msg["content"] == [
        {"type": "tool_result", "tool_use_id": "call", "content": _envelope("output", metadata={"x": 1}), "is_error": False}
    ]

    tool_msg_2 = third["messages"][4]
    assert tool_msg_2["content"][0]["is_error"] is True


def test_tools_mapped_to_input_schema_shape_and_isolated():
    first = tool_definition("first")
    second = tool_definition("second")
    client = FakeClient([response([text_block("done")])])
    session = provider_session(client, (first, second))
    session.respond((UserInput("task"),))
    tools = client.messages.calls[0]["tools"]
    assert [tool["name"] for tool in tools] == ["first", "second"]
    assert all("input_schema" in tool for tool in tools)
    tools[0]["input_schema"]["properties"]["path"]["type"] = "number"
    assert first.input_schema["properties"]["path"]["type"] == "string"
    assert session is not None


def test_no_tools_omits_tools_and_tool_choice():
    client = FakeClient([response([text_block("done")])])
    session = provider_session(client, tools=())
    session.respond((UserInput("task"),))
    payload = client.messages.calls[0]
    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_disable_parallel_tool_use_set_when_tools_present():
    client = FakeClient([response([text_block("done")])])
    session = provider_session(client)
    session.respond((UserInput("task"),))
    payload = client.messages.calls[0]
    assert payload["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}


def test_tool_call_round_trip_and_parallel_tool_use_blocks_in_single_result_message():
    client = FakeClient([
        response(
            [tool_use_block("call_a", input={"path": "a.py"}), tool_use_block("call_b", input={"path": "b.py"})],
            stop_reason="tool_use",
        ),
        response([text_block("done")]),
    ])
    session = provider_session(client)
    turn = session.respond((UserInput("task"),))
    assert turn.stop_reason is ModelStopReason.TOOL_USE
    assert [(call.call_id, call.name, call.arguments) for call in turn.tool_calls] == [
        ("call_a", "read_file", {"path": "a.py"}),
        ("call_b", "read_file", {"path": "b.py"}),
    ]

    # All tool_results for a batch go back in ONE user message.
    session.respond(
        (
            ToolResultInput(ModelToolResult("call_a", "out-a", False)),
            ToolResultInput(ModelToolResult("call_b", "out-b", True)),
        )
    )
    second_call = client.messages.calls[1]
    tool_result_message = second_call["messages"][-1]
    assert tool_result_message["role"] == "user"
    assert tool_result_message["content"] == [
        {"type": "tool_result", "tool_use_id": "call_a", "content": _envelope("out-a"), "is_error": False},
        {"type": "tool_result", "tool_use_id": "call_b", "content": _envelope("out-b", ok=False), "is_error": True},
    ]


def test_thinking_blocks_preserved_verbatim_in_history():
    thinking = thinking_block()
    tool_call = tool_use_block("call_a")
    client = FakeClient([
        response([thinking, tool_call], stop_reason="tool_use"),
        response([text_block("done")]),
    ])
    session = provider_session(client)
    session.respond((UserInput("task"),))
    session.respond((ToolResultInput(ModelToolResult("call_a", "ok", False)),))

    second_call = client.messages.calls[1]
    assistant_message = second_call["messages"][1]
    assert assistant_message["role"] == "assistant"
    # The full content list — including the thinking block — is replayed
    # unchanged: same object identity, not a reconstruction.
    assert assistant_message["content"][0] is thinking
    assert assistant_message["content"][1] is tool_call


def test_haiku_model_omits_thinking():
    client = FakeClient([response([text_block("done")])])
    session = provider_session(client, model="claude-haiku-4-5")
    session.respond((UserInput("task"),))
    assert "thinking" not in client.messages.calls[0]


def test_non_haiku_model_sends_adaptive_thinking():
    client = FakeClient([response([text_block("done")])])
    session = provider_session(client, model="claude-sonnet-5")
    session.respond((UserInput("task"),))
    assert client.messages.calls[0]["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", ModelStopReason.COMPLETED),
        ("max_tokens", ModelStopReason.LENGTH),
        ("refusal", ModelStopReason.REFUSAL),
        ("stop_sequence", ModelStopReason.COMPLETED),
        ("unknown_thing", ModelStopReason.OTHER),
    ],
)
def test_stop_reason_mapping(stop_reason, expected):
    client = FakeClient([response([text_block("text")], stop_reason=stop_reason)])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.stop_reason is expected


def test_tool_use_blocks_take_precedence_over_stop_reason():
    client = FakeClient([response([tool_use_block("call")], stop_reason="end_turn")])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.stop_reason is ModelStopReason.TOOL_USE


def test_pause_turn_resumes_automatically_without_new_user_message():
    client = FakeClient([
        response([text_block("partial")], stop_reason="pause_turn", usage_obj=usage(5, 5)),
        response([text_block("rest")], stop_reason="end_turn", usage_obj=usage(3, 4)),
    ])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.stop_reason is ModelStopReason.COMPLETED
    assert turn.text == "rest"
    # Usage from both internal turns is accumulated into the returned turn.
    assert turn.usage.input_tokens == 8
    assert turn.usage.output_tokens == 9
    # Second internal request resumed with no new user message appended —
    # just the paused assistant turn.
    assert len(client.messages.calls) == 2
    assert client.messages.calls[1]["messages"][-1]["role"] == "assistant"


def test_pause_turn_gives_up_after_max_resumes_and_maps_to_other():
    paused = [response([text_block(f"chunk-{i}")], stop_reason="pause_turn") for i in range(10)]
    client = FakeClient(paused)
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.stop_reason is ModelStopReason.OTHER
    # 1 initial + 5 bounded resumes
    assert len(client.messages.calls) == 6


def test_usage_mapping_folds_cache_tokens_into_input_tokens():
    client = FakeClient([
        response(
            [text_block("done")],
            usage_obj=usage(input_tokens=11, output_tokens=22, cache_read_input_tokens=3, cache_creation_input_tokens=4),
        )
    ])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.usage.input_tokens == 11 + 3 + 4
    assert turn.usage.output_tokens == 22


def test_missing_content_is_protocol_error():
    client = FakeClient([obj(id="msg", content=None, stop_reason="end_turn", usage=usage())])
    with pytest.raises(AnthropicMessagesProtocolError):
        provider_session(client).respond((UserInput("task"),))


def test_invalid_tool_use_input_is_protocol_error():
    bad_block = obj(type="tool_use", id="call", name="read_file", input="not-an-object")
    client = FakeClient([response([bad_block], stop_reason="tool_use")])
    with pytest.raises(AnthropicMessagesProtocolError):
        provider_session(client).respond((UserInput("task"),))


@pytest.mark.parametrize("bad_input", [(), (ToolResultInput(ModelToolResult("call", "x", False)),)])
def test_input_sequence_validation(bad_input):
    client = FakeClient([response([text_block("done")])])
    session = provider_session(client)
    with pytest.raises((TypeError, ValueError)):
        session.respond(bad_input)


def test_backend_does_not_leak_api_key():
    client = FakeClient([response([text_block("done")])])
    backend = AnthropicMessagesBackend("claude-opus-5", api_key="secret-test-key", client=client)
    backend.open_session(instructions="i", tools=(), allow_parallel_tool_calls=False).respond((UserInput("x"),))
    assert all("secret-test-key" not in repr(call) for call in client.messages.calls)


def test_smoke_construct_real_anthropic_client_through_factory():
    import anthropic

    from anthropic_catalog import anthropic_backend_from_config

    backend = anthropic_backend_from_config(api_key="sk-test", model="claude-opus-5")
    assert isinstance(backend.client, anthropic.Anthropic)
    assert backend.model == "claude-opus-5"


# Opt-in gate for the single test that talks to the real Anthropic API. The key
# below is a deliberately invalid placeholder (never a real secret), but the call
# still leaves the machine, so the default suite stays offline/deterministic.
# Run it explicitly with: IMECE_RUN_LIVE_API_TESTS=1 pytest tests/test_anthropic_messages_backend.py
_RUN_LIVE_API_TESTS = os.environ.get("IMECE_RUN_LIVE_API_TESTS") == "1"


@pytest.mark.skipif(
    not _RUN_LIVE_API_TESTS,
    reason="live Anthropic API call (opt-in): set IMECE_RUN_LIVE_API_TESTS=1 to run",
)
def test_real_invalid_key_request_raises_authentication_error():
    import anthropic

    client = anthropic.Anthropic(api_key="sk-ant-invalid-test-key-00000000000000000000")
    with pytest.raises(anthropic.AuthenticationError):
        client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=16,
            messages=[{"role": "user", "content": "hi"}],
        )


class _FakeAuthenticationError(Exception):
    """Stand-in for anthropic.AuthenticationError: same propagation contract, no network."""


def test_sdk_errors_propagate_and_are_wrapped_by_agent_session_like_other_backends(tmp_path):
    # Neither the Responses nor the Chat Completions backend catches/re-maps
    # SDK exceptions themselves — they let them propagate out of respond(),
    # and AgentSession._respond is what wraps any exception into
    # AgentBackendError. This backend follows the same design.
    client = FakeClient([_FakeAuthenticationError("invalid api key: secret-test-key")])
    backend = AnthropicMessagesBackend("claude-opus-5", client=client)
    registry = ToolRegistry()
    policy = PolicyEvaluator([PermissionRule("*", "*", PermissionEffect.ALLOW)])
    session = AgentSession(
        backend=backend,
        registry=registry,
        policy=policy,
        context=ToolExecutionContext(LocalWorkspace(tmp_path)),
    )
    with pytest.raises(AgentBackendError):
        session.start("do something")


def test_real_agent_harness_with_fake_anthropic_client(tmp_path):
    (tmp_path / "input.txt").write_text("original", encoding="utf-8")
    client = FakeClient([
        response([tool_use_block("read-call", "read_file", {"path": "input.txt"})], stop_reason="tool_use"),
        response(
            [tool_use_block("write-call", "write_file", {"path": "output.txt", "content": "changed"})],
            stop_reason="tool_use",
        ),
        response([text_block("finished")]),
    ])
    backend = AnthropicMessagesBackend("claude-opus-5", client=client)
    registry = ToolRegistry()
    register_workspace_tools(registry)
    policy = PolicyEvaluator([PermissionRule("*", "*", PermissionEffect.ALLOW)])
    session = AgentSession(
        backend=backend,
        registry=registry,
        policy=policy,
        context=ToolExecutionContext(LocalWorkspace(tmp_path)),
    )
    outcome = session.start("inspect then change")
    assert outcome.final_text == "finished"
    assert outcome.model_turns == 3
    assert outcome.tool_calls == 2
    assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "changed"
    assert client.messages.calls[1]["messages"][-1]["content"][0]["tool_use_id"] == "read-call"
    assert client.messages.calls[2]["messages"][-1]["content"][0]["tool_use_id"] == "write-call"
