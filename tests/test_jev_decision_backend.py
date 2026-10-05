"""JevDecisionBackend — SDK-free tests (docs/JEV-DESIGN.md, Spike S1b).

Covers the laziness/lifecycle/validation logic of the real backend with
injected scripted clients — NO typesafe-sdk requirement: every test here
runs whether or not the optional SDK is installed, and none of them touches
the network. The real-SDK contract (AsyncTypeSafeClient over
httpx2.MockTransport, exact retry/timeout wiring, streaming deadline
behavior, wire-body log suppression) lives in
tests/test_jev_decision_backend_sdk.py, which skips via
pytest.importorskip when the optional SDK is absent.

Key properties proven here:
- the constructor never imports the SDK and never reads TYPESAFE_API_KEY;
- a missing key / missing SDK surfaces only at decide() as a typed
  DecisionBackendError (never a run-construction failure);
- the production asyncio wrapper: async injected clients work, the wallclock
  deadline cancels a hung/trickling call (typed TIMEOUT), the close always
  runs (bounded), a running event loop is refused safely with no leaked
  coroutine, and no extra threads are created;
- response validation: exact question set, unknown labels, non-finite
  scores, bad sums, contradictory (non-argmax) choices, invalid token
  counts — all typed MALFORMED/REQUEST_INVALID, no raw values echoed;
- error mapping safety: fixed messages only, no cause chain, no key.
"""

import asyncio
import logging
import sys
import threading
import time
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_runtime.errors import DecisionBackendError, DecisionBackendFailureReason  # noqa: E402
from decision_runtime.jev_backend import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_CLOSE_BUDGET_S,
    DEFAULT_DEADLINE_S,
    DEFAULT_MODEL,
    DEFAULT_REQUEST_TIMEOUT_S,
    JevDecisionBackend,
    _SdkWireBodyLogFilter,
)
from decision_runtime.models import ChoiceAnswer, NoulAnswer, ScoreAnswer  # noqa: E402
from decision_runtime.triage import TriageFacts, build_triage_spec, build_triage_state  # noqa: E402

KEY = "k-test-decision-backend-key"

_QUESTION_NAMES = ("failure_kind", "caused_by_change", "fixable_by_agent")


# ---------------------------------------------------------------------------
# fixtures/helpers (SDK-free)
# ---------------------------------------------------------------------------


def _triage_state():
    facts = TriageFacts(
        check_id="c1",
        command=("pytest", "-q", "tests/test_a.py"),
        exit_code=1,
        timed_out=False,
        error_block="ModuleNotFoundError: No module named 'requests'",
        changed_paths=("src/adapter.py",),
        baseline_status="fail",
        baseline_exit_code=1,
    )
    return build_triage_state(facts), build_triage_spec("verification_failure_triage")


def _fake_answers():
    """Real-SDK-shaped answers for the triage question set (all three
    primitives; score probabilities as the string keys raw JSON carries)."""
    choice = type("FakeChoiceAnswer", (), {})()
    choice.type = "choice"
    choice.choice = "missing_dependency"
    choice.probabilities = {
        "code_bug": 0.02,
        "test_needs_update": 0.02,
        "missing_dependency": 0.9,
        "environment_or_tooling": 0.02,
        "flaky_or_timeout": 0.02,
        "unrelated_preexisting": 0.02,
    }
    choice.confidence = 0.9
    noul = type("FakeNoulAnswer", (), {})()
    noul.type = "noul"
    noul.noul = 0.15
    score = type("FakeScoreAnswer", (), {})()
    score.type = "score"
    score.score = 0.0
    score.confidence = 0.8
    score.probabilities = {"0": 0.8, "1": 0.1, "2": 0.1}
    score.legend = {"0": "needs the user", "1": "uncertain", "2": "clearly fixable"}
    return {"failure_kind": choice, "caused_by_change": noul, "fixable_by_agent": score}


def _fake_response(answers=None, model="jev-1.13.0", usage=(120, 12)):
    response = type("FakeResponse", (), {})()
    response.model = model
    usage_obj = type("FakeUsage", (), {})()
    usage_obj.input_tokens = usage[0] if usage else None
    usage_obj.output_tokens = usage[1] if usage else None
    response.usage = usage_obj if usage else None
    response.answers = dict(_fake_answers() if answers is None else answers)
    return response


class _FakeClient:
    """Duck-typed SYNC stand-in for the SDK client (legacy test seam): the
    backend tolerates it, but it carries NO wallclock promise (documented).
    Records the system_one call, optionally raises, counts close() calls."""

    def __init__(self, response=None, error=None):
        self.response = response if response is not None else _fake_response()
        self.error = error
        self.calls: list[dict] = []
        self.closed = False

    def system_one(self, *, state, questions, **kwargs):
        self.calls.append({"state": state, "questions": questions})
        if self.error is not None:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


class _FakeAsyncClient:
    """Duck-typed ASYNC stand-in for the production AsyncTypeSafeClient:
    awaitable system_one + awaitable aclose — the seam the backend actually
    promises the wallclock deadline on."""

    def __init__(self, response=None, *, error=None, call_delay=0.0, close_delay=0.0):
        self.response = response if response is not None else _fake_response()
        self.error = error
        self.call_delay = call_delay
        self.close_delay = close_delay
        self.calls: list[dict] = []
        self.closed = False

    async def system_one(self, *, state, questions, **kwargs):
        self.calls.append({"state": state, "questions": questions})
        if self.call_delay:
            await asyncio.sleep(self.call_delay)
        if self.error is not None:
            raise self.error
        return self.response

    async def aclose(self):
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        self.closed = True


def _backend_with(client, captured: dict | None = None, **backend_kwargs):
    def factory(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return client

    return JevDecisionBackend(client_factory=factory, api_key_resolver=lambda: KEY, **backend_kwargs)


def _variant_response(**answer_edits):
    answers = _fake_answers()
    for name, answer in answer_edits.items():
        if answer is None:
            answers.pop(name, None)
        else:
            answers[name] = answer
    return _fake_response(answers=answers)


# ---------------------------------------------------------------------------
# laziness / missing key / missing SDK
# ---------------------------------------------------------------------------


def test_constructor_is_lazy_no_key_check_no_sdk_import(monkeypatch):
    def _must_not_import():  # pragma: no cover - only runs on a lazy-constructor bug
        raise AssertionError("JevDecisionBackend must not import the SDK in its constructor")

    monkeypatch.setattr("decision_runtime.jev_backend._import_typesafe_sdk", _must_not_import)
    monkeypatch.setattr("decision_runtime.jev_backend.import_typesafe_sdk", _must_not_import)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    backend = JevDecisionBackend()
    assert backend.BACKEND_NAME == "jev"
    assert backend.REMOTE_STATE_SANITIZED is True


def test_decide_without_key_fails_typed_and_without_sdk_import(monkeypatch):
    def _must_not_import():  # pragma: no cover - only runs on a key-before-SDK bug
        raise AssertionError("decide() must fail on the missing key before importing the SDK")

    monkeypatch.setattr("decision_runtime.jev_backend._import_typesafe_sdk", _must_not_import)
    monkeypatch.setattr("decision_runtime.jev_backend.import_typesafe_sdk", _must_not_import)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        JevDecisionBackend().decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MISSING_API_KEY
    assert "TYPESAFE_API_KEY" in str(excinfo.value)


def test_decide_without_sdk_fails_typed(monkeypatch):
    def _no_sdk():
        raise ImportError("No module named 'typesafe_sdk'")

    monkeypatch.setattr("decision_runtime.jev_backend._import_typesafe_sdk", _no_sdk)
    monkeypatch.setattr("decision_runtime.jev_backend.import_typesafe_sdk", _no_sdk)
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        JevDecisionBackend().decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MISSING_SDK


def test_public_import_seam_is_shared_and_lazy(monkeypatch):
    """The credentials/UI workers bind the SDK import through this module's
    seam; the PUBLIC name exists and both names resolve to the same
    function."""
    from decision_runtime import jev_backend

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert jev_backend._import_typesafe_sdk is jev_backend.import_typesafe_sdk


def test_key_is_read_only_from_environ_at_decide_time(monkeypatch):
    captured: dict = {}
    client = _FakeClient()
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)

    def factory(**kwargs):
        captured.update(kwargs)
        return client

    backend = JevDecisionBackend(client_factory=factory)  # default env-only resolver
    state, spec = _triage_state()
    backend.decide(spec, state)
    assert captured["api_key"] == KEY
    assert client.closed is True


def test_key_value_is_never_logged_and_never_in_error_messages(monkeypatch, caplog):
    planted = f"{KEY} leaked-evidence-payload-42"
    client = _FakeClient(error=RuntimeError(planted))
    with caplog.at_level(logging.DEBUG):
        backend = _backend_with(client)
        state, spec = _triage_state()
        with pytest.raises(DecisionBackendError) as excinfo:
            backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.REQUEST_INVALID
    assert KEY not in str(excinfo.value)
    assert "leaked-evidence-payload-42" not in str(excinfo.value)
    assert all(KEY not in str(record.getMessage()) for record in caplog.records)
    assert client.closed is True  # closed in finally despite the failure


def test_default_api_key_resolver_ignores_empty_and_non_string_values(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "   ")
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        JevDecisionBackend().decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MISSING_API_KEY


def test_non_dict_state_fails_typed_without_network():
    client = _FakeClient()
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, ["not", "a", "dict"])
    assert excinfo.value.reason is DecisionBackendFailureReason.REQUEST_INVALID
    assert client.calls == []


# ---------------------------------------------------------------------------
# the production asyncio wrapper (SDK-free: injected async clients)
# ---------------------------------------------------------------------------


def test_decide_returns_canonical_result_via_async_injected_factory():
    client = _FakeAsyncClient()
    backend = _backend_with(client)
    state, spec = _triage_state()

    result = backend.decide(spec, state)

    assert result.decision_id == "verification_failure_triage"
    assert result.question_set_version == spec.question_set_version
    assert result.backend == "jev"
    assert result.model_version == "jev-1.13.0"
    assert result.prompt_tokens == 120
    assert result.fallback_used is False
    assert isinstance(result.latency_ms, int) and result.latency_ms >= 0
    assert isinstance(result.answers["failure_kind"], ChoiceAnswer)
    assert isinstance(result.answers["caused_by_change"], NoulAnswer)
    assert isinstance(result.answers["fixable_by_agent"], ScoreAnswer)
    assert result.answers["failure_kind"].choice == "missing_dependency"
    # score probabilities arrive with int SDK keys -> str in local models
    assert set(result.answers["fixable_by_agent"].probabilities) == {"0", "1", "2"}
    assert client.closed is True  # aclose awaited


def test_deadline_cancels_a_hung_async_call_typed_and_closed():
    """A call that never answers is cancelled by the wallclock deadline: the
    backend returns a typed TIMEOUT (the gate falls back to the rule
    backend), the async client's aclose still ran, and no extra thread was
    left behind."""
    client = _FakeAsyncClient(call_delay=30.0)
    backend = _backend_with(client, deadline_s=0.2)
    state, spec = _triage_state()
    threads_before = threading.active_count()

    started = time.monotonic()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    elapsed = time.monotonic() - started

    assert excinfo.value.reason is DecisionBackendFailureReason.TIMEOUT
    assert 0.15 <= elapsed < 5.0
    assert client.closed is True  # aclose ran in finally despite the deadline
    assert threading.active_count() == threads_before  # no runaway thread


def test_close_budget_bounds_a_hung_close_without_losing_the_result():
    """The close that always follows gets its own bounded budget: even a hung
    close cannot extend the wallclock unboundedly, and a SUCCESSFUL response
    is still returned."""
    client = _FakeAsyncClient(close_delay=30.0)
    backend = _backend_with(client, deadline_s=1.0, close_budget_s=0.2)
    state, spec = _triage_state()

    started = time.monotonic()
    result = backend.decide(spec, state)
    elapsed = time.monotonic() - started

    assert result.model_version == "jev-1.13.0"
    assert elapsed < 5.0
    assert client.closed is False  # close was abandoned at its budget (best-effort by contract)


def test_hung_close_after_deadline_does_not_hang_decide():
    client = _FakeAsyncClient(call_delay=30.0, close_delay=30.0)
    backend = _backend_with(client, deadline_s=0.2, close_budget_s=0.2)
    state, spec = _triage_state()

    started = time.monotonic()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    elapsed = time.monotonic() - started

    assert excinfo.value.reason is DecisionBackendFailureReason.TIMEOUT
    assert elapsed < 5.0  # deadline + close budget, no unbounded hang


def test_decide_inside_a_running_loop_fails_typed_with_no_leaked_coroutine():
    """Called from inside a running event loop (e.g. a UI/async worker), the
    backend refuses UP FRONT — typed error, no nested-loop RuntimeError, and
    no coroutine is ever created (nothing is left un-awaited)."""
    state, spec = _triage_state()
    backend = _backend_with(_FakeAsyncClient())

    async def scenario():
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)  # a leaked coroutine would warn
            with pytest.raises(DecisionBackendError) as excinfo:
                backend.decide(spec, state)
        assert excinfo.value.reason is DecisionBackendFailureReason.REQUEST_INVALID

    asyncio.run(scenario())


def test_factory_error_is_mapped_typed():
    def broken_factory(**kwargs):
        raise ValueError("bad factory")

    backend = JevDecisionBackend(client_factory=broken_factory, api_key_resolver=lambda: KEY)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.REQUEST_INVALID
    assert "bad factory" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# sent-state / question shaping (injected factory)
# ---------------------------------------------------------------------------


def test_decide_sends_allowlisted_state_and_labels_untrusted_fields():
    client = _FakeClient()
    backend = _backend_with(client)
    state, spec = _triage_state()
    state["complete_file_contents"] = "SECRET FILE BODY"  # unknown field: must never leave

    backend.decide(spec, state)

    sent_state = client.calls[0]["state"]
    assert sent_state == {
        "command": ["pytest", "-q", "tests/test_a.py"],
        "exit_code": 1,
        "timed_out": False,
        "error_block": "ModuleNotFoundError: No module named 'requests'",
        "changed_paths": ["src/adapter.py"],
        "baseline_status": "fail",
        "baseline_exit_code": 1,
    }
    assert "complete_file_contents" not in sent_state
    sent_questions = client.calls[0]["questions"]
    assert set(sent_questions) == set(_QUESTION_NAMES)
    assert sent_questions["failure_kind"]["type"] == "choice"
    assert sent_questions["caused_by_change"]["type"] == "noul"
    assert sent_questions["fixable_by_agent"]["type"] == "score"
    # design rule 4: the untrusted diagnostics are labeled in the instructions
    for name in _QUESTION_NAMES:
        assert "untrusted process output" in sent_questions[name]["instructions"]


def test_factory_receives_pinned_model_timeout_and_base_url():
    captured: dict = {}
    client = _FakeClient()
    backend = _backend_with(client, captured=captured)
    state, spec = _triage_state()

    backend.decide(spec, state)

    assert captured["model"] == DEFAULT_MODEL
    assert captured["timeout"] == DEFAULT_REQUEST_TIMEOUT_S
    assert captured["base_url"] == DEFAULT_BASE_URL == "https://api.typesafe.ai"


def test_typesafe_default_model_env_override_is_honored(monkeypatch):
    captured: dict = {}
    backend = _backend_with(_FakeClient(), captured=captured)
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "jev-9.9.9")
    state, spec = _triage_state()

    backend.decide(spec, state)

    assert captured["model"] == "jev-9.9.9"


def test_typesafe_default_model_env_invalid_value_fails_closed(monkeypatch):
    backend = _backend_with(_FakeClient())
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "jev with spaces")
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.REQUEST_INVALID


def test_base_url_is_never_taken_from_ambient_env(monkeypatch):
    """The backend pins the HTTPS endpoint explicitly; an ambient
    TYPESAFE_BASE_URL (which could embed credentials) is never honored."""
    captured: dict = {}
    backend = _backend_with(_FakeClient(), captured=captured)
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://user:secret@evil.example.com")
    state, spec = _triage_state()

    backend.decide(spec, state)

    assert captured["base_url"] == "https://api.typesafe.ai"


def test_public_sanitize_helper_matches_remote_state_semantics():
    backend = JevDecisionBackend()
    clean_stdout, clean_stderr, command = backend.sanitize_raw_diagnostics(
        stdout='PASSWORD="correct horse battery staple"',
        stderr="",
        command=("sh", "-c", "tool --password secret"),
    )
    assert clean_stdout == "PASSWORD=[REDACTED]"
    assert command == ("sh", "-c", "tool --password [REDACTED]")


# ---------------------------------------------------------------------------
# malformed / inconsistent remote answers -> typed fallback signal
# ---------------------------------------------------------------------------


def test_missing_answer_is_malformed():
    client = _FakeClient(_variant_response(fixable_by_agent=None))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE


def test_extraneous_answer_is_malformed():
    answers = _fake_answers()
    answers["extra_question"] = answers["caused_by_change"]
    client = _FakeClient(_fake_response(answers=answers))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE


def test_unknown_choice_label_is_malformed_and_value_not_echoed():
    answer = _fake_answers()["failure_kind"]
    answer.choice = "not_a_real_label_42"
    client = _FakeClient(_fake_response(answers={"failure_kind": answer}))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE
    assert "not_a_real_label_42" not in str(excinfo.value)


def test_choice_not_the_argmax_of_its_own_probabilities_is_malformed():
    answer = _fake_answers()["failure_kind"]
    answer.choice = "code_bug"  # probabilities clearly favor missing_dependency
    client = _FakeClient(_fake_response(answers={"failure_kind": answer}))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE


def test_choice_tied_with_the_max_is_accepted():
    answer = _fake_answers()["failure_kind"]
    answer.choice = "code_bug"
    answer.probabilities = dict(answer.probabilities)
    # exact tie for the max (0.49 + 0.49 + 4x0.005 = 1.0)
    answer.probabilities["code_bug"] = 0.49
    answer.probabilities["missing_dependency"] = 0.49
    for label in ("test_needs_update", "environment_or_tooling", "flaky_or_timeout", "unrelated_preexisting"):
        answer.probabilities[label] = 0.005
    client = _FakeClient(_variant_response(failure_kind=answer))
    backend = _backend_with(client)
    state, spec = _triage_state()

    result = backend.decide(spec, state)

    assert result.answers["failure_kind"].choice == "code_bug"


def test_missing_probability_label_is_malformed():
    answer = _fake_answers()["failure_kind"]
    answer.probabilities.pop("code_bug")
    client = _FakeClient(_fake_response(answers={"failure_kind": answer}))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE


def test_nonfinite_score_is_malformed():
    answer = _fake_answers()["fixable_by_agent"]
    answer.score = float("nan")
    client = _FakeClient(_variant_response(fixable_by_agent=answer))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE


def test_score_outside_levels_is_malformed():
    answer = _fake_answers()["fixable_by_agent"]
    answer.score = 2.5  # max level is 2
    client = _FakeClient(_variant_response(fixable_by_agent=answer))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE


def test_clearly_invalid_probability_sum_is_malformed():
    answer = _fake_answers()["failure_kind"]
    answer.probabilities = {label: 0.15 for label in answer.probabilities}  # sums to 0.9
    client = _FakeClient(_fake_response(answers={"failure_kind": answer}))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE


def test_rounded_probability_sum_within_tolerance_is_normalized():
    answer = _fake_answers()["failure_kind"]
    answer.probabilities = dict(answer.probabilities)
    answer.probabilities["missing_dependency"] = 0.89  # sum 0.99 — rounded-server shape
    client = _FakeClient(_variant_response(failure_kind=answer))
    backend = _backend_with(client)
    state, spec = _triage_state()

    result = backend.decide(spec, state)

    probabilities = result.answers["failure_kind"].probabilities
    assert abs(sum(probabilities.values()) - 1.0) <= 1e-6
    assert probabilities["missing_dependency"] == max(probabilities.values())


def test_confidence_out_of_range_is_malformed():
    answer = _fake_answers()["failure_kind"]
    answer.confidence = 1.5
    client = _FakeClient(_fake_response(answers={"failure_kind": answer}))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE


def test_wrong_answer_kind_for_question_is_malformed():
    client = _FakeClient(
        _fake_response(answers={"failure_kind": _fake_answers()["caused_by_change"]})
    )
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE


def test_invalid_model_version_is_malformed():
    client = _FakeClient(_fake_response(model="jev v1.13 (internal build)"))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE
    assert "internal build" not in str(excinfo.value)


def test_missing_usage_is_malformed():
    client = _FakeClient(_fake_response(usage=None))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE


def test_none_input_tokens_maps_to_zero_prompt_tokens():
    client = _FakeClient(_fake_response(usage=(None, None)))
    backend = _backend_with(client)
    state, spec = _triage_state()

    result = backend.decide(spec, state)

    assert result.prompt_tokens == 0


@pytest.mark.parametrize("bad_tokens", ["120", -5, 1.5, True, [], {}])
def test_invalid_token_counts_are_rejected_not_quietly_zeroed(bad_tokens):
    client = _FakeClient(_fake_response(usage=(bad_tokens, 0)))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE
    assert str(bad_tokens) not in str(excinfo.value)


# ---------------------------------------------------------------------------
# error mapping safety (sync injected seam)
# ---------------------------------------------------------------------------


def test_http_error_message_is_safe_even_from_unmapped_exceptions():
    client = _FakeClient(error=RuntimeError("HTTP 500 with raw body {\"secret\": \"x\"}"))
    backend = _backend_with(client)
    state, spec = _triage_state()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.REQUEST_INVALID
    assert "raw body" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None  # cause chain suppressed, nothing leaks via traceback objects


# ---------------------------------------------------------------------------
# SDK wire-body log suppression (pure logging, no SDK needed)
# ---------------------------------------------------------------------------


def test_wire_body_log_filter_drops_debug_only():
    record_debug = logging.LogRecord("typesafe_sdk", logging.DEBUG, "p", 1, "body=%r", ("x",), None)
    record_info = logging.LogRecord("typesafe_sdk", logging.INFO, "p", 1, "request ok", (), None)
    guard = _SdkWireBodyLogFilter()
    assert guard.filter(record_debug) is False
    assert guard.filter(record_info) is True
