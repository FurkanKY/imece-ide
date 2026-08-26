"""Immutable, provider-neutral models for the bounded native Fix Loop."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from review_runtime.models import ReviewReport, ReviewVerdict
from verification_runtime.models import VerificationPlan, VerificationReport, VerificationStatus

from fix_runtime.errors import FixLoopInputError

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_ID_LENGTH = 128
_MAX_TASK_CHARS = 32_000
_MAX_PLAN_CHARS = 64_000

DEFAULT_MAX_FIX_ATTEMPTS = 2
MIN_MAX_FIX_ATTEMPTS = 1
MAX_MAX_FIX_ATTEMPTS = 5


def _stable_id(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ID_LENGTH
        or _ID_RE.fullmatch(value) is None
    ):
        raise FixLoopInputError(f"{field} must be a bounded stable identifier.")
    return value


def _bounded_text(value: Any, field: str, *, max_chars: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise FixLoopInputError(f"{field} must be a string.")
    if "\x00" in value:
        raise FixLoopInputError(f"{field} must not contain NUL characters.")
    if not allow_empty and not value.strip():
        raise FixLoopInputError(f"{field} must be non-empty.")
    if len(value) > max_chars:
        raise FixLoopInputError(f"{field} exceeds the maximum of {max_chars} characters.")
    return value


class FixTriggerKind(StrEnum):
    VERIFICATION_FAIL = "verification_fail"
    REVIEW_NEEDS_FIX = "review_needs_fix"


class FixLoopStatus(StrEnum):
    COMPLETED = "completed"
    EXHAUSTED = "exhausted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FixTrigger:
    """Evidence that makes a bounded fix attempt eligible to run.

    Only two shapes are valid — deterministic Verification always wins:
    a VERIFICATION_FAIL trigger carries no review evidence at all, and a
    REVIEW_NEEDS_FIX trigger requires a PASSing verification whose identity
    the review itself already references (review provenance, not trust).
    """

    kind: FixTriggerKind
    verification_report: VerificationReport
    review_report: ReviewReport | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FixTriggerKind):
            raise FixLoopInputError("FixTrigger.kind must be a FixTriggerKind.")
        if not isinstance(self.verification_report, VerificationReport):
            raise FixLoopInputError("FixTrigger.verification_report must be a VerificationReport.")

        if self.kind is FixTriggerKind.VERIFICATION_FAIL:
            if self.verification_report.status is not VerificationStatus.FAIL:
                raise FixLoopInputError(
                    "VERIFICATION_FAIL trigger requires VerificationReport.status == FAIL."
                )
            if self.review_report is not None:
                raise FixLoopInputError("VERIFICATION_FAIL trigger must not carry a review_report.")
            return

        # REVIEW_NEEDS_FIX
        if self.verification_report.status is not VerificationStatus.PASS:
            raise FixLoopInputError(
                "REVIEW_NEEDS_FIX trigger requires VerificationReport.status == PASS."
            )
        if not isinstance(self.review_report, ReviewReport):
            raise FixLoopInputError("REVIEW_NEEDS_FIX trigger requires a ReviewReport.")
        if self.review_report.verdict is not ReviewVerdict.NEEDS_FIX:
            raise FixLoopInputError("REVIEW_NEEDS_FIX trigger requires ReviewReport.verdict == NEEDS_FIX.")
        if self.review_report.verification_id != self.verification_report.verification_id:
            raise FixLoopInputError(
                "REVIEW_NEEDS_FIX trigger review_report.verification_id must match verification_report.verification_id."
            )
        if self.review_report.verification_status != "pass":
            raise FixLoopInputError(
                "REVIEW_NEEDS_FIX trigger review_report.verification_status must be 'pass'."
            )


@dataclass(frozen=True, slots=True)
class FixWorkerRequest:
    """The bounded fix instruction actually handed to the Worker port.

    `rendered_input` IS the trust-boundary-enforced string produced by
    fix_runtime.prompt.render_fix_worker_input() for this exact attempt —
    FixLoopRunner never renders it merely for validation and then lets an
    adapter reconstruct its own prompt from the raw trigger. A concrete
    WorkerAttemptRunner MUST treat `rendered_input` as the actual fix
    instruction/input it feeds to the underlying harness.
    """

    task: str
    trigger: FixTrigger
    attempt_index: int
    rendered_input: str
    plan: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task", _bounded_text(self.task, "FixWorkerRequest.task", max_chars=_MAX_TASK_CHARS))
        if not isinstance(self.trigger, FixTrigger):
            raise FixLoopInputError("FixWorkerRequest.trigger must be a FixTrigger.")
        if type(self.attempt_index) is not int or self.attempt_index < 1:
            raise FixLoopInputError("FixWorkerRequest.attempt_index must be a positive integer.")
        from fix_runtime.prompt import MAX_FIX_INPUT_CHARS  # lazy: prompt imports models

        object.__setattr__(
            self, "rendered_input",
            _bounded_text(
                self.rendered_input, "FixWorkerRequest.rendered_input", max_chars=MAX_FIX_INPUT_CHARS,
            ),
        )
        if self.plan is not None:
            object.__setattr__(
                self, "plan",
                _bounded_text(self.plan, "FixWorkerRequest.plan", max_chars=_MAX_PLAN_CHARS, allow_empty=True),
            )


@dataclass(frozen=True, slots=True)
class FixLoopRequest:
    task: str
    trigger: FixTrigger
    verification_plan: VerificationPlan
    plan: str | None = None
    max_fix_attempts: int = DEFAULT_MAX_FIX_ATTEMPTS

    def __post_init__(self) -> None:
        object.__setattr__(self, "task", _bounded_text(self.task, "FixLoopRequest.task", max_chars=_MAX_TASK_CHARS))
        if not isinstance(self.trigger, FixTrigger):
            raise FixLoopInputError("FixLoopRequest.trigger must be a FixTrigger.")
        if not isinstance(self.verification_plan, VerificationPlan):
            raise FixLoopInputError("FixLoopRequest.verification_plan must be a VerificationPlan.")
        if self.plan is not None:
            object.__setattr__(
                self, "plan",
                _bounded_text(self.plan, "FixLoopRequest.plan", max_chars=_MAX_PLAN_CHARS, allow_empty=True),
            )
        if (
            isinstance(self.max_fix_attempts, bool)
            or not isinstance(self.max_fix_attempts, int)
            or not (MIN_MAX_FIX_ATTEMPTS <= self.max_fix_attempts <= MAX_MAX_FIX_ATTEMPTS)
        ):
            raise FixLoopInputError(
                f"FixLoopRequest.max_fix_attempts must be an integer in "
                f"[{MIN_MAX_FIX_ATTEMPTS}, {MAX_MAX_FIX_ATTEMPTS}]."
            )


@dataclass(frozen=True, slots=True)
class FixAttemptResult:
    fix_attempt_id: str
    attempt_index: int
    worker_execution_id: str
    changed: bool
    before_diff_sha256: str
    after_diff_sha256: str

    def __post_init__(self) -> None:
        _stable_id(self.fix_attempt_id, "FixAttemptResult.fix_attempt_id")
        _stable_id(self.worker_execution_id, "FixAttemptResult.worker_execution_id")
        if type(self.attempt_index) is not int or self.attempt_index < 1:
            raise FixLoopInputError("FixAttemptResult.attempt_index must be a positive integer.")
        if type(self.changed) is not bool:
            raise FixLoopInputError("FixAttemptResult.changed must be a boolean.")


@dataclass(frozen=True, slots=True)
class FixLoopReport:
    fix_loop_id: str
    status: FixLoopStatus
    attempts_used: int
    reason: str
    final_execution_id: str | None = None
    verification_report: VerificationReport | None = None
    review_report: ReviewReport | None = None
    diff_sha256: str | None = None

    def __post_init__(self) -> None:
        _stable_id(self.fix_loop_id, "FixLoopReport.fix_loop_id")
        if not isinstance(self.status, FixLoopStatus):
            raise FixLoopInputError("FixLoopReport.status must be a FixLoopStatus.")
        if type(self.attempts_used) is not int or self.attempts_used < 0:
            raise FixLoopInputError("FixLoopReport.attempts_used must be a non-negative integer.")
        if not isinstance(self.reason, str) or not self.reason:
            raise FixLoopInputError("FixLoopReport.reason must be a non-empty string.")


def new_fix_loop_id() -> str:
    return f"fix_{uuid.uuid4()}"


def new_fix_attempt_id() -> str:
    return f"fixatt_{uuid.uuid4()}"


def new_fix_execution_id() -> str:
    return f"exec_fix_{uuid.uuid4()}"


def validate_fix_loop_id(value: Any) -> str:
    return _stable_id(value, "fix_loop_id")
