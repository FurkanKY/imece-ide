import sys
from itertools import count
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import (  # noqa: E402
    AgentLimits,
    AgentSession,
    AgentSessionState,
    ApprovalDecision,
    ModelStopReason,
    ModelToolCall,
    ModelToolResult,
    ModelTurn,
    ModelUsage,
    ToolResultInput,
    UserInput,
)
from agent_runtime.errors import (  # noqa: E402
    AgentApprovalError,
    AgentBackendError,
    AgentIncompleteError,
    AgentLifecycleError,
    AgentLimitError,
    AgentProtocolError,
    AgentRefusalError,
    AgentToolRuntimeError,
)
from tool_runtime import (  # noqa: E402
    PermissionEffect,
    PermissionRequest,
    PermissionRule,
    PolicyEvaluator,
    ToolAnnotations,
    ToolExecutionContext,
    ToolObservation,
    ToolRegistry,
    ToolSpec,
)
from tool_runtime.errors import (  # noqa: E402
    ToolApprovalRequiredError,
    ToolCallConsumedError,
    ToolPreparedCallError,
)
from tool_runtime.tools.workspace_files import register_workspace_tools  # noqa: E402
from workspace.local import LocalWorkspace  # noqa: E402


SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}
_IDS = count(1)


def turn(text="", calls=(), reason=ModelStopReason.COMPLETED, input_tokens=1, output_tokens=1):
    return ModelTurn(text, tuple(calls), reason, ModelUsage(input_tokens, output_tokens))


class ScriptedSession:
    def __init__(self, turns):
        self.turns = list(turns)
        self.received = []

    def respond(self, input_items):
        self.received.append(input_items)
        if not self.turns:
            raise AssertionError("scripted backend exhausted")
        return self.turns.pop(0)


class ScriptedBackend:
    def __init__(self, turns, mutate_tools=False, open_error=None, respond_error=None):
        self.session = ScriptedSession(turns)
        self.mutate_tools = mutate_tools
        self.open_error = open_error
        self.respond_error = respond_error
        self.open_args = None

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        if self.open_error:
            raise self.open_error
        self.open_args = (instructions, tools, allow_parallel_tool_calls)
        if self.mutate_tools and tools:
            tools[0].input_schema["properties"]["value"]["type"] = "number"
        if self.respond_error:
            self.session.respond = lambda _items: (_ for _ in ()).throw(self.respond_error)
        return self.session


class EchoExecutor:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def execute(self, arguments, context):
        self.calls.append(arguments)
        if self.error:
            raise self.error
        return ToolObservation(f"echo:{arguments['value']}", {"value": arguments["value"]})


def fake_registry(executor=None):
    registry = ToolRegistry()
    executor = executor or EchoExecutor()
    registry.register(
        ToolSpec(
            "echo", "Echo a value", SCHEMA, ToolAnnotations(read_only=True, idempotent=True),
            lambda arguments, context: [PermissionRequest("read", "echo")],
        ),
        executor,
    )
    return registry, executor


def session_with(backend, registry, policy=None, workspace=None, limits=None):
    workspace = workspace or LocalWorkspace(Path.cwd())
    policy = policy or PolicyEvaluator([PermissionRule("*", "*", PermissionEffect.ALLOW)])
    return AgentSession(
        backend=backend,
        registry=registry,
        policy=policy,
        context=ToolExecutionContext(workspace=workspace),
        instructions="system instructions",
        limits=limits,
    )


def tool_call(call_id="tool-1", name="echo", value="x"):
    return ModelToolCall(call_id, name, {"value": value})


def test_text_only_loop_and_first_input():
    registry, _ = fake_registry()
    backend = ScriptedBackend([turn("final answer")])
    session = session_with(backend, registry)
    outcome = session.start("do the task")
    assert outcome.final_text == "final answer"
    assert backend.open_args[2] is False
    assert backend.session.received == [(UserInput("do the task"),)]
    assert outcome.model_turns == 1
    assert session.state is AgentSessionState.COMPLETED


def test_tool_result_continuation_preserves_call_id_and_order():
    registry, executor = fake_registry()
    backend = ScriptedBackend([
        turn(calls=[tool_call("a", value="one"), tool_call("b", value="two")], reason=ModelStopReason.TOOL_USE),
        turn("finished"),
    ])
    outcome = session_with(backend, registry).start("task")
    assert outcome.final_text == "finished"
    assert [args["value"] for args in executor.calls] == ["one", "two"]
    results = backend.session.received[1]
    assert [item.result.call_id for item in results] == ["a", "b"]
    assert all(isinstance(item, ToolResultInput) and not item.result.is_error for item in results)


def test_tool_observation_metadata_reaches_model_result():
    registry, _ = fake_registry()
    backend = ScriptedBackend([
        turn(calls=[tool_call("meta")], reason=ModelStopReason.TOOL_USE),
        turn("done"),
    ])
    session_with(backend, registry).start("task")
    assert backend.session.received[1][0].result.metadata == {"value": "x"}


def test_tool_definitions_are_ordered_plain_copies_and_cannot_mutate_specs():
    registry, _ = fake_registry()
    backend = ScriptedBackend([turn("done")], mutate_tools=True)
    session_with(backend, registry).start("task")
    assert [tool.name for tool in backend.open_args[1]] == ["echo"]
    assert type(backend.open_args[1][0].input_schema) is dict
    assert registry.get("echo").input_schema["properties"]["value"]["type"] == "string"


def test_two_tool_definitions_preserve_registry_order():
    registry, executor = fake_registry()
    registry.register(
        ToolSpec(
            "second", "Second echo", SCHEMA, ToolAnnotations(read_only=True, idempotent=True),
            lambda arguments, context: [PermissionRequest("read", "second")],
        ),
        executor,
    )
    backend = ScriptedBackend([turn("done")])
    session_with(backend, registry).start("task")
    assert [tool.name for tool in backend.open_args[1]] == ["echo", "second"]


def test_expected_tool_errors_are_returned_and_model_can_recover():
    registry, _ = fake_registry()
    backend = ScriptedBackend([
        turn(calls=[ModelToolCall("bad", "missing", {})], reason=ModelStopReason.TOOL_USE),
        turn("recovered"),
    ])
    session = session_with(backend, registry)
    outcome = session.start("task")
    result = backend.session.received[1][0].result
    assert result.is_error is True
    assert "Traceback" not in result.content
    assert outcome.final_text == "recovered"
    assert outcome.tool_errors == 1


def test_schema_denial_and_execution_failures_are_recoverable():
    executor = EchoExecutor(error=ValueError("executor boom"))
    registry, _ = fake_registry(executor)
    policy = PolicyEvaluator([
        PermissionRule("read", "*", PermissionEffect.DENY),
    ])
    backend = ScriptedBackend([
        turn(calls=[tool_call("schema", value=1)], reason=ModelStopReason.TOOL_USE),
        turn(calls=[tool_call("denied", value="x")], reason=ModelStopReason.TOOL_USE),
        turn(calls=[tool_call("failed", value="x")], reason=ModelStopReason.TOOL_USE),
        turn("recovered"),
    ])
    session = session_with(backend, registry, policy)
    outcome = session.start("task")
    assert outcome.tool_errors == 3
    assert len(executor.calls) == 0
    assert all(item.result.is_error for batch in backend.session.received[1:4] for item in batch)


def test_executor_failure_is_returned_as_recoverable_tool_error():
    executor = EchoExecutor(error=ValueError("executor boom"))
    registry, _ = fake_registry(executor)
    backend = ScriptedBackend([
        turn(calls=[tool_call("failed")], reason=ModelStopReason.TOOL_USE),
        turn("recovered"),
    ])
    outcome = session_with(backend, registry).start("task")
    assert outcome.final_text == "recovered"
    assert outcome.tool_errors == 1
    assert len(executor.calls) == 1
    result = backend.session.received[1][0].result
    assert result.is_error is True
    assert "executor boom" in result.content


def test_usage_is_aggregated_across_model_turns():
    registry, _ = fake_registry()
    backend = ScriptedBackend([
        ModelTurn(
            "",
            (tool_call("tool"),),
            ModelStopReason.TOOL_USE,
            ModelUsage(2, 3, 1.25),
        ),
        ModelTurn("done", (), ModelStopReason.COMPLETED, ModelUsage(4, 5, 2.75)),
    ])
    outcome = session_with(backend, registry).start("task")
    assert outcome.input_tokens == 6
    assert outcome.output_tokens == 8
    assert outcome.cost_usd == 4.0


def test_success_resets_consecutive_error_counter():
    registry, _ = fake_registry()
    backend = ScriptedBackend([
        turn(calls=[ModelToolCall("bad", "missing", {})], reason=ModelStopReason.TOOL_USE),
        turn(calls=[tool_call("ok")], reason=ModelStopReason.TOOL_USE),
        turn(calls=[ModelToolCall("bad2", "missing", {})], reason=ModelStopReason.TOOL_USE),
        turn("done"),
    ])
    outcome = session_with(backend, registry, limits=AgentLimits(max_consecutive_tool_errors=1)).start("task")
    assert outcome.final_text == "done"


def test_consecutive_error_limit_stops_run():
    registry, _ = fake_registry()
    backend = ScriptedBackend([
        turn(calls=[ModelToolCall("bad", "missing", {})], reason=ModelStopReason.TOOL_USE),
        turn(calls=[ModelToolCall("bad2", "missing", {})], reason=ModelStopReason.TOOL_USE),
    ])
    with pytest.raises(AgentLimitError):
        session_with(backend, registry, limits=AgentLimits(max_consecutive_tool_errors=1)).start("task")


def _approval_setup(tmp_path, turns, rules=None):
    workspace = LocalWorkspace(tmp_path)
    registry = ToolRegistry()
    register_workspace_tools(registry)
    policy = PolicyEvaluator(rules or [
        PermissionRule("read", "*", PermissionEffect.ALLOW),
        PermissionRule("list", "*", PermissionEffect.ALLOW),
        PermissionRule("search", "*", PermissionEffect.ALLOW),
        PermissionRule("edit", "*", PermissionEffect.ASK),
        PermissionRule("delete", "*", PermissionEffect.DENY),
    ])
    backend = ScriptedBackend(turns)
    return session_with(backend, registry, policy, workspace), backend, workspace


def _decision(pause, approved):
    return ApprovalDecision(pause.call_id, pause.approval_fingerprint, approved, pause.session_id)


def test_approval_pause_approve_executes_once_and_continues(tmp_path):
    backend_turns = [
        turn(calls=[ModelToolCall("write-1", "write_file", {"path": "x.txt", "content": "x"})], reason=ModelStopReason.TOOL_USE),
        turn("written"),
    ]
    session, backend, workspace = _approval_setup(tmp_path, backend_turns)
    pause = session.start("write")
    assert pause.call_id == "write-1"
    assert not (tmp_path / "x.txt").exists()
    outcome = session.resume(_decision(pause, True))
    assert outcome.final_text == "written"
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "x"
    result = backend.session.received[1][0].result
    assert result.call_id == "write-1" and result.is_error is False
    assert outcome.tool_calls == 1


def test_read_truncation_metadata_reaches_model(tmp_path):
    (tmp_path / "long.txt").write_text("".join(f"line {n}\n" for n in range(1, 302)), encoding="utf-8")
    session, backend, _ = _approval_setup(tmp_path, [
        turn(calls=[ModelToolCall("read-1", "read_file", {"path": "long.txt", "limit": 300})], reason=ModelStopReason.TOOL_USE),
        turn("done"),
    ])
    outcome = session.start("read")
    result = backend.session.received[1][0].result
    assert outcome.final_text == "done"
    assert result.metadata["truncated"] is True
    assert result.metadata["next_line"] == 301


def test_approval_denial_is_model_error_and_does_not_mutate(tmp_path):
    session, backend, workspace = _approval_setup(tmp_path, [
        turn(calls=[ModelToolCall("write-1", "write_file", {"path": "x.txt", "content": "x"})], reason=ModelStopReason.TOOL_USE),
        turn("denied"),
    ])
    pause = session.start("write")
    outcome = session.resume(_decision(pause, False))
    assert outcome.final_text == "denied"
    assert not (tmp_path / "x.txt").exists()
    assert backend.session.received[1][0].result.is_error is True


def test_wrong_approval_keeps_session_paused_and_stale_resume_fails(tmp_path):
    session, _, _ = _approval_setup(tmp_path, [
        turn(calls=[ModelToolCall("write-1", "write_file", {"path": "x.txt", "content": "x"})], reason=ModelStopReason.TOOL_USE),
        turn("done"),
    ])
    pause = session.start("write")
    with pytest.raises(AgentApprovalError):
        session.resume(ApprovalDecision("wrong", pause.approval_fingerprint, True, pause.session_id))
    assert session.state is AgentSessionState.WAITING_APPROVAL
    with pytest.raises(AgentApprovalError):
        session.resume(ApprovalDecision(pause.call_id, "wrong", True, pause.session_id))
    session.resume(_decision(pause, False))
    with pytest.raises(AgentLifecycleError):
        session.resume(_decision(pause, True))


def test_multi_call_approval_preserves_earlier_results(tmp_path):
    session, backend, _ = _approval_setup(tmp_path, [
        turn(calls=[
            ModelToolCall("echo", "write_file", {"path": "a.txt", "content": "a"}),
            ModelToolCall("echo2", "write_file", {"path": "b.txt", "content": "b"}),
        ], reason=ModelStopReason.TOOL_USE),
        turn("done"),
    ], rules=[
        PermissionRule("edit", "a.txt", PermissionEffect.ALLOW),
    ])
    first_pause = session.start("write twice")
    assert first_pause.call_id == "echo2"
    # The first call completed; pause occurs on the second call.
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "a"
    outcome = session.resume(_decision(first_pause, False))
    assert outcome.final_text == "done"
    assert [item.result.call_id for item in backend.session.received[1]] == ["echo", "echo2"]
    assert backend.session.received[1][0].result.is_error is False
    assert backend.session.received[1][1].result.is_error is True


def _three_write_turns():
    return [
        turn(calls=[
            ModelToolCall("a", "write_file", {"path": "a.txt", "content": "a"}),
            ModelToolCall("b", "write_file", {"path": "b.txt", "content": "b"}),
            ModelToolCall("c", "write_file", {"path": "c.txt", "content": "c"}),
        ], reason=ModelStopReason.TOOL_USE),
        turn("done"),
    ]


def _three_write_rules():
    return [
        PermissionRule("edit", "a.txt", PermissionEffect.ALLOW),
        PermissionRule("edit", "b.txt", PermissionEffect.ASK),
        PermissionRule("edit", "c.txt", PermissionEffect.ALLOW),
    ]


def test_three_call_approval_resume_prepares_and_executes_following_call(tmp_path):
    session, backend, _ = _approval_setup(tmp_path, _three_write_turns(), _three_write_rules())
    first_pause = session.start("write three")
    assert first_pause.call_id == "b"
    second_result = session.resume(_decision(first_pause, True))
    assert second_result.final_text == "done"
    assert [item.result.call_id for item in backend.session.received[1]] == ["a", "b", "c"]
    assert all(not item.result.is_error for item in backend.session.received[1])
    assert [path.read_text(encoding="utf-8") for path in (tmp_path / "a.txt", tmp_path / "b.txt", tmp_path / "c.txt")] == ["a", "b", "c"]
    assert second_result.tool_calls == 3


def test_three_call_approval_denial_allows_following_call(tmp_path):
    session, backend, _ = _approval_setup(tmp_path, _three_write_turns(), _three_write_rules())
    pause = session.start("write three")
    outcome = session.resume(_decision(pause, False))
    results = backend.session.received[1]
    assert outcome.final_text == "done"
    assert [item.result.call_id for item in results] == ["a", "b", "c"]
    assert results[0].result.is_error is False
    assert results[1].result.is_error is True
    assert results[2].result.is_error is False
    assert (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()
    assert (tmp_path / "c.txt").exists()


def test_second_approval_pause_is_reached_after_first_resume(tmp_path):
    rules = [
        PermissionRule("edit", "b.txt", PermissionEffect.ASK),
        PermissionRule("edit", "c.txt", PermissionEffect.ASK),
    ]
    session, _, _ = _approval_setup(tmp_path, [
        turn(calls=[
            ModelToolCall("b", "write_file", {"path": "b.txt", "content": "b"}),
            ModelToolCall("c", "write_file", {"path": "c.txt", "content": "c"}),
        ], reason=ModelStopReason.TOOL_USE),
        turn("done"),
    ], rules)
    first_pause = session.start("write twice")
    second_pause = session.resume(_decision(first_pause, True))
    assert second_pause.call_id == "c"
    assert session.state is AgentSessionState.WAITING_APPROVAL
    outcome = session.resume(_decision(second_pause, False))
    assert outcome.final_text == "done"


def test_internal_dispatcher_errors_are_not_sent_to_model():
    registry, _ = fake_registry()
    backend = ScriptedBackend([turn(calls=[tool_call("same")], reason=ModelStopReason.TOOL_USE)])
    session = session_with(backend, registry)
    session._dispatcher.execute = lambda *_args: (_ for _ in ()).throw(ToolCallConsumedError("bug"))
    with pytest.raises(AgentToolRuntimeError):
        session.start("task")
    assert len(backend.session.received) == 1


def test_approved_resume_reapproval_is_internal_failure(tmp_path):
    session, _, _ = _approval_setup(tmp_path, [
        turn(calls=[ModelToolCall("write-1", "write_file", {"path": "x.txt", "content": "x"})], reason=ModelStopReason.TOOL_USE),
    ])
    pause = session.start("write")
    session.dispatcher.execute = lambda *_args: (_ for _ in ()).throw(
        ToolApprovalRequiredError("unexpected second approval")
    )
    with pytest.raises(AgentToolRuntimeError):
        session.resume(_decision(pause, True))
    assert session.state is AgentSessionState.FAILED


@pytest.mark.parametrize("invalid_session", [None, object()])
def test_invalid_backend_session_fails_before_running(invalid_session):
    class InvalidBackend:
        def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
            return invalid_session

    registry, _ = fake_registry()
    session = session_with(InvalidBackend(), registry)
    with pytest.raises(AgentProtocolError):
        session.start("task")
    assert session.state is AgentSessionState.FAILED


def test_whitespace_only_completion_is_protocol_error():
    registry, _ = fake_registry()
    with pytest.raises(AgentProtocolError):
        session_with(ScriptedBackend([turn(" \n\t")]), registry).start("task")


def test_incomplete_cost_accounting_is_unknown():
    registry, _ = fake_registry()
    for usages in (
        (ModelUsage(1, 1, None), ModelUsage(1, 1, 0.1)),
        (ModelUsage(1, 1, 0.1), ModelUsage(1, 1, None)),
        (ModelUsage(1, 1, None), ModelUsage(1, 1, None)),
    ):
        backend = ScriptedBackend([
            ModelTurn("", (tool_call("call"),), ModelStopReason.TOOL_USE, usages[0]),
            ModelTurn("done", (), ModelStopReason.COMPLETED, usages[1]),
        ])
        outcome = session_with(backend, registry).start("task")
        assert outcome.cost_usd is None

    backend = ScriptedBackend([turn(calls=[tool_call("same"), tool_call("same")], reason=ModelStopReason.TOOL_USE)])
    session = session_with(backend, registry)
    with pytest.raises(AgentToolRuntimeError) as error:
        session.start("task")
    assert isinstance(error.value.__cause__, ToolPreparedCallError)
    assert len(backend.session.received) == 1


def test_limits_and_stop_reasons():
    registry, _ = fake_registry()
    backend = ScriptedBackend([
        turn(calls=[tool_call()], reason=ModelStopReason.TOOL_USE),
        turn("never reached"),
    ])
    with pytest.raises(AgentLimitError):
        session_with(backend, registry, limits=AgentLimits(max_model_turns=1)).start("task")

    backend = ScriptedBackend([turn(calls=[tool_call("a"), tool_call("b")], reason=ModelStopReason.TOOL_USE)])
    with pytest.raises(AgentLimitError):
        session_with(backend, registry, limits=AgentLimits(max_tool_calls=1)).start("task")

    for reason, error in ((ModelStopReason.LENGTH, AgentIncompleteError), (ModelStopReason.REFUSAL, AgentRefusalError)):
        with pytest.raises(error):
            session_with(ScriptedBackend([turn("", reason=reason)]), registry).start("task")
    for invalid in (turn(""), turn("", reason=ModelStopReason.TOOL_USE)):
        with pytest.raises(AgentProtocolError):
            session_with(ScriptedBackend([invalid]), registry).start("task")


def test_backend_failures_preserve_cause_and_lifecycle_errors():
    registry, _ = fake_registry()
    with pytest.raises(AgentBackendError) as error:
        session_with(ScriptedBackend([], open_error=ValueError("open")), registry).start("task")
    assert isinstance(error.value.__cause__, ValueError)
    backend = ScriptedBackend([turn("done")], respond_error=OSError("transport"))
    with pytest.raises(AgentBackendError) as error:
        session_with(backend, registry).start("task")
    assert isinstance(error.value.__cause__, OSError)
    completed = session_with(ScriptedBackend([turn("done")]), registry)
    completed.start("task")
    with pytest.raises(AgentLifecycleError):
        completed.start("again")
    with pytest.raises(AgentLifecycleError):
        completed.resume(ApprovalDecision("x", "y", True, "z"))


def test_sessions_have_independent_dispatchers_and_approval_tokens(tmp_path):
    turns = [
        turn(calls=[ModelToolCall("same", "write_file", {"path": "x.txt", "content": "x"})], reason=ModelStopReason.TOOL_USE),
        turn("done"),
    ]
    session_a, _, _ = _approval_setup(tmp_path, list(turns))
    session_b, _, _ = _approval_setup(tmp_path, list(turns))
    pause_a = session_a.start("task")
    pause_b = session_b.start("task")
    assert session_a.dispatcher is not session_b.dispatcher
    with pytest.raises(AgentApprovalError):
        session_b.resume(_decision(pause_a, True))
    assert session_b.state is AgentSessionState.WAITING_APPROVAL
    session_a.resume(_decision(pause_a, False))
    assert session_a.state is AgentSessionState.COMPLETED
    assert session_b.state is AgentSessionState.WAITING_APPROVAL
    session_b.resume(_decision(pause_b, False))
