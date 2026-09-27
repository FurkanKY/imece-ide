"""decision_runtime.fake_backend.FakeDecisionBackend — scripted DecisionPort for tests."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_runtime.errors import DecisionBackendError  # noqa: E402
from decision_runtime.fake_backend import FakeDecisionBackend  # noqa: E402
from decision_runtime.models import Choice, DecisionSpec, DecisionResult, NoulAnswer  # noqa: E402


def _spec():
    return DecisionSpec(
        decision_id="d1", question_set_version="v1",
        questions={"q": Choice(instructions="i", criteria={"a": "A", "b": "B"})},
    )


def _result(decision_id="d1"):
    return DecisionResult(
        decision_id=decision_id, question_set_version="v1", answers={"q": NoulAnswer(noul=0.5)},
        backend="fake", model_version="fake-v1", latency_ms=1, prompt_tokens=0, fallback_used=False,
    )


def test_returns_scripted_results_in_order():
    backend = FakeDecisionBackend([_result(), _result()])
    spec = _spec()
    backend.decide(spec, {})
    backend.decide(spec, {})
    assert backend.call_count == 2
    assert len(backend.calls) == 2


def test_raises_scripted_exceptions():
    backend = FakeDecisionBackend([DecisionBackendError("boom")])
    with pytest.raises(DecisionBackendError):
        backend.decide(_spec(), {})


def test_invokes_scripted_callable_with_actual_args():
    seen = {}

    def make_result(spec, state):
        seen["state"] = state
        return _result(decision_id=spec.decision_id)

    backend = FakeDecisionBackend([make_result])
    spec = _spec()
    result = backend.decide(spec, {"x": 1})
    assert seen["state"] == {"x": 1}
    assert result.decision_id == spec.decision_id


def test_raises_when_script_exhausted():
    backend = FakeDecisionBackend([_result()])
    backend.decide(_spec(), {})
    with pytest.raises(DecisionBackendError):
        backend.decide(_spec(), {})
