import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from planner_runtime.errors import PlannerProtocolError  # noqa: E402
from planner_runtime.models import MAX_PLAN_STEPS, TaskComplexity, TaskScope  # noqa: E402
from planner_runtime.parser import MAX_MODEL_OUTPUT_CHARS, parse_plan_decision  # noqa: E402


def _valid_payload(**overrides):
    payload = {
        "summary": "Implement the feature.",
        "steps": [{"title": "Step one", "objective": "Do the first part."}],
        "acceptance_criteria": ["existing tests remain passing"],
        "risks": ["may affect performance"],
        "task_profile": {"complexity": "LOW", "scope": "LOCAL"},
    }
    payload.update(overrides)
    return payload


def _valid_text(**overrides):
    return json.dumps(_valid_payload(**overrides))


# ---------------- valid ----------------


def test_valid_complete_json():
    decision = parse_plan_decision(_valid_text())
    assert decision.summary == "Implement the feature."
    assert len(decision.steps) == 1
    assert decision.steps[0].title == "Step one"
    assert decision.acceptance_criteria == ("existing tests remain passing",)
    assert decision.risks == ("may affect performance",)
    assert decision.task_profile.complexity is TaskComplexity.LOW
    assert decision.task_profile.scope is TaskScope.LOCAL


@pytest.mark.parametrize("complexity", ["LOW", "MEDIUM", "HIGH"])
def test_valid_complexity_values(complexity):
    decision = parse_plan_decision(_valid_text(task_profile={"complexity": complexity, "scope": "LOCAL"}))
    assert decision.task_profile.complexity.value == complexity


@pytest.mark.parametrize("scope", ["LOCAL", "MULTI_AREA", "REPOSITORY_WIDE"])
def test_valid_scope_values(scope):
    decision = parse_plan_decision(_valid_text(task_profile={"complexity": "LOW", "scope": scope}))
    assert decision.task_profile.scope.value == scope


def test_valid_empty_acceptance_criteria_and_risks():
    decision = parse_plan_decision(_valid_text(acceptance_criteria=[], risks=[]))
    assert decision.acceptance_criteria == ()
    assert decision.risks == ()


def test_valid_multiple_steps():
    steps = [{"title": f"Step {i}", "objective": f"Objective {i}"} for i in range(3)]
    decision = parse_plan_decision(_valid_text(steps=steps))
    assert len(decision.steps) == 3


# ---------------- structural rejection ----------------


def test_unknown_top_level_field_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(unexpected="value"))


def test_unknown_task_profile_field_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(task_profile={"complexity": "LOW", "scope": "LOCAL", "extra": 1}))


@pytest.mark.parametrize("missing", ["summary", "steps", "acceptance_criteria", "risks", "task_profile"])
def test_missing_required_field_rejected(missing):
    payload = _valid_payload()
    del payload[missing]
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(json.dumps(payload))


def test_duplicate_key_rejected():
    text = '{"summary":"a","summary":"b","steps":[{"title":"t","objective":"o"}],"acceptance_criteria":[],"risks":[],"task_profile":{"complexity":"LOW","scope":"LOCAL"}}'
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(text)


def test_markdown_fence_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(f"```json\n{_valid_text()}\n```")


def test_prose_prefix_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(f"Here is the plan: {_valid_text()}")


def test_prose_suffix_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(f"{_valid_text()} Let me know if you have questions.")


def test_top_level_array_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(f"[{_valid_text()}]")


def test_nan_rejected():
    raw = (
        '{"summary": NaN, "steps": [{"title":"t","objective":"o"}], '
        '"acceptance_criteria": [], "risks": [], '
        '"task_profile": {"complexity":"LOW","scope":"LOCAL"}}'
    )
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(raw)


def test_infinity_rejected():
    raw = (
        '{"summary": Infinity, "steps": [{"title":"t","objective":"o"}], '
        '"acceptance_criteria": [], "risks": [], '
        '"task_profile": {"complexity":"LOW","scope":"LOCAL"}}'
    )
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(raw)


def test_invalid_complexity_enum_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(task_profile={"complexity": "EXTREME", "scope": "LOCAL"}))


def test_invalid_scope_enum_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(task_profile={"complexity": "LOW", "scope": "GALAXY_WIDE"}))


def test_empty_steps_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(steps=[]))


def test_too_many_steps_rejected():
    steps = [{"title": f"s{i}", "objective": "o"} for i in range(MAX_PLAN_STEPS + 1)]
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(steps=steps))


def test_oversized_step_title_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(steps=[{"title": "x" * 400, "objective": "o"}]))


def test_oversized_step_objective_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(steps=[{"title": "t", "objective": "x" * 3000}]))


def test_too_many_acceptance_criteria_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(acceptance_criteria=[f"c{i}" for i in range(25)]))


def test_oversized_acceptance_criterion_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(acceptance_criteria=["x" * 1500]))


def test_too_many_risks_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(risks=[f"r{i}" for i in range(20)]))


def test_oversized_risk_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(risks=["x" * 1500]))


def test_model_output_over_max_chars_rejected():
    oversized = "x" * (MAX_MODEL_OUTPUT_CHARS + 1)
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(oversized)


def test_wrong_type_summary_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(summary=123))


def test_wrong_type_steps_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(steps="not a list"))


def test_wrong_type_acceptance_criteria_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(acceptance_criteria="not a list"))


def test_step_missing_field_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(steps=[{"title": "t"}]))


def test_step_unknown_field_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(steps=[{"title": "t", "objective": "o", "extra": 1}]))


def test_step_not_an_object_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(steps=["just a string"]))


def test_acceptance_criteria_entry_wrong_type_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(acceptance_criteria=[123]))


def test_empty_output_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision("")


def test_whitespace_only_output_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision("   \n  ")


def test_malformed_json_rejected():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision("{not valid json")


# ---------------- model cannot forge runtime-owned fields ----------------


def test_model_cannot_supply_plan_id():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(plan_id="plan_forged"))


def test_model_cannot_supply_repository_fingerprint():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(repository_fingerprint="a" * 64))


def test_model_cannot_supply_task_sha256():
    with pytest.raises(PlannerProtocolError):
        parse_plan_decision(_valid_text(task_sha256="a" * 64))


def test_model_cannot_supply_provider_routing_fields():
    for field in ("model", "provider", "executor", "agent", "route", "budget"):
        with pytest.raises(PlannerProtocolError):
            parse_plan_decision(_valid_text(**{field: "value"}))
