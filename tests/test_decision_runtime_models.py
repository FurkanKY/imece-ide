"""decision_runtime.models — contract validation tests."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_runtime.errors import DecisionInputError  # noqa: E402
from decision_runtime.models import (  # noqa: E402
    Choice,
    ChoiceAnswer,
    DecisionResult,
    DecisionSpec,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    validate_state,
)


def test_choice_requires_non_empty_criteria():
    with pytest.raises(DecisionInputError):
        Choice(instructions="pick one", criteria={})


def test_choice_rejects_too_long_instructions():
    with pytest.raises(DecisionInputError):
        Choice(instructions="x" * 5000, criteria={"a": "A"})


def test_score_requires_at_least_two_levels():
    with pytest.raises(DecisionInputError):
        Score(instructions="rate it", criteria=("only one",))
    score = Score(instructions="rate it", criteria=("low", "mid", "high"))
    assert score.max_score == 2


def test_noul_requires_exactly_true_false_keys():
    with pytest.raises(DecisionInputError):
        Noul(instructions="is it?", criteria={"yes": "Yes", "no": "No"})
    noul = Noul(instructions="is it?")
    assert set(noul.criteria) == {"true", "false"}


def test_decision_spec_validates_questions():
    spec = DecisionSpec(
        decision_id="d1", question_set_version="v1",
        questions={"q1": Choice(instructions="i", criteria={"a": "A", "b": "B"})},
    )
    assert spec.decision_id == "d1"
    with pytest.raises(DecisionInputError):
        DecisionSpec(decision_id="d1", question_set_version="v1", questions={})
    with pytest.raises(DecisionInputError):
        DecisionSpec(decision_id="bad id!", question_set_version="v1", questions=spec.questions)


def test_validate_state_bounds_and_requires_json_serializable():
    assert validate_state({"a": 1}) == {"a": 1}
    with pytest.raises(DecisionInputError):
        validate_state("not a dict")
    circular: dict = {}
    circular["self"] = circular
    with pytest.raises(DecisionInputError):
        validate_state(circular)
    with pytest.raises(DecisionInputError):
        validate_state({"big": "x" * 200_000})


def test_choice_answer_requires_choice_in_probabilities():
    with pytest.raises(DecisionInputError):
        ChoiceAnswer(choice="x", probabilities={"a": 1.0}, confidence=0.9)


def test_choice_answer_requires_probabilities_sum_to_one():
    with pytest.raises(DecisionInputError):
        ChoiceAnswer(choice="a", probabilities={"a": 0.5, "b": 0.3}, confidence=0.9)


def test_score_answer_and_noul_answer_validate_ranges():
    ScoreAnswer(score=1.5, probabilities={"0": 0.1, "1": 0.2, "2": 0.7}, confidence=0.8)
    with pytest.raises(DecisionInputError):
        ScoreAnswer(score="bad", probabilities={"0": 1.0}, confidence=0.8)
    NoulAnswer(noul=0.5)
    with pytest.raises(DecisionInputError):
        NoulAnswer(noul=1.5)


def test_decision_result_confidence_of_for_each_answer_kind():
    result = DecisionResult(
        decision_id="d1", question_set_version="v1",
        answers={
            "choice_q": ChoiceAnswer(choice="a", probabilities={"a": 0.9, "b": 0.1}, confidence=0.8),
            "score_q": ScoreAnswer(score=1.0, probabilities={"0": 0.1, "1": 0.8, "2": 0.1}, confidence=0.6),
            "noul_q": NoulAnswer(noul=0.9),
        },
        backend="rule", model_version="rule-v1", latency_ms=1, prompt_tokens=0, fallback_used=False,
    )
    assert result.confidence_of("choice_q") == 0.8
    assert result.confidence_of("score_q") == 0.6
    assert result.confidence_of("noul_q") == pytest.approx(0.8)  # |2*0.9 - 1|


def test_decision_result_rejects_bad_fields():
    answers = {"q": NoulAnswer(noul=0.5)}
    with pytest.raises(DecisionInputError):
        DecisionResult(
            decision_id="d1", question_set_version="v1", answers=answers,
            backend="rule", model_version="rule-v1", latency_ms=-1, prompt_tokens=0, fallback_used=False,
        )
    with pytest.raises(DecisionInputError):
        DecisionResult(
            decision_id="d1", question_set_version="v1", answers={},
            backend="rule", model_version="rule-v1", latency_ms=1, prompt_tokens=0, fallback_used=False,
        )
