"""Strict JSON parser for the Planner's final-answer protocol.

The model's final answer MUST be exactly one JSON object — no Markdown
fences, no prose before/after, no duplicate keys, no NaN/Infinity, no
unknown keys, no top-level array. Malformed or domain-invalid output NEVER
becomes a PlanReport; it always raises a typed PlannerProtocolError.
"""

from __future__ import annotations

import json
from typing import Any

from planner_runtime.errors import PlannerInputError, PlannerProtocolError
from planner_runtime.models import ParsedPlanDecision, PlanStep, TaskComplexity, TaskScope, TaskProfile

MAX_MODEL_OUTPUT_CHARS = 32_000

_TOP_LEVEL_KEYS = {"summary", "steps", "acceptance_criteria", "risks", "task_profile"}
_STEP_KEYS = {"title", "objective"}
_TASK_PROFILE_KEYS = {"complexity", "scope"}


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise PlannerProtocolError(f"Duplicate JSON key in plan output: {key!r}")
        seen[key] = value
    return seen


def _reject_constant(value: str) -> float:
    raise PlannerProtocolError(f"Non-finite JSON constant is not allowed in plan output: {value}")


def _strict_load(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except PlannerProtocolError:
        raise
    except (ValueError, RecursionError) as exc:
        raise PlannerProtocolError(f"Plan output is not valid JSON: {exc}") from exc


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise PlannerProtocolError(f"{field} must be a string.")
    return value


def _step_from_object(obj: Any) -> PlanStep:
    if not isinstance(obj, dict):
        raise PlannerProtocolError("Each plan step must be a JSON object.")
    unknown = set(obj) - _STEP_KEYS
    if unknown:
        raise PlannerProtocolError(f"Unknown plan step key(s): {sorted(unknown)}")
    if "title" not in obj or "objective" not in obj:
        raise PlannerProtocolError("Each plan step requires 'title' and 'objective'.")
    title = _require_string(obj["title"], "Plan step 'title'")
    objective = _require_string(obj["objective"], "Plan step 'objective'")
    try:
        return PlanStep(title=title, objective=objective)
    except PlannerInputError as exc:
        raise PlannerProtocolError(f"Invalid plan step: {exc}") from exc


def _string_list(raw: Any, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise PlannerProtocolError(f"'{field}' must be a JSON array.")
    result = []
    for entry in raw:
        result.append(_require_string(entry, f"Each '{field}' entry"))
    return tuple(result)


def _task_profile_from_object(obj: Any) -> TaskProfile:
    if not isinstance(obj, dict):
        raise PlannerProtocolError("'task_profile' must be a JSON object.")
    unknown = set(obj) - _TASK_PROFILE_KEYS
    if unknown:
        raise PlannerProtocolError(f"Unknown task_profile key(s): {sorted(unknown)}")
    if "complexity" not in obj or "scope" not in obj:
        raise PlannerProtocolError("'task_profile' requires 'complexity' and 'scope'.")
    raw_complexity = _require_string(obj["complexity"], "task_profile 'complexity'")
    raw_scope = _require_string(obj["scope"], "task_profile 'scope'")
    try:
        complexity = TaskComplexity(raw_complexity)
    except ValueError as exc:
        raise PlannerProtocolError(f"Invalid task_profile complexity: {raw_complexity!r}") from exc
    try:
        scope = TaskScope(raw_scope)
    except ValueError as exc:
        raise PlannerProtocolError(f"Invalid task_profile scope: {raw_scope!r}") from exc
    try:
        return TaskProfile(complexity=complexity, scope=scope)
    except PlannerInputError as exc:
        raise PlannerProtocolError(f"Invalid task_profile: {exc}") from exc


def parse_plan_decision(text: str) -> ParsedPlanDecision:
    """Parse the Planner's final answer into a validated ParsedPlanDecision.

    Raises PlannerProtocolError for anything that is not exactly one strict
    JSON object matching the plan output contract — including domain-invalid
    values (e.g. an empty steps array, an oversized field, an unknown key).
    """
    if not isinstance(text, str):
        raise PlannerProtocolError("Plan output must be a string.")
    if len(text) > MAX_MODEL_OUTPUT_CHARS:
        raise PlannerProtocolError(
            f"Plan output exceeds the maximum of {MAX_MODEL_OUTPUT_CHARS} characters."
        )
    stripped = text.strip()
    if not stripped:
        raise PlannerProtocolError("Plan output is empty.")
    if stripped.startswith("```"):
        raise PlannerProtocolError("Plan output must not use Markdown code fences.")

    obj = _strict_load(stripped)
    if not isinstance(obj, dict):
        raise PlannerProtocolError("Plan output must be a single top-level JSON object.")

    unknown = set(obj) - _TOP_LEVEL_KEYS
    if unknown:
        raise PlannerProtocolError(f"Unknown top-level key(s): {sorted(unknown)}")
    missing = _TOP_LEVEL_KEYS - set(obj)
    if missing:
        raise PlannerProtocolError(f"Plan output is missing required key(s): {sorted(missing)}")

    summary = _require_string(obj["summary"], "'summary'")

    raw_steps = obj["steps"]
    if not isinstance(raw_steps, list):
        raise PlannerProtocolError("'steps' must be a JSON array.")
    steps = tuple(_step_from_object(entry) for entry in raw_steps)

    acceptance_criteria = _string_list(obj["acceptance_criteria"], "acceptance_criteria")
    risks = _string_list(obj["risks"], "risks")
    task_profile = _task_profile_from_object(obj["task_profile"])

    try:
        return ParsedPlanDecision(
            summary=summary,
            steps=steps,
            acceptance_criteria=acceptance_criteria,
            risks=risks,
            task_profile=task_profile,
        )
    except PlannerInputError as exc:
        raise PlannerProtocolError(f"Invalid plan decision: {exc}") from exc
