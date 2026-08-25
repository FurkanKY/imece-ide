"""Immutable, provider-neutral models for the native semantic Reviewer."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from verification_runtime.models import VerificationReport
from workspace.base import normalize_workspace_relative_path
from workspace.errors import WorkspaceBoundaryError

from review_runtime.errors import ReviewInputError

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_MAX_ID_LENGTH = 128
_MAX_SUMMARY_CHARS = 4_000
_MAX_FINDING_MESSAGE_CHARS = 2_000
_MAX_FINDINGS = 32

_MAX_TASK_CHARS = 32_000
_MAX_PLAN_CHARS = 64_000
_MAX_DIFF_CHARS = 200_000


def _bounded_text(value: Any, field: str, *, max_chars: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ReviewInputError(f"{field} must be a string.")
    if "\x00" in value:
        raise ReviewInputError(f"{field} must not contain NUL characters.")
    if not allow_empty and not value.strip():
        raise ReviewInputError(f"{field} must be non-empty.")
    if len(value) > max_chars:
        raise ReviewInputError(f"{field} exceeds the maximum of {max_chars} characters.")
    return value


def _stable_id(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ID_LENGTH
        or _ID_RE.fullmatch(value) is None
    ):
        raise ReviewInputError(f"{field} must be a bounded stable identifier.")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ReviewInputError(f"{field} must be a lowercase SHA-256 hex digest.")
    return value


def _repository_path(value: str, field: str) -> str:
    try:
        normalized = normalize_workspace_relative_path(value, allow_root=False)
    except WorkspaceBoundaryError as exc:
        raise ReviewInputError(f"{field} must be a normalized workspace-relative file path.") from exc
    if normalized != value:
        raise ReviewInputError(f"{field} must already be normalized with forward slashes.")
    return normalized


class ReviewVerdict(StrEnum):
    APPROVED = "APPROVED"
    NEEDS_FIX = "NEEDS_FIX"


class ReviewSeverity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    severity: ReviewSeverity
    message: str
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.severity, ReviewSeverity):
            raise ReviewInputError("ReviewFinding.severity must be a ReviewSeverity.")
        message = _bounded_text(
            self.message, "ReviewFinding.message", max_chars=_MAX_FINDING_MESSAGE_CHARS
        )
        object.__setattr__(self, "message", message)

        path = self.path
        if path is not None:
            path = _repository_path(path, "ReviewFinding.path")
            object.__setattr__(self, "path", path)

        has_start = self.start_line is not None
        has_end = self.end_line is not None
        if has_start != has_end:
            raise ReviewInputError(
                "ReviewFinding.start_line and end_line must both be provided or both omitted."
            )
        if has_start:
            if path is None:
                raise ReviewInputError("ReviewFinding line information requires a path.")
            if (
                type(self.start_line) is not int
                or type(self.end_line) is not int
                or self.start_line < 1
                or self.end_line < self.start_line
            ):
                raise ReviewInputError("ReviewFinding line range is invalid.")


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """Model-supplied review outcome, before runtime provenance is attached."""

    verdict: ReviewVerdict
    summary: str
    findings: tuple[ReviewFinding, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, ReviewVerdict):
            raise ReviewInputError("ReviewDecision.verdict must be a ReviewVerdict.")
        summary = _bounded_text(self.summary, "ReviewDecision.summary", max_chars=_MAX_SUMMARY_CHARS)
        object.__setattr__(self, "summary", summary)

        findings = tuple(self.findings)
        if any(not isinstance(finding, ReviewFinding) for finding in findings):
            raise ReviewInputError("ReviewDecision.findings must contain only ReviewFinding values.")
        if len(findings) > _MAX_FINDINGS:
            raise ReviewInputError(f"ReviewDecision.findings exceeds the maximum of {_MAX_FINDINGS}.")
        object.__setattr__(self, "findings", findings)

        if self.verdict is ReviewVerdict.APPROVED and findings:
            raise ReviewInputError("APPROVED decisions must not contain findings.")
        if self.verdict is ReviewVerdict.NEEDS_FIX and not findings:
            raise ReviewInputError("NEEDS_FIX decisions must contain at least one finding.")


@dataclass(frozen=True, slots=True)
class ReviewReport:
    """Runtime-enriched review outcome; provenance fields are runtime-owned."""

    review_id: str
    verdict: ReviewVerdict
    summary: str
    findings: tuple[ReviewFinding, ...]
    repository_fingerprint: str
    diff_sha256: str
    verification_id: str | None = None
    verification_status: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "review_id", _stable_id(self.review_id, "ReviewReport.review_id"))
        if not isinstance(self.verdict, ReviewVerdict):
            raise ReviewInputError("ReviewReport.verdict must be a ReviewVerdict.")
        summary = _bounded_text(self.summary, "ReviewReport.summary", max_chars=_MAX_SUMMARY_CHARS)
        object.__setattr__(self, "summary", summary)

        findings = tuple(self.findings)
        if any(not isinstance(finding, ReviewFinding) for finding in findings):
            raise ReviewInputError("ReviewReport.findings must contain only ReviewFinding values.")
        if len(findings) > _MAX_FINDINGS:
            raise ReviewInputError(f"ReviewReport.findings exceeds the maximum of {_MAX_FINDINGS}.")
        object.__setattr__(self, "findings", findings)

        if self.verdict is ReviewVerdict.APPROVED and findings:
            raise ReviewInputError("APPROVED reports must not contain findings.")
        if self.verdict is ReviewVerdict.NEEDS_FIX and not findings:
            raise ReviewInputError("NEEDS_FIX reports must contain at least one finding.")

        object.__setattr__(
            self, "repository_fingerprint", _sha256(self.repository_fingerprint, "ReviewReport.repository_fingerprint")
        )
        object.__setattr__(self, "diff_sha256", _sha256(self.diff_sha256, "ReviewReport.diff_sha256"))

        if self.verification_id is not None:
            _stable_id(self.verification_id, "ReviewReport.verification_id")
        if self.verification_status is not None and not isinstance(self.verification_status, str):
            raise ReviewInputError("ReviewReport.verification_status must be a string or None.")


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    task: str
    diff: str
    plan: str | None = None
    verification_report: VerificationReport | None = None

    def __post_init__(self) -> None:
        task = _bounded_text(self.task, "ReviewRequest.task", max_chars=_MAX_TASK_CHARS)
        object.__setattr__(self, "task", task)

        diff = _bounded_text(self.diff, "ReviewRequest.diff", max_chars=_MAX_DIFF_CHARS, allow_empty=True)
        object.__setattr__(self, "diff", diff)

        if self.plan is not None:
            plan = _bounded_text(self.plan, "ReviewRequest.plan", max_chars=_MAX_PLAN_CHARS, allow_empty=True)
            object.__setattr__(self, "plan", plan)

        if self.verification_report is not None and not isinstance(
            self.verification_report, VerificationReport
        ):
            raise ReviewInputError("ReviewRequest.verification_report must be a VerificationReport or None.")

    @property
    def diff_sha256(self) -> str:
        return hashlib.sha256(self.diff.encode("utf-8")).hexdigest()


def new_review_id() -> str:
    return f"rev_{uuid.uuid4()}"


def validate_review_id(value: Any) -> str:
    return _stable_id(value, "review_id")
