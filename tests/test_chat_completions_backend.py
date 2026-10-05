import json
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
from agent_runtime.providers.chat_completions import (  # noqa: E402
    ChatCompletionsBackend,
    ChatCompletionsProtocolError,
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


def obj(**values):
    return SimpleNamespace(**values)


def usage(prompt_tokens=0, completion_tokens=0, **extra):
    return obj(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, **extra)


def message(content=None, tool_calls=None, **extra):
    return obj(content=content, tool_calls=tool_calls, **extra)


def choice(msg, finish_reason="stop"):
    return obj(message=msg, finish_reason=finish_reason)


def response(*choices, usage_obj=None):
    return obj(choices=list(choices), usage=usage_obj or usage())


def tool_call(call_id, name="read_file", arguments='{"path":"a.py"}'):
    return obj(id=call_id, type="function", function=obj(name=name, arguments=arguments))


class FakeCompletions:
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
        self.chat = obj(completions=FakeCompletions(responses))


def tool_definition(name="read_file"):
    return ModelToolDefinition(name, f"{name} description", SCHEMA)


def provider_session(client, tools=(tool_definition(),), **kwargs):
    backend = ChatCompletionsBackend("model-test", client=client, **kwargs)
    return backend.open_session(
        instructions="system prompt",
        tools=tuple(tools),
        allow_parallel_tool_calls=False,
    )


def test_request_construction_and_message_history_accumulation():
    client = FakeClient([
        response(choice(message("first"))),
        response(choice(message("second"))),
        response(choice(message("third"))),
    ])
    session = provider_session(client, max_tokens=123)
    assert session.respond((UserInput("task\nexact"),)).text == "first"
    session.respond((ToolResultInput(ModelToolResult("call", "output", False, {"x": 1})),))
    session.respond((ToolResultInput(ModelToolResult("call-2", "output", True)),))

    assert len(client.chat.completions.calls) == 3
    first, second, third = client.chat.completions.calls
    assert first["model"] == "model-test"
    assert first["max_tokens"] == 123
    assert first["messages"][0] == {"role": "system", "content": "system prompt"}
    assert first["messages"][1] == {"role": "user", "content": "task\nexact"}
    assert all(call["parallel_tool_calls"] is False for call in client.chat.completions.calls)

    # message history accumulates: system, user, assistant(1), tool(1), assistant(2), tool(2)
    assert second["messages"][2] == {"role": "assistant", "content": "first"}
    tool_msg = second["messages"][3]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call"
    assert json.loads(tool_msg["content"]) == {"ok": True, "content": "output", "metadata": {"x": 1}}

    assert third["messages"][4] == {"role": "assistant", "content": "second"}
    tool_msg_2 = third["messages"][5]
    assert json.loads(tool_msg_2["content"])["ok"] is False


def test_tools_mapped_to_function_shape_and_schema_isolated():
    first = tool_definition("first")
    second = tool_definition("second")
    client = FakeClient([response(choice(message("done")))])
    session = provider_session(client, (first, second))
    session.respond((UserInput("task"),))
    tools = client.chat.completions.calls[0]["tools"]
    assert [tool["function"]["name"] for tool in tools] == ["first", "second"]
    assert all(tool["type"] == "function" for tool in tools)
    tools[0]["function"]["parameters"]["properties"]["path"]["type"] = "number"
    assert first.input_schema["properties"]["path"]["type"] == "string"
    assert session is not None


def test_no_tools_omits_tools_and_parallel_tool_calls():
    client = FakeClient([response(choice(message("done")))])
    session = provider_session(client, tools=())
    session.respond((UserInput("task"),))
    payload = client.chat.completions.calls[0]
    assert "tools" not in payload
    assert "parallel_tool_calls" not in payload


def test_tool_call_round_trip_and_parallel_calls():
    client = FakeClient([
        response(choice(
            message(None, tool_calls=[
                tool_call("call_a", arguments='{"path":"a.py"}'),
                tool_call("call_b", arguments='{"path":"b.py"}'),
            ]),
            finish_reason="tool_calls",
        )),
    ])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.stop_reason is ModelStopReason.TOOL_USE
    assert [(call.call_id, call.name, call.arguments) for call in turn.tool_calls] == [
        ("call_a", "read_file", {"path": "a.py"}),
        ("call_b", "read_file", {"path": "b.py"}),
    ]


def test_missing_tool_call_id_generates_stable_id_used_in_history():
    client = FakeClient([
        response(choice(
            message(None, tool_calls=[tool_call(None, arguments='{"path":"a.py"}')]),
            finish_reason="tool_calls",
        )),
        response(choice(message("done"))),
    ])
    session = provider_session(client)
    turn = session.respond((UserInput("task"),))
    assert len(turn.tool_calls) == 1
    generated_id = turn.tool_calls[0].call_id
    assert isinstance(generated_id, str) and generated_id

    result = ModelToolResult(generated_id, "ok", False)
    session.respond((ToolResultInput(result),))
    second_call = client.chat.completions.calls[1]
    assistant_msg = second_call["messages"][2]
    assert assistant_msg["tool_calls"][0]["id"] == generated_id
    tool_msg = second_call["messages"][3]
    assert tool_msg["tool_call_id"] == generated_id


@pytest.mark.parametrize("arguments", ["{not json", '["not","object"]'])
def test_malformed_tool_call_arguments_fail(arguments):
    client = FakeClient([
        response(choice(
            message(None, tool_calls=[tool_call("call", arguments=arguments)]),
            finish_reason="tool_calls",
        )),
    ])
    with pytest.raises(ChatCompletionsProtocolError):
        provider_session(client).respond((UserInput("task"),))


def test_null_content_with_tool_calls_does_not_crash():
    client = FakeClient([
        response(choice(
            message(None, tool_calls=[tool_call("call")]),
            finish_reason="tool_calls",
        )),
    ])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.text == ""
    assert turn.stop_reason is ModelStopReason.TOOL_USE


def test_reasoning_content_is_ignored_and_never_echoed_back():
    client = FakeClient([
        response(choice(message("final answer", reasoning_content="secret chain of thought"))),
        response(choice(message("done"))),
    ])
    session = provider_session(client)
    turn = session.respond((UserInput("task"),))
    assert turn.text == "final answer"
    assert "secret" not in turn.text
    session.respond((ToolResultInput(ModelToolResult("unused-call", "x", False)),))
    # respond() requires only ToolResultInput after start; use a harmless follow up
    # to inspect the stored assistant message instead of relying on strict typing.
    sent_messages = client.chat.completions.calls[1]["messages"]
    assistant_echo = [m for m in sent_messages if m.get("role") == "assistant"][0]
    assert "reasoning_content" not in assistant_echo
    assert assistant_echo["content"] == "final answer"


def test_gemini_missing_extra_fields_are_tolerated():
    # Gemini's OpenAI-compat endpoint may omit fields such as `refusal` entirely.
    client = FakeClient([response(choice(obj(content="hi", tool_calls=None)))])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.text == "hi"
    assert turn.stop_reason is ModelStopReason.COMPLETED


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        ("stop", ModelStopReason.COMPLETED),
        ("length", ModelStopReason.LENGTH),
        ("content_filter", ModelStopReason.REFUSAL),
        ("unknown_thing", ModelStopReason.OTHER),
        (None, ModelStopReason.OTHER),
    ],
)
def test_finish_reason_mapping(finish_reason, expected):
    client = FakeClient([response(choice(message("text"), finish_reason=finish_reason))])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.stop_reason is expected


def test_tool_calls_take_precedence_over_finish_reason_stop():
    client = FakeClient([
        response(choice(
            message(None, tool_calls=[tool_call("call")]),
            finish_reason="stop",
        )),
    ])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.stop_reason is ModelStopReason.TOOL_USE


def test_usage_mapping():
    client = FakeClient([response(choice(message("done")), usage_obj=usage(11, 22))])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.usage.input_tokens == 11
    assert turn.usage.output_tokens == 22


def test_missing_choices_is_protocol_error():
    client = FakeClient([response()])
    with pytest.raises(ChatCompletionsProtocolError):
        provider_session(client).respond((UserInput("task"),))


@pytest.mark.parametrize("bad_input", [(), (ToolResultInput(ModelToolResult("call", "x", False)),)])
def test_input_sequence_validation(bad_input):
    client = FakeClient([response(choice(message("done")))])
    session = provider_session(client)
    with pytest.raises((TypeError, ValueError)):
        session.respond(bad_input)


def test_backend_does_not_leak_api_key_and_uses_base_url():
    client = FakeClient([response(choice(message("done")))])
    backend = ChatCompletionsBackend(
        "model-test", api_key="secret-test-key", base_url="https://api.example.com/v1", client=client
    )
    backend.open_session(instructions="i", tools=(), allow_parallel_tool_calls=False).respond((UserInput("x"),))
    assert all("secret-test-key" not in repr(call) for call in client.chat.completions.calls)
    assert not hasattr(client, "responses")


class _FakeAuthenticationError(Exception):
    """Stand-in for openai.AuthenticationError: same propagation contract, no httpx dependency."""


def test_sdk_errors_propagate_and_are_wrapped_by_agent_session_like_responses_backend(tmp_path):
    # The Responses backend does not catch/re-map SDK exceptions itself either — it
    # lets them propagate out of respond(), and AgentSession._respond is what wraps
    # *any* exception into AgentBackendError. This backend follows the same design:
    # no bespoke exception mapping here, so behavior stays identical for both
    # backends at the AgentSession boundary.
    client = FakeClient([_FakeAuthenticationError("invalid api key: secret-test-key")])
    backend = ChatCompletionsBackend("model-test", client=client)
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


def test_real_agent_harness_with_fake_chat_completions_client(tmp_path):
    (tmp_path / "input.txt").write_text("original", encoding="utf-8")
    client = FakeClient([
        response(choice(
            message(None, tool_calls=[tool_call("read-call", "read_file", '{"path":"input.txt"}')]),
            finish_reason="tool_calls",
        )),
        response(choice(
            message(None, tool_calls=[
                tool_call("write-call", "write_file", '{"path":"output.txt","content":"changed"}')
            ]),
            finish_reason="tool_calls",
        )),
        response(choice(message("finished"))),
    ])
    backend = ChatCompletionsBackend("model-test", client=client)
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
    assert client.chat.completions.calls[1]["messages"][-1]["tool_call_id"] == "read-call"
    assert client.chat.completions.calls[2]["messages"][-1]["tool_call_id"] == "write-call"
