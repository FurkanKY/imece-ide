"""JevDecisionBackend — real typesafe-sdk contract tests (Spike S1b).

This module REQUIRES the optional typesafe-sdk dependency and skips via
pytest.importorskip when it is absent — it is never part of a
SDK-less environment's failure surface, and none of these tests ever call
the live API (every response is an httpx2.MockTransport fixture).

Pinned against typesafe-sdk 0.7.2 (async path): AsyncTypeSafeClient's
`system_one` is awaitable and closing is `aclose()` (NOT `close()`). All
tests run the backend's production asyncio wrapper (`asyncio.run` +
`asyncio.wait_for` wallclock deadline + bounded `aclose` in `finally`).
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("typesafe_sdk")
pytest.importorskip("httpx2")

import httpx2  # noqa: E402
import typesafe_sdk  # noqa: E402

from decision_runtime.errors import DecisionBackendError, DecisionBackendFailureReason  # noqa: E402
from decision_runtime.jev_backend import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_REQUEST_TIMEOUT_S,
    JevDecisionBackend,
    RETRY_BUDGET_S,
    _build_retry_policy,
    _SdkWireBodyLogFilter,
)
from decision_runtime.models import ChoiceAnswer, NoulAnswer, ScoreAnswer  # noqa: E402
from decision_runtime.triage import TriageFacts, build_triage_spec, build_triage_state  # noqa: E402

KEY = "k-test-decision-backend-key"
_QUESTION_NAMES = ("failure_kind", "caused_by_change", "fixable_by_agent")


def _fake_answers():
    choice = type("FakeChoiceAnswer", (), {})()
    choice.type = "choice"
    choice.choice = "missing_dependency"
    choice.probabilities = {
        "code_bug": 0.02, "test_needs_update": 0.02, "missing_dependency": 0.9,
        "environment_or_tooling": 0.02, "flaky_or_timeout": 0.02, "unrelated_preexisting": 0.02,
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


def _fake_response(model="jev-1.13.0", usage=(120, 12)):
    response = type("FakeResponse", (), {})()
    response.model = model
    usage_obj = type("FakeUsage", (), {})()
    usage_obj.input_tokens = usage[0] if usage else None
    usage_obj.output_tokens = usage[1] if usage else None
    response.usage = usage_obj if usage else None
    response.answers = dict(_fake_answers())
    return response


class _RecordingAsyncClient:
    """The AsyncTypeSafeClient shape (awaitable system_one + aclose) used to
    intercept the default client factory without any network."""

    def __init__(self, **kwargs):
        pass

    async def system_one(self, **kwargs):
        return _fake_response()

    async def aclose(self):
        pass


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


class _TrickleStream(httpx2.AsyncByteStream):
    """An infinite, slowly-dribbling response body: per-phase HTTP timeouts
    can NEVER bound it (every read succeeds in time) — only the backend's
    wallclock deadline can. Records whether its aclose ran (httpx2 closes
    the response during deadline-cancellation unwinding)."""

    def __init__(self, interval: float = 0.05):
        self._interval = interval
        self.aclose_called = False
        self.iteration_cancelled = False

    async def __aiter__(self):
        try:
            while True:
                yield b" "
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            self.iteration_cancelled = True
            raise

    async def aclose(self):
        self.aclose_called = True


class _TrickleTransport(httpx2.AsyncBaseTransport):
    """Async transport whose RESPONSE body trickles forever (and optionally
    delays the response headers) — the unbounded-response shape the deadline
    must catch."""

    def __init__(self, *, headers_delay: float = 0.0, interval: float = 0.05):
        self.headers_delay = headers_delay
        self.requests: list[httpx2.Request] = []
        self.stream = _TrickleStream(interval=interval)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.headers_delay:
            await asyncio.sleep(self.headers_delay)
        return httpx2.Response(200, stream=self.stream)


class _CloseTracked:
    """Wraps a real client so tests can assert aclose() ran exactly once."""

    def __init__(self, client):
        self._client = client
        self.closed = False

    async def system_one(self, **kwargs):
        return await self._client.system_one(**kwargs)

    async def aclose(self):
        self.closed = True
        await self._client.aclose()


class _CountingTransport(httpx2.AsyncBaseTransport):
    """Records every request dispatched through any inner transport."""

    def __init__(self, inner: httpx2.AsyncBaseTransport):
        self._inner = inner
        self.requests: list[httpx2.Request] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return await self._inner.handle_async_request(request)


def _mock_backend(handler=None, *, transport=None, max_retries=0, **backend_kwargs):
    """Real AsyncTypeSafeClient over a MockTransport/AsyncBaseTransport,
    wired through the seam's own _build_retry_policy so tests exercise the
    production settings. Returns (backend, counting_transport, closed_flags)."""
    closed_flags: list[bool] = []

    def counting_handler(request: httpx2.Request) -> httpx2.Response:
        return handler(request)

    inner_transport = transport if transport is not None else httpx2.MockTransport(counting_handler)
    counting_transport = _CountingTransport(inner_transport)

    def factory(**kwargs):
        client = typesafe_sdk.AsyncTypeSafeClient(
            api_key=kwargs["api_key"],
            model=kwargs["model"],
            timeout=kwargs["timeout"],
            base_url=kwargs["base_url"],
            transport=counting_transport,
            retry=_build_retry_policy(max_retries),
        )
        tracked = _CloseTracked(client)
        original_aclose = tracked.aclose

        async def _note_aclose():
            await original_aclose()
            closed_flags.append(True)

        tracked.aclose = _note_aclose  # noqa: SLF001 - test seam
        return tracked

    backend = JevDecisionBackend(
        client_factory=factory, api_key_resolver=lambda: KEY, max_retries=max_retries, **backend_kwargs
    )
    return backend, counting_transport, closed_flags


def _sdk_response_body(status=200, *, retry_after=None):
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        answers = {}
        for name, question in body["questions"].items():
            if question["type"] == "choice":
                answers[name] = {
                    "type": "choice",
                    "choice": "missing_dependency",
                    "probabilities": {label: (0.9 if label == "missing_dependency" else 0.02) for label in question["criteria"]},
                    "confidence": 0.9,
                }
            elif question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.15}
            else:
                answers[name] = {
                    "type": "score",
                    "score": 0.0,
                    "confidence": 0.8,
                    "legend": {str(i): level for i, level in enumerate(question["criteria"])},
                    "probabilities": {"0": 0.8, "1": 0.1, "2": 0.1},
                }
        payload = {"model": "jev-1.13.0", "usage": {"input_tokens": 87, "output_tokens": 0}, "answers": answers}
        headers = {"content-type": "application/json"}
        if retry_after is not None:
            headers["retry-after"] = str(retry_after)
        return httpx2.Response(status, content=json.dumps(payload).encode(), headers=headers)

    return handler


# ---------------------------------------------------------------------------
# exact production settings on the REAL async client
# ---------------------------------------------------------------------------


def test_build_retry_policy_exact_settings():
    policy = _build_retry_policy(0)
    assert policy.max_retries == 0
    assert policy.backoff_initial == 0.0
    assert policy.backoff_max == 0.0
    assert policy.respect_retry_after is False
    assert policy.api_connection_error is False
    assert policy.api_timeout_error is False
    assert policy.timeout == RETRY_BUDGET_S

    policy_one = _build_retry_policy(1)
    assert policy_one.max_retries == 1


def test_default_client_factory_builds_async_client_with_exact_pinned_settings(monkeypatch):
    captured: dict = {}

    class _CapturingAsyncClient(_RecordingAsyncClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured.update(kwargs)

    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", _CapturingAsyncClient)
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)

    state, spec = _triage_state()
    JevDecisionBackend().decide(spec, state)

    assert captured["api_key"] == KEY
    assert captured["model"] == "jev-1.13.0"
    assert captured["timeout"] == DEFAULT_REQUEST_TIMEOUT_S
    assert captured["base_url"] == "https://api.typesafe.ai"
    policy = captured["retry"]
    assert policy.max_retries == 0
    assert policy.respect_retry_after is False
    assert policy.backoff_initial == 0.0


# ---------------------------------------------------------------------------
# real async client end-to-end (MockTransport)
# ---------------------------------------------------------------------------


def test_real_sdk_async_mock_transport_end_to_end():
    state, spec = _triage_state()
    state["internal_only"] = {"big": "blob"}  # never leaves
    backend, transport, closed = _mock_backend(_sdk_response_body())

    result = backend.decide(spec, state)

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.url.path == "/v1/systemone"
    assert request.url.host == "api.typesafe.ai"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "jev-1.13.0"
    assert "internal_only" not in body["state"]
    assert set(body["state"]) == {
        "command", "exit_code", "timed_out", "error_block", "changed_paths", "baseline_status", "baseline_exit_code",
    }
    assert set(body["questions"]) == set(_QUESTION_NAMES)
    # the real SystemOneResponse accessors, pinned against SDK 0.7.2
    assert result.backend == "jev"
    assert result.model_version == "jev-1.13.0"
    assert result.prompt_tokens == 87
    assert result.fallback_used is False
    assert isinstance(result.answers["failure_kind"], ChoiceAnswer)
    assert isinstance(result.answers["fixable_by_agent"], ScoreAnswer)
    assert isinstance(result.answers["caused_by_change"], NoulAnswer)
    assert result.answers["fixable_by_agent"].probabilities == {"0": 0.8, "1": 0.1, "2": 0.1}
    assert result.answers["caused_by_change"].noul == pytest.approx(0.15)
    assert result.confidence_of("failure_kind") == pytest.approx(0.9)
    assert closed == [True]  # the real client's aclose was awaited exactly once


@pytest.mark.parametrize(
    "status,expected_reason",
    [
        (400, DecisionBackendFailureReason.REQUEST_INVALID),
        (401, DecisionBackendFailureReason.AUTHENTICATION),
        (429, DecisionBackendFailureReason.RATE_LIMITED),
        (503, DecisionBackendFailureReason.REQUEST_INVALID),
    ],
)
def test_http_errors_are_single_attempt_typed_and_safe(status, expected_reason):
    """The default policy retries NOTHING: even 429/5xx are one bounded
    attempt, and 400/401 (never retryable) are a single call too."""
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status,
            content=json.dumps({"error": {"message": "raw server error body"}}).encode(),
            headers={"content-type": "application/json"},
        )

    state, spec = _triage_state()
    backend, transport, closed = _mock_backend(handler)
    started = time.monotonic()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    elapsed = time.monotonic() - started

    assert len(transport.requests) == 1  # no retry of any kind
    assert elapsed < 5.0  # bounded attempt, no Retry-After waits
    assert excinfo.value.reason is expected_reason
    assert "raw server error body" not in str(excinfo.value)
    assert closed == [True]  # aclose awaited in finally despite the HTTP failure
    assert excinfo.value.__cause__ is None  # no raw exception chain leaks


def test_http_5xx_is_retried_at_most_once_when_max_retries_is_1():
    state, spec = _triage_state()
    backend, transport, closed = _mock_backend(_sdk_response_body(503), max_retries=1)
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert len(transport.requests) == 2  # initial attempt + exactly one retry
    assert excinfo.value.reason is DecisionBackendFailureReason.REQUEST_INVALID


def test_http_429_retry_after_is_not_honoured_even_with_one_retry():
    """respect_retry_after=False: an hour-long Retry-After must not become an
    unbounded wait (bounded by the zero backoff instead)."""
    state, spec = _triage_state()
    backend, transport, closed = _mock_backend(_sdk_response_body(429, retry_after=3600), max_retries=1)
    started = time.monotonic()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    elapsed = time.monotonic() - started

    assert len(transport.requests) == 2
    assert elapsed < 5.0
    assert excinfo.value.reason is DecisionBackendFailureReason.RATE_LIMITED


def test_sdk_per_phase_timeout_is_typed_and_not_retried():
    """The SDK's own per-phase timeout path (httpx2 ReadTimeout ->
    TypeSafeAPITimeoutError) maps to the typed TIMEOUT reason."""
    state, spec = _triage_state()

    class _SlowHeadersTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(5.0)  # far beyond the per-phase HTTP timeout
            raise AssertionError("should have timed out first")  # pragma: no cover

    backend, transport, closed = _mock_backend(transport=_SlowHeadersTransport())
    started = time.monotonic()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    elapsed = time.monotonic() - started

    assert len(transport.requests) == 1  # dispatched once; the headers phase then hung
    assert 0.5 <= elapsed < 5.0  # per-phase timeout (1.2 s connect/read), not the 1.5 s deadline
    assert excinfo.value.reason is DecisionBackendFailureReason.TIMEOUT
    assert closed == [True]


# ---------------------------------------------------------------------------
# the wallclock deadline vs an UNBOUNDED trickling response
# ---------------------------------------------------------------------------


def test_deadline_cancels_a_trickling_response_and_closes_everything():
    """A response body that drips one byte every 50 ms forever: per-phase
    HTTP timeouts read each drip in time and would NEVER fire — the
    wallclock deadline cancels the task, httpx2 closes the response stream
    during cancellation, the client's aclose runs in the finally, and the
    caller gets a typed TIMEOUT. No background task survives asyncio.run."""
    state, spec = _triage_state()
    trickle = _TrickleTransport(interval=0.05)
    backend, counting, closed = _mock_backend(transport=trickle, deadline_s=0.5)

    started = time.monotonic()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    elapsed = time.monotonic() - started

    assert excinfo.value.reason is DecisionBackendFailureReason.TIMEOUT
    assert 0.4 <= elapsed < 5.0  # bounded by the deadline (+ close budget), not infinite
    assert len(counting.requests) == 1  # exactly one request was sent
    assert trickle.stream.iteration_cancelled is True  # the dribble task was cancelled
    assert trickle.stream.aclose_called is True  # the response stream was closed
    assert closed == [True]  # the client's aclose ran in the finally


def test_deadline_cancels_slow_response_headers_too():
    state, spec = _triage_state()
    trickle = _TrickleTransport(headers_delay=30.0)
    backend, transport, closed = _mock_backend(transport=trickle, deadline_s=0.5)

    started = time.monotonic()
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    elapsed = time.monotonic() - started

    assert excinfo.value.reason is DecisionBackendFailureReason.TIMEOUT
    assert elapsed < 5.0
    assert closed == [True]


def test_deadline_error_carries_no_raw_chain_or_body():
    state, spec = _triage_state()
    transport = _TrickleTransport(interval=0.05)
    backend, transport, closed = _mock_backend(transport=transport, deadline_s=0.5)

    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)

    assert excinfo.value.__cause__ is None
    assert "dribble" not in str(excinfo.value).lower()


def test_malformed_real_sdk_response_is_typed_as_malformed():
    """The real SDK silently DROPS answers with unknown types — an answer
    missing entirely must be malformed at OUR seam, not silently accepted."""
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=json.dumps({"model": "jev-1.13.0", "usage": {"input_tokens": 1}, "answers": {}}).encode(),
            headers={"content-type": "application/json"},
        )

    state, spec = _triage_state()
    backend, transport, closed = _mock_backend(handler)
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MALFORMED_RESPONSE


def test_no_background_tasks_survive_a_deadline_timeout(caplog):
    """asyncio.run must tear the loop down with NOTHING pending: the trickled
    response's stream was cancelled and closed, the client closed, and the
    asyncio logger stayed free of 'Task was destroyed' errors."""
    state, spec = _triage_state()
    trickle = _TrickleTransport(interval=0.05)
    backend, counting, closed = _mock_backend(transport=trickle, deadline_s=0.5)

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        with pytest.raises(DecisionBackendError) as excinfo:
            backend.decide(spec, state)

    assert excinfo.value.reason is DecisionBackendFailureReason.TIMEOUT
    assert trickle.stream.aclose_called and closed == [True]
    # "Task was destroyed but it is pending!" would surface as an asyncio
    # ERROR record; none may exist for this decide() call.
    assert all("pending" not in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# SDK wire-body log suppression (real SDK logger)
# ---------------------------------------------------------------------------


def test_sdk_body_logging_is_suppressed_even_at_debug_level(monkeypatch, caplog):
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", _RecordingAsyncClient)
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    sdk_logger = logging.getLogger("typesafe_sdk")
    with caplog.at_level(logging.DEBUG):
        state, spec = _triage_state()
        JevDecisionBackend().decide(spec, state)
        sdk_logger.debug("wire body=Authorization Bearer %s state=%r", KEY, state)
        sdk_logger.info("GET /v1/systemone <- 200")

    assert any(isinstance(f, _SdkWireBodyLogFilter) for f in sdk_logger.filters)
    assert all(KEY not in record.getMessage() for record in caplog.records)
    assert "wire body" not in caplog.text
    assert "GET /v1/systemone <- 200" in caplog.text
