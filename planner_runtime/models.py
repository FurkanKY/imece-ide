"""Immutable, provider-neutral models for the native semantic Planner.

Planner describes WHAT should be achieved (advisory, high-level implementation
objectives) — never low-level implementation mechanics, executable commands,
verification argv, or provider/model/executor routing. See
docs/superpowers/specs/2026-08-26-native-planner-design.md for the full
trust-boundary and routing-hint rationale.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from planner_runtime.errors import PlannerInputError

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_MAX_ID_LENGTH = 128

MAX_TASK_CHARS = 32_000
MAX_SUMMARY_CHARS = 4_000

MAX_PLAN_STEPS = 16
MAX_STEP_TITLE_CHARS = 300
MAX_STEP_OBJECTIVE_CHARS = 2_000

MAX_ACCEPTANCE_CRITERIA = 24
MAX_ACCEPTANCE_CRITERION_CHARS = 1_000

MAX_RISKS = 16
MAX_RISK_CHARS = 1_000


def _bounded_text(value: Any, field: str, *, max_chars: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise PlannerInputError(f"{field} must be a string.")
    if "\x00" in value:
        raise PlannerInputError(f"{field} must not contain NUL characters.")
    if not allow_empty and not value.strip():
        raise PlannerInputError(f"{field} must be non-empty.")
    if len(value) > max_chars:
        raise PlannerInputError(f"{field} exceeds the maximum of {max_chars} characters.")
    return value


def _stable_id(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ID_LENGTH
        or _ID_RE.fullmatch(value) is None
    ):
        raise PlannerInputError(f"{field} must be a bounded stable identifier.")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise PlannerInputError(f"{field} must be a lowercase SHA-256 hex digest.")
    return value


class TaskComplexity(StrEnum):
    """Advisory hint only — never used to authorize tools, execution, or terminal Run state.

    LOW: bounded/simple localized reasoning, limited interactions.
    MEDIUM: multiple coordinated changes or meaningful cross-module reasoning.
    HIGH: architectural/cross-cutting/high-risk changes.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class TaskScope(StrEnum):
    """Advisory hint only — never used to authorize tools, execution, or terminal Run state.

    LOCAL: one narrowly-contained component/area.
    MULTI_AREA: multiple related modules/components.
    REPOSITORY_WIDE: broad cross-cutting/repository-level work.
    """

    LOCAL = "LOCAL"
    MULTI_AREA = "MULTI_AREA"
    REPOSITORY_WIDE = "REPOSITORY_WIDE"


def _validate_steps(steps: Any, field: str) -> tuple["PlanStep", ...]:
    steps = tuple(steps)
    if not steps:
        raise PlannerInputError(f"{field} must contain at least one step.")
    if len(steps) > MAX_PLAN_STEPS:
        raise PlannerInputError(f"{field} exceeds the maximum of {MAX_PLAN_STEPS} steps.")
    if any(not isinstance(step, PlanStep) for step in steps):
        raise PlannerInputError(f"{field} must contain only PlanStep values.")
    return steps


def _validate_string_tuple(values: Any, field: str, *, max_items: int, max_chars: int) -> tuple[str, ...]:
    values = tuple(values)
    if len(values) > max_items:
        raise PlannerInputError(f"{field} exceeds the maximum of {max_items} items.")
    return tuple(_bounded_text(value, field, max_chars=max_chars) for value in values)


def _validate_task_profile(value: Any, field: str) -> "TaskProfile":
    if not isinstance(value, TaskProfile):
        raise PlannerInputError(f"{field} must be a TaskProfile.")
    return value


@dataclass(frozen=True, slots=True)
class PlanStep:
    title: str
    objective: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _bounded_text(self.title, "PlanStep.title", max_chars=MAX_STEP_TITLE_CHARS))
        object.__setattr__(
            self, "objective",
            _bounded_text(self.objective, "PlanStep.objective", max_chars=MAX_STEP_OBJECTIVE_CHARS),
        )


@dataclass(frozen=True, slots=True)
class TaskProfile:
    """Advisory routing hints only. See TaskComplexity/TaskScope docstrings.

    3I never derives authorization, tool access, process execution, or
    terminal Run state from this value — a future trusted RoutingPolicy may
    consume it, but 3I does not implement or call one.
    """

    complexity: TaskComplexity
    scope: TaskScope

    def __post_init__(self) -> None:
        if not isinstance(self.complexity, TaskComplexity):
            raise PlannerInputError("TaskProfile.complexity must be a TaskComplexity.")
        if not isinstance(self.scope, TaskScope):
            raise PlannerInputError("TaskProfile.scope must be a TaskScope.")


@dataclass(frozen=True, slots=True)
class ParsedPlanDecision:
    """Model-supplied plan outcome, before runtime provenance is attached."""

    summary: str
    steps: tuple[PlanStep, ...]
    acceptance_criteria: tuple[str, ...]
    risks: tuple[str, ...]
    task_profile: TaskProfile

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "summary", _bounded_text(self.summary, "ParsedPlanDecision.summary", max_chars=MAX_SUMMARY_CHARS)
        )
        object.__setattr__(self, "steps", _validate_steps(self.steps, "ParsedPlanDecision.steps"))
        object.__setattr__(
            self, "acceptance_criteria",
            _validate_string_tuple(
                self.acceptance_criteria, "ParsedPlanDecision.acceptance_criteria",
                max_items=MAX_ACCEPTANCE_CRITERIA, max_chars=MAX_ACCEPTANCE_CRITERION_CHARS,
            ),
        )
        object.__setattr__(
            self, "risks",
            _validate_string_tuple(
                self.risks, "ParsedPlanDecision.risks", max_items=MAX_RISKS, max_chars=MAX_RISK_CHARS,
            ),
        )
        object.__setattr__(
            self, "task_profile", _validate_task_profile(self.task_profile, "ParsedPlanDecision.task_profile")
        )


@dataclass(frozen=True, slots=True)
class PlanReport:
    """Runtime-enriched plan outcome; provenance fields are runtime-owned.

    plan_id / repository_fingerprint / task_sha256 are NEVER supplied by the
    model — PlannerRunner is the only code that constructs this type, and it
    always computes those three fields itself (see planner_runtime.runner).

    This report is itself derived LLM data: it is ADVISORY when consumed by
    a future Worker, never an authoritative instruction set, and it never
    contains executable commands or a VerificationPlan.
    """

    plan_id: str
    summary: str
    steps: tuple[PlanStep, ...]
    acceptance_criteria: tuple[str, ...]
    risks: tuple[str, ...]
    task_profile: TaskProfile
    repository_fingerprint: str
    task_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _stable_id(self.plan_id, "PlanReport.plan_id"))
        object.__setattr__(
            self, "summary", _bounded_text(self.summary, "PlanReport.summary", max_chars=MAX_SUMMARY_CHARS)
        )
        object.__setattr__(self, "steps", _validate_steps(self.steps, "PlanReport.steps"))
        object.__setattr__(
            self, "acceptance_criteria",
            _validate_string_tuple(
                self.acceptance_criteria, "PlanReport.acceptance_criteria",
                max_items=MAX_ACCEPTANCE_CRITERIA, max_chars=MAX_ACCEPTANCE_CRITERION_CHARS,
            ),
        )
        object.__setattr__(
            self, "risks",
            _validate_string_tuple(self.risks, "PlanReport.risks", max_items=MAX_RISKS, max_chars=MAX_RISK_CHARS),
        )
        object.__setattr__(self, "task_profile", _validate_task_profile(self.task_profile, "PlanReport.task_profile"))
        object.__setattr__(
            self, "repository_fingerprint", _sha256(self.repository_fingerprint, "PlanReport.repository_fingerprint")
        )
        object.__setattr__(self, "task_sha256", _sha256(self.task_sha256, "PlanReport.task_sha256"))


def new_plan_id() -> str:
    return f"plan_{uuid.uuid4()}"


def validate_plan_id(value: Any) -> str:
    return _stable_id(value, "plan_id")
