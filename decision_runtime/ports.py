"""DecisionPort — the seam every decision backend implements.

Kept deliberately tiny (one method) so the backends are interchangeable:
`RuleDecisionBackend` (deterministic fallback), `FakeDecisionBackend`
(tests) and — since Spike S1b — the real `JevDecisionBackend`
(typesafe-sdk, optional dependency; see docs/JEV-DESIGN.md "Architecture").
JevDecisionBackend imports its SDK and reads TYPESAFE_API_KEY lazily, only
inside decide(); nothing here imports typesafe-sdk or reads TYPESAFE_API_KEY.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from decision_runtime.models import DecisionResult, DecisionSpec


class DecisionPort(Protocol):
    """`decide(spec, state) -> DecisionResult`.

    Implementations MUST answer every question named in `spec.questions` and
    MUST echo `spec.decision_id`/`spec.question_set_version` back on the
    result unchanged. A backend that cannot answer confidently should still
    return a DecisionResult (with low confidence) rather than raising —
    raising is reserved for actual backend failure (timeout, API error,
    malformed response), which callers must catch and treat as a signal to
    fall back to RuleDecisionBackend (design rule 1).
    """

    def decide(self, spec: DecisionSpec, state: Mapping[str, Any]) -> DecisionResult: ...
