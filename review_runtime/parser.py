"""Strict JSON parser for the Reviewer's final-answer protocol.

The model's final answer MUST be exactly one JSON object — no Markdown
fences, no prose before/after, no duplicate keys, no NaN/Infinity, no
unknown keys. Malformed or domain-invalid output is NEVER interpreted as
APPROVED; it always raises a typed ReviewProtocolError.
"""

from __future__ import annotations

import json
from typing import Any

from review_runtime.errors import ReviewInputError, ReviewProtocolError
from review_runtime.models import ReviewDecision, ReviewFinding, ReviewSeverity, ReviewVerdict

MAX_MODEL_OUTPUT_CHARS = 20_000

_TOP_LEVEL_KEYS = {"verdict", "summary", "findings"}
_FINDING_KEYS = {"severity", "message", "path", "start_line", "end_line"}


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ReviewProtocolError(f"Duplicate JSON key in review output: {key!r}")
        seen[key] = value
    return seen


def _reject_constant(value: str) -> float:
    raise ReviewProtocolError(f"Non-finite JSON constant is not allowed in review output: {value}")


def _strict_load(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except ReviewProtocolError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ReviewProtocolError(f"Review output is not valid JSON: {exc}") from exc


def _finding_from_object(obj: dict[str, Any]) -> ReviewFinding:
    if not isinstance(obj, dict):
        raise ReviewProtocolError("Each review finding must be a JSON object.")
    unknown = set(obj) - _FINDING_KEYS
    if unknown:
        raise ReviewProtocolError(f"Unknown finding key(s): {sorted(unknown)}")
    if "severity" not in obj or "message" not in obj:
        raise ReviewProtocolError("Each review finding requires 'severity' and 'message'.")
    raw_severity = obj["severity"]
    if not isinstance(raw_severity, str):
        raise ReviewProtocolError("Finding 'severity' must be a string.")
    try:
        severity = ReviewSeverity(raw_severity)
    except ValueError as exc:
        raise ReviewProtocolError(f"Invalid finding severity: {raw_severity!r}") from exc
    message = obj["message"]
    if not isinstance(message, str):
        raise ReviewProtocolError("Finding 'message' must be a string.")
    path = obj.get("path")
    if path is not None and not isinstance(path, str):
        raise ReviewProtocolError("Finding 'path' must be a string or null.")
    start_line = obj.get("start_line")
    end_line = obj.get("end_line")
    for name, value in (("start_line", start_line), ("end_line", end_line)):
        if value is not None and (type(value) is not int):
            raise ReviewProtocolError(f"Finding '{name}' must be an integer or null.")
    try:
        return ReviewFinding(
            severity=severity,
            message=message,
            path=path,
            start_line=start_line,
            end_line=end_line,
        )
    except ReviewInputError as exc:
        raise ReviewProtocolError(f"Invalid review finding: {exc}") from exc


def parse_review_decision(text: str) -> ReviewDecision:
    """Parse the Reviewer's final answer into a validated ReviewDecision.

    Raises ReviewProtocolError for anything that is not exactly one strict
    JSON object matching the review contract — including domain-invalid
    combinations (e.g. APPROVED with findings).
    """
    if not isinstance(text, str):
        raise ReviewProtocolError("Review output must be a string.")
    if len(text) > MAX_MODEL_OUTPUT_CHARS:
        raise ReviewProtocolError(
            f"Review output exceeds the maximum of {MAX_MODEL_OUTPUT_CHARS} characters."
        )
    stripped = text.strip()
    if not stripped:
        raise ReviewProtocolError("Review output is empty.")
    if stripped.startswith("```"):
        raise ReviewProtocolError("Review output must not use Markdown code fences.")

    obj = _strict_load(stripped)
    if not isinstance(obj, dict):
        raise ReviewProtocolError("Review output must be a single top-level JSON object.")

    unknown = set(obj) - _TOP_LEVEL_KEYS
    if unknown:
        raise ReviewProtocolError(f"Unknown top-level key(s): {sorted(unknown)}")
    if "verdict" not in obj or "summary" not in obj:
        raise ReviewProtocolError("Review output requires 'verdict' and 'summary'.")

    raw_verdict = obj["verdict"]
    if not isinstance(raw_verdict, str):
        raise ReviewProtocolError("'verdict' must be a string.")
    try:
        verdict = ReviewVerdict(raw_verdict)
    except ValueError as exc:
        raise ReviewProtocolError(f"Invalid review verdict: {raw_verdict!r}") from exc

    summary = obj["summary"]
    if not isinstance(summary, str):
        raise ReviewProtocolError("'summary' must be a string.")

    raw_findings = obj.get("findings", [])
    if not isinstance(raw_findings, list):
        raise ReviewProtocolError("'findings' must be a JSON array.")
    findings = tuple(_finding_from_object(entry) for entry in raw_findings)

    try:
        return ReviewDecision(verdict=verdict, summary=summary, findings=findings)
    except ReviewInputError as exc:
        raise ReviewProtocolError(f"Invalid review decision: {exc}") from exc
