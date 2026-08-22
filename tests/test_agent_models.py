import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import (  # noqa: E402
    AgentLimits,
    ApprovalDecision,
    ModelStopReason,
    ModelToolCall,
    ModelToolDefinition,
    ModelToolResult,
    ModelTurn,
    ModelUsage,
    ToolResultInput,
    UserInput,
)
from agent_runtime.errors import AgentInputError  # noqa: E402


def test_model_tool_call_defensive_strict_object_copy():
    arguments = {"value": "before"}
    call = ModelToolCall("call-1", "echo", arguments)
    arguments["value"] = "after"
    assert call.arguments == {"value": "before"}


@pytest.mark.parametrize("value", [True, -1, 1.5])
def test_usage_counts_require_nonnegative_ints(value):
    with pytest.raises(AgentInputError):
        ModelUsage(input_tokens=value)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_usage_cost_must_be_finite(value):
    with pytest.raises(AgentInputError):
        ModelUsage(cost_usd=value)


def test_model_tool_definition_is_plain_json_compatible():
    definition = ModelToolDefinition(
        "echo",
        "Echo",
        {"type": "object", "properties": {"value": {"type": "string"}}},
    )
    assert type(definition.input_schema) is dict
    assert type(definition.input_schema["properties"]) is dict
    definition.input_schema["properties"]["value"]["type"] = "number"
    assert definition.input_schema["properties"]["value"]["type"] == "number"


def test_model_turn_is_immutable_at_outer_collection_and_has_no_hidden_reasoning():
    turn = ModelTurn(
        "visible",
        [ModelToolCall("call-1", "echo", {})],
        ModelStopReason.TOOL_USE,
        ModelUsage(1, 2),
    )
    assert isinstance(turn.tool_calls, tuple)
    assert "chain_of_thought" not in ModelTurn.__dataclass_fields__
    assert "thoughts" not in ModelTurn.__dataclass_fields__


def test_input_items_and_result_validate_contract():
    assert UserInput("task").text == "task"
    metadata = {"truncated": True, "nested": {"value": 1}}
    result = ModelToolResult("call-1", "ok", False, metadata)
    metadata["nested"]["value"] = 2
    assert result.metadata["nested"]["value"] == 1
    assert ToolResultInput(result).result is result
    with pytest.raises(AgentInputError):
        ModelToolResult("call-1", "ok", 0)  # type: ignore[arg-type]
    with pytest.raises(AgentInputError):
        ModelToolResult("call-1", "ok", False, {"bad": float("nan")})
    with pytest.raises(AgentInputError):
        ModelToolResult("call-1", "ok", False, {1: "bad"})


def test_limits_require_positive_non_bool_ints():
    assert AgentLimits() == AgentLimits(20, 50, 5)
    with pytest.raises(AgentInputError):
        AgentLimits(max_model_turns=True)
    with pytest.raises(AgentInputError):
        AgentLimits(max_tool_calls=0)


def test_approval_decision_requires_session_binding():
    with pytest.raises(AgentInputError):
        ApprovalDecision("call-1", "fp", True, "")
