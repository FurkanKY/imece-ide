import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import (  # noqa: E402
    AgentSession,
    ModelStopReason,
    ModelToolCall,
    ModelToolDefinition,
    ModelToolResult,
    ToolResultInput,
    UserInput,
)
from agent_runtime.providers.openai_responses import (  # noqa: E402
    OpenAIResponsesBackend,
    OpenAIResponsesProtocolError,
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


def response(response_id, output, status="completed", **extra):
    return obj(id=response_id, output=output, status=status, **extra)


def message(*texts):
    return obj(
        type="message",
        content=[obj(type="output_text", text=text) for text in texts],
    )


def function_call(call_id, name="read_file", arguments='{"path":"a.py"}'):
    return obj(type="function_call", call_id=call_id, name=name, arguments=arguments)


class FakeResponses:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("fake response script exhausted")
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.responses = FakeResponses(responses)


def tool_definition(name="read_file"):
    return ModelToolDefinition(name, f"{name} description", SCHEMA)


def provider_session(client, tools=(tool_definition(),), **kwargs):
    backend = OpenAIResponsesBackend("gpt-test", client=client, **kwargs)
    return backend.open_session(
        instructions="system prompt",
        tools=tuple(tools),
        allow_parallel_tool_calls=False,
    )


def test_request_construction_and_response_id_continuation():
    client = FakeClient([
        response("resp_1", [message("first")]),
        response("resp_2", [message("second")]),
        response("resp_3", [message("third")]),
    ])
    session = provider_session(client, max_output_tokens=123, reasoning_effort="low")
    assert session.respond((UserInput("task\nexact"),)).text == "first"
    session.respond((ToolResultInput(ModelToolResult("call", "output", False, {"x": 1})),))
    session.respond((ToolResultInput(ModelToolResult("call-2", "output", True)),))

    assert len(client.responses.calls) == 3
    first, second, third = client.responses.calls
    assert first["model"] == "gpt-test"
    assert first["instructions"] == second["instructions"] == third["instructions"] == "system prompt"
    assert "previous_response_id" not in first
    assert second["previous_response_id"] == "resp_1"
    assert third["previous_response_id"] == "resp_2"
    assert all(call["parallel_tool_calls"] is False for call in client.responses.calls)
    assert all(call["tools"] == first["tools"] for call in client.responses.calls)
    assert first["max_output_tokens"] == 123
    assert first["reasoning"] == {"effort": "low"}
    assert first["input"] == [{"role": "user", "content": "task\nexact"}]
    assert second["input"][0]["type"] == "function_call_output"
    assert json.loads(second["input"][0]["output"]) == {
        "ok": True,
        "content": "output",
        "metadata": {"x": 1},
    }
    assert second["input"][0]["call_id"] == "call"
    assert json.loads(third["input"][0]["output"])["ok"] is False


def test_tools_preserve_order_plain_schema_and_strict_false():
    first = tool_definition("first")
    second = tool_definition("second")
    client = FakeClient([response("resp", [message("done")])])
    session = provider_session(client, (first, second))
    session.respond((UserInput("task"),))
    tools = client.responses.calls[0]["tools"]
    assert [tool["name"] for tool in tools] == ["first", "second"]
    assert all(tool["strict"] is False for tool in tools)
    assert all(type(tool["parameters"]) is dict for tool in tools)
    tools[0]["parameters"]["properties"]["path"]["type"] = "number"
    assert first.input_schema["properties"]["path"]["type"] == "string"
    assert session is not None


def test_tool_result_envelope_preserves_metadata_and_error_flag():
    metadata = {"truncated": True, "next_line": 301}
    result = ModelToolResult("call_123", "...", False, metadata)
    metadata["truncated"] = False
    client = FakeClient([response("resp", [message("need result")]), response("resp-2", [message("done")])])
    session = provider_session(client)
    session.respond((UserInput("task"),))
    session.respond((ToolResultInput(result),))
    payload = client.responses.calls[1]["input"][0]
    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == "call_123"
    assert json.loads(payload["output"]) == {
        "ok": True,
        "content": "...",
        "metadata": {"truncated": True, "next_line": 301},
    }
    error_client = FakeClient([response("r1", [message("x")]), response("r2", [message("y")])])
    error_session = provider_session(error_client)
    error_session.respond((UserInput("task"),))
    error_session.respond((ToolResultInput(ModelToolResult("error", "bad", True)),))
    assert json.loads(error_client.responses.calls[1]["input"][0]["output"])["ok"] is False


def test_function_call_parsing_and_order():
    client = FakeClient([response("resp", [
        function_call("call_a", arguments='{"path":"a.py"}'),
        function_call("call_b", arguments='{"path":"b.py"}'),
    ], status="completed")])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.stop_reason is ModelStopReason.TOOL_USE
    assert [(call.call_id, call.name, call.arguments) for call in turn.tool_calls] == [
        ("call_a", "read_file", {"path": "a.py"}),
        ("call_b", "read_file", {"path": "b.py"}),
    ]


@pytest.mark.parametrize("arguments", ["{not json", '["not","object"]'])
def test_malformed_function_arguments_fail(arguments):
    client = FakeClient([response("resp", [function_call("call", arguments=arguments)])])
    with pytest.raises(OpenAIResponsesProtocolError):
        provider_session(client).respond((UserInput("task"),))


def test_text_mixed_output_and_reasoning_are_parsed_correctly():
    client = FakeClient([response("resp", [
        obj(type="reasoning", summary=[obj(type="summary_text", text="hidden")]),
        message("visible ", "text"),
        function_call("call"),
    ])])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.text == "visible text"
    assert turn.stop_reason is ModelStopReason.TOOL_USE
    assert "hidden" not in turn.text


@pytest.mark.parametrize(
    ("provider_response", "expected"),
    [
        (response("completed", [message("done")]), ModelStopReason.COMPLETED),
        (response("length", [], "incomplete", incomplete_details=obj(reason="max_output_tokens")), ModelStopReason.LENGTH),
        (response("refusal", [obj(type="refusal", refusal="no")]), ModelStopReason.REFUSAL),
        (response("failed", [], "failed"), ModelStopReason.ERROR),
        (response("unknown", [], "queued"), ModelStopReason.OTHER),
    ],
)
def test_stop_reason_mapping(provider_response, expected):
    client = FakeClient([provider_response])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.stop_reason is expected


def test_function_call_precedes_completed_status():
    client = FakeClient([response("resp", [function_call("call")], status="completed")])
    turn = provider_session(client).respond((UserInput("task"),))
    assert turn.stop_reason is ModelStopReason.TOOL_USE


def test_missing_response_id_is_protocol_failure():
    client = FakeClient([response("", [message("done")])])
    with pytest.raises(OpenAIResponsesProtocolError):
        provider_session(client).respond((UserInput("task"),))


@pytest.mark.parametrize("bad_input", [(), (ToolResultInput(ModelToolResult("call", "x", False)),)])
def test_input_sequence_validation(bad_input):
    client = FakeClient([response("resp", [message("done")])])
    session = provider_session(client)
    with pytest.raises((TypeError, ValueError)):
        session.respond(bad_input)


def test_backend_does_not_use_chat_completions_and_api_key_not_in_payload():
    client = FakeClient([response("resp", [message("done")])])
    backend = OpenAIResponsesBackend("gpt-test", api_key="secret-test-key", client=client)
    backend.open_session(instructions="i", tools=(), allow_parallel_tool_calls=False).respond((UserInput("x"),))
    assert all("secret-test-key" not in repr(call) for call in client.responses.calls)
    assert not hasattr(client, "chat")


def test_real_agent_harness_with_fake_openai_client(tmp_path):
    (tmp_path / "input.txt").write_text("original", encoding="utf-8")
    client = FakeClient([
        response("resp_1", [function_call("read-call", "read_file", '{"path":"input.txt"}')]),
        response("resp_2", [function_call("write-call", "write_file", '{"path":"output.txt","content":"changed"}')]),
        response("resp_3", [message("finished")]),
    ])
    backend = OpenAIResponsesBackend("gpt-test", client=client)
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
    assert [call.get("previous_response_id") for call in client.responses.calls] == [None, "resp_1", "resp_2"]
    assert client.responses.calls[1]["input"][0]["call_id"] == "read-call"
    assert client.responses.calls[2]["input"][0]["call_id"] == "write-call"
