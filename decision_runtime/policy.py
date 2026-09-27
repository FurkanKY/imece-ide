"""DecisionPolicy — confidence thresholds shared by every decision point.

docs/JEV-DESIGN.md: "Docs recommend risk-scaled thresholds: e.g. >0.9 act,
middle band confirm, <0.5 escalate; destructive actions need higher
thresholds." This module owns exactly that banding, with sane defaults, and
nothing else — per-decision action MAPPING (which failure_kind maps to which
pipeline action) lives next to the decision it belongs to (see
decision_runtime.triage), not here, because that mapping is decision-
specific while the confidence bands are not.

Thresholds live in a plain dict so a future eval (S1b) can tune them without
touching code — see docs/JEV-DESIGN.md "DecisionPolicy per decision:
thresholds ... live in config so the eval can tune them."
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from decision_runtime.errors import DecisionInputError

# Sane defaults per docs/JEV-DESIGN.md's confidence guidance.
DEFAULT_ACT_THRESHOLD = 0.9
DEFAULT_ESCALATE_THRESHOLD = 0.5


class ConfidenceBand(StrEnum):
    ACT = "act"
    CONFIRM = "confirm"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    """Per-decision confidence thresholds. `act_threshold > escalate_threshold`."""

    act_threshold: float = DEFAULT_ACT_THRESHOLD
    escalate_threshold: float = DEFAULT_ESCALATE_THRESHOLD

    def __post_init__(self) -> None:
        for name, value in (("act_threshold", self.act_threshold), ("escalate_threshold", self.escalate_threshold)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DecisionInputError(f"DecisionPolicy.{name} must be a number.")
            if not (0.0 <= float(value) <= 1.0):
                raise DecisionInputError(f"DecisionPolicy.{name} must be in [0, 1].")
        if self.escalate_threshold > self.act_threshold:
            raise DecisionInputError(
                "DecisionPolicy.escalate_threshold must not exceed act_threshold."
            )

    def band(self, confidence: float) -> ConfidenceBand:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise DecisionInputError("confidence must be a number.")
        if confidence >= self.act_threshold:
            return ConfidenceBand.ACT
        if confidence < self.escalate_threshold:
            return ConfidenceBand.ESCALATE
        return ConfidenceBand.CONFIRM

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None, *, key: str) -> "DecisionPolicy":
        """Load `config[key]` as `{"act_threshold": ..., "escalate_threshold": ...}`.

        Missing config, a missing key, or a missing field each fall back to
        the class default for that field — a partially-specified override
        (e.g. only `act_threshold`) is honored field-by-field.
        """
        section = {}
        if config is not None:
            candidate = config.get(key)
            if isinstance(candidate, Mapping):
                section = candidate
        return cls(
            act_threshold=section.get("act_threshold", DEFAULT_ACT_THRESHOLD),
            escalate_threshold=section.get("escalate_threshold", DEFAULT_ESCALATE_THRESHOLD),
        )
