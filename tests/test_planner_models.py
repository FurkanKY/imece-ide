import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from planner_runtime.errors import PlannerInputError  # noqa: E402
from planner_runtime.models import (  # noqa: E402
    MAX_ACCEPTANCE_CRITERIA,
    MAX_ACCEPTANCE_CRITERION_CHARS,
    MAX_PLAN_STEPS,
    MAX_RISK_CHARS,
    MAX_RISKS,
    MAX_STEP_OBJECTIVE_CHARS,
    MAX_STEP_TITLE_CHARS,
    MAX_SUMMARY_CHARS,
    ParsedPlanDecision,
    PlanReport,
    PlanStep,
    TaskComplexity,
    TaskProfile,
    TaskScope,
    new_plan_id,
    validate_plan_id,
)

FP = "a" * 64


def _step(title="Do the thing", objective="Achieve the objective"):
    return PlanStep(title=title, objective=objective)


def _profile(complexity=TaskComplexity.LOW, scope=TaskScope.LOCAL):
    return TaskProfile(complexity=complexity, scope=scope)


def _decision(**overrides):
    kwargs = dict(
        summary="A summary",
        steps=(_step(),),
        acceptance_criteria=("tests pass",),
        risks=("none",),
        task_profile=_profile(),
    )
    kwargs.update(overrides)
    return ParsedPlanDecision(**kwargs)


def _report(**overrides):
    kwargs = dict(
        plan_id=new_plan_id(),
        summary="A summary",
        steps=(_step(),),
        acceptance_criteria=("tests pass",),
        risks=("none",),
        task_profile=_profile(),
        repository_fingerprint=FP,
        task_sha256=FP,
    )
    kwargs.update(overrides)
    return PlanReport(**kwargs)


# ---------------- PlanStep ----------------


def test_step_minimal_valid():
    step = _step()
    assert step.title == "Do the thing"


def test_step_rejects_empty_title():
    with pytest.raises(PlannerInputError):
        PlanStep(title="", objective="x")


def test_step_rejects_empty_objective():
    with pytest.raises(PlannerInputError):
        PlanStep(title="x", objective="")


def test_step_rejects_nul_title():
    with pytest.raises(PlannerInputError):
        PlanStep(title="bad\x00title", objective="x")


def test_step_rejects_oversized_title():
    with pytest.raises(PlannerInputError):
        PlanStep(title="x" * (MAX_STEP_TITLE_CHARS + 1), objective="x")


def test_step_rejects_oversized_objective():
    with pytest.raises(PlannerInputError):
        PlanStep(title="x", objective="x" * (MAX_STEP_OBJECTIVE_CHARS + 1))


def test_step_is_frozen():
    step = _step()
    with pytest.raises(AttributeError):
        step.title = "other"


# ---------------- TaskComplexity / TaskScope / TaskProfile ----------------


def test_task_complexity_values():
    assert {c.value for c in TaskComplexity} == {"LOW", "MEDIUM", "HIGH"}


def test_task_scope_values():
    assert {s.value for s in TaskScope} == {"LOCAL", "MULTI_AREA", "REPOSITORY_WIDE"}


def test_task_profile_valid_combinations():
    for complexity in TaskComplexity:
        for scope in TaskScope:
            profile = TaskProfile(complexity=complexity, scope=scope)
            assert profile.complexity is complexity
            assert profile.scope is scope


def test_task_profile_rejects_non_enum_complexity():
    with pytest.raises(PlannerInputError):
        TaskProfile(complexity="LOW", scope=TaskScope.LOCAL)


def test_task_profile_rejects_non_enum_scope():
    with pytest.raises(PlannerInputError):
        TaskProfile(complexity=TaskComplexity.LOW, scope="LOCAL")


def test_task_profile_has_no_routing_fields():
    profile = _profile()
    assert not hasattr(profile, "model")
    assert not hasattr(profile, "provider")
    assert not hasattr(profile, "executor")
    assert not hasattr(profile, "route")
    assert not hasattr(profile, "budget")


# ---------------- ParsedPlanDecision ----------------


def test_decision_minimal_valid():
    decision = _decision()
    assert decision.summary == "A summary"
    assert len(decision.steps) == 1


def test_decision_rejects_empty_steps():
    with pytest.raises(PlannerInputError):
        _decision(steps=())


def test_decision_rejects_too_many_steps():
    with pytest.raises(PlannerInputError):
        _decision(steps=tuple(_step(title=f"s{i}") for i in range(MAX_PLAN_STEPS + 1)))


def test_decision_accepts_max_steps():
    decision = _decision(steps=tuple(_step(title=f"s{i}") for i in range(MAX_PLAN_STEPS)))
    assert len(decision.steps) == MAX_PLAN_STEPS


def test_decision_allows_empty_acceptance_criteria_and_risks():
    decision = _decision(acceptance_criteria=(), risks=())
    assert decision.acceptance_criteria == ()
    assert decision.risks == ()


def test_decision_rejects_too_many_acceptance_criteria():
    with pytest.raises(PlannerInputError):
        _decision(acceptance_criteria=tuple(f"c{i}" for i in range(MAX_ACCEPTANCE_CRITERIA + 1)))


def test_decision_rejects_oversized_acceptance_criterion():
    with pytest.raises(PlannerInputError):
        _decision(acceptance_criteria=("x" * (MAX_ACCEPTANCE_CRITERION_CHARS + 1),))


def test_decision_rejects_too_many_risks():
    with pytest.raises(PlannerInputError):
        _decision(risks=tuple(f"r{i}" for i in range(MAX_RISKS + 1)))


def test_decision_rejects_oversized_risk():
    with pytest.raises(PlannerInputError):
        _decision(risks=("x" * (MAX_RISK_CHARS + 1),))


def test_decision_rejects_wrong_step_type():
    with pytest.raises(PlannerInputError):
        _decision(steps=("not a step",))


def test_decision_rejects_wrong_task_profile_type():
    with pytest.raises(PlannerInputError):
        _decision(task_profile="LOW")


def test_decision_is_frozen():
    decision = _decision()
    with pytest.raises(AttributeError):
        decision.summary = "other"


def test_decision_summary_bounded():
    with pytest.raises(PlannerInputError):
        _decision(summary="x" * (MAX_SUMMARY_CHARS + 1))


# ---------------- PlanReport ----------------


def test_report_minimal_valid():
    report = _report()
    assert report.plan_id.startswith("plan_")
    assert report.repository_fingerprint == FP
    assert report.task_sha256 == FP


def test_report_rejects_bad_plan_id():
    with pytest.raises(PlannerInputError):
        _report(plan_id="")


def test_report_rejects_bad_repository_fingerprint():
    with pytest.raises(PlannerInputError):
        _report(repository_fingerprint="not-hex")


def test_report_rejects_bad_task_sha256():
    with pytest.raises(PlannerInputError):
        _report(task_sha256="short")


def test_report_rejects_empty_steps():
    with pytest.raises(PlannerInputError):
        _report(steps=())


def test_report_is_frozen():
    report = _report()
    with pytest.raises(AttributeError):
        report.plan_id = "other"


def test_report_no_mutable_list_leakage():
    step = _step()
    report = _report(steps=(step,))
    assert isinstance(report.steps, tuple)
    assert isinstance(report.acceptance_criteria, tuple)
    assert isinstance(report.risks, tuple)


# ---------------- IDs ----------------


def test_new_plan_id_shape():
    plan_id = new_plan_id()
    assert plan_id.startswith("plan_")
    assert validate_plan_id(plan_id) == plan_id


def test_new_plan_id_unique():
    assert new_plan_id() != new_plan_id()


@pytest.mark.parametrize("bad", ["", "has space", "x" * 200, "plan/1", "plan\x00id"])
def test_validate_plan_id_rejects_bad_shapes(bad):
    with pytest.raises(PlannerInputError):
        validate_plan_id(bad)


# ---------------- task_sha256 runtime computation (mirrored at runner level) ----------------


def test_task_sha256_deterministic_reference():
    task = "Add a feature to the widget."
    expected = hashlib.sha256(task.encode("utf-8")).hexdigest()
    report = _report(task_sha256=expected)
    assert report.task_sha256 == expected
    assert len(report.task_sha256) == 64
