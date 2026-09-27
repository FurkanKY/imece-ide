"""Provider-independent decision contracts (docs/JEV-DESIGN.md "Architecture").

Mirrors TypeSafe's three question primitives (Choice / Score / Noul) closely
enough that a future JevDecisionBackend can answer a DecisionSpec built here
without any reshaping, while keeping every type here free of any dependency
on typesafe-sdk (see design rule 1: Jev is an optional accelerator, never a
hard dependency — decision_runtime must import cleanly with no API key and
no typesafe-sdk installed at all).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from decision_runtime.errors import DecisionInputError

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_ID_LENGTH = 128
_MAX_INSTRUCTIONS_CHARS = 4_000
_MAX_CRITERION_CHARS = 1_000
_MAX_CHOICE_OPTIONS = 255
_MAX_STATE_CHARS = 128_000  # generous local guard; real Jev's 32k-token state limit is a backend concern


def _id(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ID_LENGTH
        or _ID_RE.fullmatch(value) is None
    ):
        raise DecisionInputError(f"{field_name} must be a bounded stable identifier.")
    return value


def _bounded_text(value: Any, field_name: str, *, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionInputError(f"{field_name} must be a non-empty string.")
    if "\x00" in value:
        raise DecisionInputError(f"{field_name} must not contain NUL characters.")
    if len(value) > max_chars:
        raise DecisionInputError(f"{field_name} exceeds {max_chars} characters.")
    return value


class QuestionKind(StrEnum):
    """The three System One primitives — see docs/JEV-DESIGN.md "Verified facts"."""

    CHOICE = "choice"
    SCORE = "score"
    NOUL = "noul"


@dataclass(frozen=True, slots=True)
class Choice:
    """`Choice(instructions, criteria={label: description})` -> choice/probabilities/confidence."""

    instructions: str
    criteria: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instructions",
            _bounded_text(self.instructions, "Choice.instructions", max_chars=_MAX_INSTRUCTIONS_CHARS),
        )
        if not isinstance(self.criteria, Mapping) or not self.criteria:
            raise DecisionInputError("Choice.criteria must be a non-empty mapping.")
        if len(self.criteria) > _MAX_CHOICE_OPTIONS:
            raise DecisionInputError(f"Choice.criteria exceeds {_MAX_CHOICE_OPTIONS} options.")
        cleaned: dict[str, str] = {}
        for label, description in self.criteria.items():
            if not isinstance(label, str) or not label:
                raise DecisionInputError("Choice.criteria labels must be non-empty strings.")
            cleaned[label] = _bounded_text(
                description, f"Choice.criteria[{label!r}]", max_chars=_MAX_CRITERION_CHARS,
            )
        object.__setattr__(self, "criteria", cleaned)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(self.criteria.keys())


@dataclass(frozen=True, slots=True)
class Score:
    """`Score(instructions, criteria=[level0, level1, ...])` -> score/probabilities/confidence."""

    instructions: str
    criteria: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instructions",
            _bounded_text(self.instructions, "Score.instructions", max_chars=_MAX_INSTRUCTIONS_CHARS),
        )
        levels = tuple(self.criteria)
        if len(levels) < 2:
            raise DecisionInputError("Score.criteria must list at least two levels.")
        object.__setattr__(
            self, "criteria",
            tuple(
                _bounded_text(level, f"Score.criteria[{index}]", max_chars=_MAX_CRITERION_CHARS)
                for index, level in enumerate(levels)
            ),
        )

    @property
    def max_score(self) -> int:
        return len(self.criteria) - 1


@dataclass(frozen=True, slots=True)
class Noul:
    """`Noul(instructions, criteria={true, false})` -> noul in [0, 1], no separate confidence."""

    instructions: str
    criteria: Mapping[str, str] = field(
        default_factory=lambda: {"true": "The statement holds.", "false": "The statement does not hold."}
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instructions",
            _bounded_text(self.instructions, "Noul.instructions", max_chars=_MAX_INSTRUCTIONS_CHARS),
        )
        if set(self.criteria.keys()) != {"true", "false"}:
            raise DecisionInputError("Noul.criteria must have exactly the keys 'true' and 'false'.")
        object.__setattr__(
            self, "criteria",
            {
                key: _bounded_text(value, f"Noul.criteria[{key!r}]", max_chars=_MAX_CRITERION_CHARS)
                for key, value in self.criteria.items()
            },
        )


Question = Choice | Score | Noul


def _question_kind(question: Question) -> QuestionKind:
    if isinstance(question, Choice):
        return QuestionKind.CHOICE
    if isinstance(question, Score):
        return QuestionKind.SCORE
    if isinstance(question, Noul):
        return QuestionKind.NOUL
    raise DecisionInputError(f"Unsupported question type: {type(question)!r}")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class DecisionSpec:
    """One decision point: a stable id, a versioned question set, and the questions.

    `question_set_version` is recorded on every DecisionResult/decision.made
    event (design rule 5) so historical decisions remain interpretable after
    the question wording or option set changes.
    """

    decision_id: str
    question_set_version: str
    questions: Mapping[str, Question]

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _id(self.decision_id, "DecisionSpec.decision_id"))
        object.__setattr__(
            self, "question_set_version",
            _id(self.question_set_version, "DecisionSpec.question_set_version"),
        )
        if not isinstance(self.questions, Mapping) or not self.questions:
            raise DecisionInputError("DecisionSpec.questions must be a non-empty mapping.")
        cleaned: dict[str, Question] = {}
        for name, question in self.questions.items():
            if not isinstance(name, str) or not name:
                raise DecisionInputError("DecisionSpec.questions keys must be non-empty strings.")
            _question_kind(question)  # validates type
            cleaned[name] = question
        object.__setattr__(self, "questions", cleaned)


def validate_state(state: Any) -> dict[str, Any]:
    """Bound and shallow-validate the `state` dict handed to DecisionPort.decide.

    Design rule 3/4: code does the math and only filtered, small state is
    sent — this function only enforces the outer size/shape bound; callers
    remain responsible for actually filtering fields down to what a question
    needs and for never including secrets.
    """
    if not isinstance(state, dict):
        raise DecisionInputError("state must be a dict.")
    import json

    try:
        rendered = json.dumps(state, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        raise DecisionInputError("state must be JSON-serializable.") from exc
    if len(rendered) > _MAX_STATE_CHARS:
        raise DecisionInputError(f"state exceeds {_MAX_STATE_CHARS} characters once serialized.")
    return state


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float

    def __post_init__(self) -> None:
        if not isinstance(self.choice, str) or not self.choice:
            raise DecisionInputError("ChoiceAnswer.choice must be a non-empty string.")
        _validate_probabilities(self.probabilities)
        if self.choice not in self.probabilities:
            raise DecisionInputError("ChoiceAnswer.choice must be a key of probabilities.")
        _validate_unit_interval(self.confidence, "ChoiceAnswer.confidence")


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    score: float
    probabilities: Mapping[str, float]
    confidence: float

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise DecisionInputError("ScoreAnswer.score must be a number.")
        _validate_probabilities(self.probabilities)
        _validate_unit_interval(self.confidence, "ScoreAnswer.confidence")


@dataclass(frozen=True, slots=True)
class NoulAnswer:
    """No separate confidence — see docs/JEV-DESIGN.md: "noul in 0-1, no separate confidence"."""

    noul: float

    def __post_init__(self) -> None:
        _validate_unit_interval(self.noul, "NoulAnswer.noul")


Answer = ChoiceAnswer | ScoreAnswer | NoulAnswer


def _validate_unit_interval(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionInputError(f"{field_name} must be a number.")
    if not (0.0 <= float(value) <= 1.0):
        raise DecisionInputError(f"{field_name} must be in [0, 1].")


def _validate_probabilities(probabilities: Any) -> None:
    if not isinstance(probabilities, Mapping) or not probabilities:
        raise DecisionInputError("probabilities must be a non-empty mapping.")
    total = 0.0
    for label, value in probabilities.items():
        if not isinstance(label, str) or not label:
            raise DecisionInputError("probabilities keys must be non-empty strings.")
        _validate_unit_interval(value, f"probabilities[{label!r}]")
        total += float(value)
    if abs(total - 1.0) > 1e-6:
        raise DecisionInputError(f"probabilities must sum to 1.0 (got {total!r}).")


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """One decide() call's full, auditable outcome (design rule 5)."""

    decision_id: str
    question_set_version: str
    answers: Mapping[str, Answer]
    backend: str
    model_version: str
    latency_ms: int
    prompt_tokens: int
    fallback_used: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _id(self.decision_id, "DecisionResult.decision_id"))
        object.__setattr__(
            self, "question_set_version",
            _id(self.question_set_version, "DecisionResult.question_set_version"),
        )
        if not isinstance(self.answers, Mapping) or not self.answers:
            raise DecisionInputError("DecisionResult.answers must be a non-empty mapping.")
        for name, answer in self.answers.items():
            if not isinstance(name, str) or not name:
                raise DecisionInputError("DecisionResult.answers keys must be non-empty strings.")
            if not isinstance(answer, (ChoiceAnswer, ScoreAnswer, NoulAnswer)):
                raise DecisionInputError(f"DecisionResult.answers[{name!r}] has an unsupported type.")
        if not isinstance(self.backend, str) or not self.backend:
            raise DecisionInputError("DecisionResult.backend must be a non-empty string.")
        if not isinstance(self.model_version, str) or not self.model_version:
            raise DecisionInputError("DecisionResult.model_version must be a non-empty string.")
        if type(self.latency_ms) is not int or self.latency_ms < 0:
            raise DecisionInputError("DecisionResult.latency_ms must be a non-negative integer.")
        if type(self.prompt_tokens) is not int or self.prompt_tokens < 0:
            raise DecisionInputError("DecisionResult.prompt_tokens must be a non-negative integer.")
        if type(self.fallback_used) is not bool:
            raise DecisionInputError("DecisionResult.fallback_used must be a boolean.")

    def confidence_of(self, question_name: str) -> float:
        """Confidence for one answer — Noul has no separate confidence, so its
        |2p-1| distance from 0.5 (the same shape TypeSafe uses for a 2-option
        Choice: `(3*pmax - 1)/2` reduces to `2*pmax - 1` at n=2) is used."""
        answer = self.answers[question_name]
        if isinstance(answer, NoulAnswer):
            return abs(2.0 * answer.noul - 1.0)
        return answer.confidence
