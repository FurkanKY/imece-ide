import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_runtime import (
    PermissionRequest,
    ToolAnnotations,
    ToolExecutionContext,
    ToolObservation,
    ToolRegistry,
    ToolSpec,
)
from tool_runtime.errors import ToolNotFoundError, ToolRegistrationError


SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}


class FakeExecutor:
    def execute(self, arguments, context):
        return ToolObservation(content=arguments["value"], metadata={})


def resolver(arguments, context):
    return [PermissionRequest("read", f"value/{arguments['value']}")]


def spec(name="fake"):
    return ToolSpec(name, "A fake tool", SCHEMA, ToolAnnotations(read_only=True), resolver)


def test_register_and_get_and_deterministic_order():
    registry = ToolRegistry()
    registry.register(spec("first"), FakeExecutor())
    registry.register(spec("second"), FakeExecutor())
    assert registry.get("first").name == "first"
    assert [item.name for item in registry.list_specs()] == ["first", "second"]


def test_duplicate_rejected_and_unknown_is_typed():
    registry = ToolRegistry()
    registry.register(spec(), FakeExecutor())
    with pytest.raises(ToolRegistrationError, match="zaten kayıtlı"):
        registry.register(spec(), FakeExecutor())
    with pytest.raises(ToolNotFoundError):
        registry.get("missing")


@pytest.mark.parametrize("name", ["", "   "])
def test_invalid_name_rejected(name):
    with pytest.raises(ToolRegistrationError):
        spec(name)


@pytest.mark.parametrize("description", ["", "   "])
def test_invalid_description_rejected(description):
    with pytest.raises(ToolRegistrationError):
        ToolSpec("fake", description, SCHEMA, ToolAnnotations(), resolver)


def test_invalid_and_non_object_schema_rejected():
    with pytest.raises(ToolRegistrationError, match="Geçersiz JSON Schema"):
        ToolSpec("bad", "bad", {"type": "object", "properties": {"x": {"type": "not-a-type"}}}, ToolAnnotations(), resolver)
    with pytest.raises(ToolRegistrationError, match="type='object'"):
        ToolSpec("bad", "bad", {"type": "string"}, ToolAnnotations(), resolver)


def test_returned_specs_cannot_mutate_registry_internals():
    registry = ToolRegistry()
    registry.register(spec(), FakeExecutor())
    specs = registry.list_specs()
    with pytest.raises(TypeError):
        specs[0].input_schema["properties"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        specs[0].input_schema["properties"]["value"]["type"] = "number"  # type: ignore[index]
    assert registry.get("fake").input_schema["properties"]["value"]["type"] == "string"
