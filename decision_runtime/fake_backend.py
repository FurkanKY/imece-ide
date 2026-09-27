"""FakeDecisionBackend — a scripted DecisionPort for tests.

Never used in production wiring; exists purely so decision_runtime callers
and their consumers (e.g. fix_runtime) can be tested without any network
access or a real backend, per docs/JEV-DESIGN.md: "engine_factory wires it
when enabled; tests use a scripted fake backend."
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from decision_runtime.errors import DecisionBackendError
from decision_runtime.models import DecisionResult, DecisionSpec

_ScriptEntry = DecisionResult | BaseException | Callable[[DecisionSpec, Mapping[str, Any]], DecisionResult]


class FakeDecisionBackend:
    """Returns pre-scripted DecisionResults (or raises pre-scripted errors), in order.

    Each call to decide() consumes the next scripted entry. An entry may be:
      - a DecisionResult, returned as-is;
      - an exception instance, raised (simulates a backend failure so
        callers' design-rule-1 fallback path can be exercised);
      - a callable `(spec, state) -> DecisionResult`, invoked with the call's
        actual arguments (for scripting an answer that depends on state).
    Calling decide() more times than scripted entries were provided raises
    DecisionBackendError — a test bug, not a production fallback path.
    """

    def __init__(self, script: list[_ScriptEntry]) -> None:
        self._script = list(script)
        self._index = 0
        self.calls: list[tuple[DecisionSpec, Mapping[str, Any]]] = []

    @property
    def call_count(self) -> int:
        return self._index

    def decide(self, spec: DecisionSpec, state: Mapping[str, Any]) -> DecisionResult:
        self.calls.append((spec, state))
        if self._index >= len(self._script):
            raise DecisionBackendError(
                f"FakeDecisionBackend.decide() called {self._index + 1} times but only "
                f"{len(self._script)} scripted entries were provided."
            )
        entry = self._script[self._index]
        self._index += 1
        if isinstance(entry, BaseException):
            raise entry
        if isinstance(entry, DecisionResult):
            return entry
        return entry(spec, state)
