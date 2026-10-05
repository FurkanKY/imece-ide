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
_MAX_FEEDBACK_CHARS = 8_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_MAX_FIX_ATTEMPTS = 2
MIN_MAX_FIX_ATTEMPTS = 1
MAX_MAX_FIX_ATTEMPTS = 5
_MAX_RENDER_PATHS = 256
_MAX_RENDER_PATH_CHARS = 1_024
_MAX_RENDER_CLASSIFICATION_CHARS = 128


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


def _validate_diff_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise FixLoopInputError(f"{field} must be a lowercase SHA-256 hex digest.")
    return value


class FixTriggerKind(StrEnum):
    VERIFICATION_FAIL = "verification_fail"
    REVIEW_NEEDS_FIX = "review_needs_fix"
    # F2 (follow-up on a proposal): the user typed a follow-up instruction
    # while a proposal was pending -- there is no fresh Verification/Review
    # evidence yet (the run is resuming from WAITING_USER), only the user's
    # own feedback text and the diff it refers to.
    USER_FEEDBACK = "user_feedback"


class FixLoopStatus(StrEnum):
    COMPLETED = "completed"
    EXHAUSTED = "exhausted"
    FAILED = "failed"
    # Jev System One decision layer (docs/JEV-DESIGN.md Spike S1): the decision
    # gate deliberately stopped the loop WITHOUT settling the Run itself --
    # unlike every other terminal status, FixLoopRunner does NOT call
    # completion_gate for this one (see FixLoopRunner._run_attempts's decision-
    # gate block). The Run is left RUNNING; the caller (pipeline_runtime.
    # PipelineRunner) is responsible for finishing settlement itself, exactly
    # mirroring its own existing "no verification plan detected" advisory-
    # review path. `reason` distinguishes WHY: "needs_user_environment"
    # (missing dependency/tooling) or "pre_existing_failure" (the same check
    # also fails on the baseline).
    NEEDS_USER = "needs_user"


@dataclass(frozen=True, slots=True)
class FixTrigger:
    """Evidence that makes a bounded fix attempt eligible to run.

    Three shapes are valid — deterministic Verification always wins over a
    human's review feedback, and a human's follow-up wins only because there
    is nothing else to arbitrate against (it starts a fresh loop from
    WAITING_USER, not a continuation of stale evidence):

    - VERIFICATION_FAIL carries no review evidence at all.
    - REVIEW_NEEDS_FIX requires a PASSing verification whose identity the
      review itself already references (review provenance, not trust).
    - USER_FEEDBACK (F2, follow-up on a proposal) carries the user's own
      feedback text plus the diff_sha256 it refers to (validated by
      FixLoopRunner against the CURRENT workspace change set, exactly like
      REVIEW_NEEDS_FIX's diff check) instead of fresh Verification/Review
      evidence — there is none yet, the run is resuming from WAITING_USER.
      It MAY carry the last known verification_report/review_report purely
      as informational context (never as a gate: the fields aren't cross-
      checked against each other or against diff_sha256 the way
      REVIEW_NEEDS_FIX's are).
    """

    kind: FixTriggerKind
    verification_report: VerificationReport | None
    review_report: ReviewReport | None = None
    feedback: str | None = None
    diff_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FixTriggerKind):
            raise FixLoopInputError("FixTrigger.kind must be a FixTriggerKind.")
        if self.verification_report is not None and not isinstance(
            self.verification_report, VerificationReport
        ):
            raise FixLoopInputError("FixTrigger.verification_report must be a VerificationReport or None.")
        if self.review_report is not None and not isinstance(self.review_report, ReviewReport):
            raise FixLoopInputError("FixTrigger.review_report must be a ReviewReport or None.")

        if self.kind is FixTriggerKind.VERIFICATION_FAIL:
            if not isinstance(self.verification_report, VerificationReport):
                raise FixLoopInputError("VERIFICATION_FAIL trigger requires a VerificationReport.")
            if self.verification_report.status is not VerificationStatus.FAIL:
                raise FixLoopInputError(
                    "VERIFICATION_FAIL trigger requires VerificationReport.status == FAIL."
                )
            if self.review_report is not None:
                raise FixLoopInputError("VERIFICATION_FAIL trigger must not carry a review_report.")
            if self.feedback is not None or self.diff_sha256 is not None:
                raise FixLoopInputError("VERIFICATION_FAIL trigger must not carry feedback/diff_sha256.")
            return

        if self.kind is FixTriggerKind.REVIEW_NEEDS_FIX:
            if not isinstance(self.verification_report, VerificationReport):
                raise FixLoopInputError("REVIEW_NEEDS_FIX trigger requires a VerificationReport.")
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
            if self.feedback is not None or self.diff_sha256 is not None:
                raise FixLoopInputError("REVIEW_NEEDS_FIX trigger must not carry feedback/diff_sha256.")
            return

        # USER_FEEDBACK (F2)
        object.__setattr__(
            self, "feedback",
            _bounded_text(self.feedback, "FixTrigger.feedback", max_chars=_MAX_FEEDBACK_CHARS),
        )
        object.__setattr__(
            self, "diff_sha256",
            _validate_diff_sha256(self.diff_sha256, "FixTrigger.diff_sha256"),
        )


@dataclass(frozen=True, slots=True)
class InitialWorkerRenderContext:
    """Detached, bounded recipe for canonical initial-input rebinding."""

    verification_preview: str
    pinned_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        from fix_runtime.prompt import MAX_FIX_INPUT_CHARS

        object.__setattr__(self, "verification_preview", _bounded_text(
            self.verification_preview, "InitialWorkerRenderContext.verification_preview",
            max_chars=MAX_FIX_INPUT_CHARS, allow_empty=True,
        ))
        if not isinstance(self.pinned_paths, (tuple, list)) or len(self.pinned_paths) > _MAX_RENDER_PATHS:
            raise FixLoopInputError("InitialWorkerRenderContext.pinned_paths must be a bounded sequence.")
        paths = tuple(self.pinned_paths)
        for path in paths:
            _bounded_text(path, "InitialWorkerRenderContext.pinned_path", max_chars=_MAX_RENDER_PATH_CHARS)
        object.__setattr__(self, "pinned_paths", paths)


@dataclass(frozen=True, slots=True)
class FixWorkerRenderContext:
    """Detached, bounded recipe for this exact fix-attempt input."""

    max_fix_attempts: int
    pinned_paths: tuple[str, ...] = ()
    classification: str | None = None

    def __post_init__(self) -> None:
        if type(self.max_fix_attempts) is not int or not MIN_MAX_FIX_ATTEMPTS <= self.max_fix_attempts <= MAX_MAX_FIX_ATTEMPTS:
            raise FixLoopInputError("FixWorkerRenderContext.max_fix_attempts is out of bounds.")
        if not isinstance(self.pinned_paths, (tuple, list)) or len(self.pinned_paths) > _MAX_RENDER_PATHS:
            raise FixLoopInputError("FixWorkerRenderContext.pinned_paths must be a bounded sequence.")
        paths = tuple(self.pinned_paths)
        for path in paths:
            _bounded_text(path, "FixWorkerRenderContext.pinned_path", max_chars=_MAX_RENDER_PATH_CHARS)
        object.__setattr__(self, "pinned_paths", paths)
        if self.classification is not None:
            object.__setattr__(self, "classification", _bounded_text(
                self.classification, "FixWorkerRenderContext.classification",
                max_chars=_MAX_RENDER_CLASSIFICATION_CHARS,
                allow_empty=True,
            ))


def _capture_initial_worker_render_context(
    verification_preview: str, pinned_paths,
) -> InitialWorkerRenderContext | None:
    """Best-effort bounded recipe capture; never changes rendered-input success."""
    try:
        return InitialWorkerRenderContext(verification_preview, tuple(pinned_paths))
    except FixLoopInputError:
        return None


def _capture_fix_worker_render_context(
    max_fix_attempts: int, pinned_paths, classification: str | None,
) -> FixWorkerRenderContext | None:
    """Capture only context representable within metadata bounds.

    Match the fix renderer's display semantics for classification: empty is
    valid and longer labels are displayed as their first 128 characters.
    """
    display_classification = (
        classification[:_MAX_RENDER_CLASSIFICATION_CHARS]
        if classification is not None else None
    )
    try:
        return FixWorkerRenderContext(max_fix_attempts, tuple(pinned_paths), display_classification)
    except FixLoopInputError:
        return None


@dataclass(frozen=True, slots=True)
class FixWorkerRequest:
    """The bounded fix instruction actually handed to the Worker port.

    `rendered_input` IS the trust-boundary-enforced string produced by
    fix_runtime.prompt.render_fix_worker_input() for this exact attempt —
    FixLoopRunner never renders it merely for validation and then lets an
    adapter reconstruct its own prompt from the raw trigger. It remains the
    input of record unless an opted-in verified collaboration safe point
    replaces it using `render_context`; absent metadata means no such rebind
    can occur. A concrete WorkerAttemptRunner MUST treat `rendered_input` as
    the actual fix instruction/input it feeds to the underlying harness.
    """

    task: str
    trigger: FixTrigger
    attempt_index: int
    rendered_input: str
    plan: str | None = None
    render_context: FixWorkerRenderContext | None = None

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
        if self.render_context is not None:
            if not isinstance(self.render_context, FixWorkerRenderContext):
                raise FixLoopInputError("FixWorkerRequest.render_context must be FixWorkerRenderContext or None.")
            object.__setattr__(self, "render_context", FixWorkerRenderContext(
                self.render_context.max_fix_attempts,
                self.render_context.pinned_paths,
                self.render_context.classification,
            ))


@dataclass(frozen=True, slots=True)
class InitialWorkerRequest:
    """The bounded initial-implementation instruction handed to the Worker port.

    Mirrors FixWorkerRequest's contract for the FIRST Worker attempt of a
    Run (before any Verification/Review evidence exists): `rendered_input`
    IS the trust-boundary-enforced string produced by
    fix_runtime.prompt.render_initial_worker_input() for this exact attempt.
    A concrete WorkerAttemptRunner MUST treat `rendered_input` as the actual
    input it feeds to the underlying harness, exactly as it does for a
    FixWorkerRequest. Only an opted-in verified collaboration safe point may
    replace it using `render_context`; absent metadata prevents such a rebind.
    See fix_runtime.ports.WorkerAttemptRunner.
    """

    task: str
    rendered_input: str
    plan: str | None = None
    render_context: InitialWorkerRenderContext | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "task", _bounded_text(self.task, "InitialWorkerRequest.task", max_chars=_MAX_TASK_CHARS)
        )
        from fix_runtime.prompt import MAX_FIX_INPUT_CHARS  # lazy: prompt imports models

        object.__setattr__(
            self, "rendered_input",
            _bounded_text(
                self.rendered_input, "InitialWorkerRequest.rendered_input", max_chars=MAX_FIX_INPUT_CHARS,
            ),
        )
        if self.plan is not None:
            object.__setattr__(
                self, "plan",
                _bounded_text(self.plan, "InitialWorkerRequest.plan", max_chars=_MAX_PLAN_CHARS, allow_empty=True),
            )
        if self.render_context is not None:
            if not isinstance(self.render_context, InitialWorkerRenderContext):
                raise FixLoopInputError("InitialWorkerRequest.render_context must be InitialWorkerRenderContext or None.")
            object.__setattr__(self, "render_context", InitialWorkerRenderContext(
                self.render_context.verification_preview,
                self.render_context.pinned_paths,
            ))


@dataclass(frozen=True, slots=True)
class FixLoopRequest:
    task: str
    trigger: FixTrigger
    verification_plan: VerificationPlan
    plan: str | None = None
    max_fix_attempts: int = DEFAULT_MAX_FIX_ATTEMPTS
    # F2 (follow-up on a proposal): when set, used as the Reviewer's task
    # context INSTEAD OF `task` for every review call this fix loop makes
    # (e.g. task + the user's follow-up instruction) -- purely additive:
    # None reproduces the exact prior (pre-F2) behavior of reviewing against
    # `task` verbatim. Never used for the Worker's rendered input (see
    # fix_runtime.prompt.render_fix_worker_input; the trigger carries its
    # own feedback field for that).
    review_task: str | None = None
    # F6 (@-mentions): ordered, workspace-relative paths the user explicitly
    # pinned for this run -- threaded verbatim into render_fix_worker_input's
    # "USER-REFERENCED FILES" section for every fix attempt. Purely additive:
    # the default () reproduces the exact prior (pre-@-mentions) behavior.
    pinned_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "task", _bounded_text(self.task, "FixLoopRequest.task", max_chars=_MAX_TASK_CHARS))
        if not isinstance(self.trigger, FixTrigger):
            raise FixLoopInputError("FixLoopRequest.trigger must be a FixTrigger.")
        if not isinstance(self.verification_plan, VerificationPlan):
            raise FixLoopInputError("FixLoopRequest.verification_plan must be a VerificationPlan.")
        if not isinstance(self.pinned_paths, (tuple, list)) or not all(
            isinstance(item, str) for item in self.pinned_paths
        ):
            raise FixLoopInputError("FixLoopRequest.pinned_paths must be a sequence of strings.")
        object.__setattr__(self, "pinned_paths", tuple(self.pinned_paths))
        if self.plan is not None:
            object.__setattr__(
                self, "plan",
                _bounded_text(self.plan, "FixLoopRequest.plan", max_chars=_MAX_PLAN_CHARS, allow_empty=True),
            )
        if self.review_task is not None:
            object.__setattr__(
                self, "review_task",
                _bounded_text(self.review_task, "FixLoopRequest.review_task", max_chars=_MAX_TASK_CHARS),
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
    # Decision-layer-only field (docs/JEV-DESIGN.md Spike S1): a Turkish,
    # human-facing message set only for status=NEEDS_USER with reason
    # "needs_user_environment" (missing dependency/tooling) -- None otherwise,
    # including for reason "pre_existing_failure" (that case has no message
    # of its own; the pre-existing verification evidence speaks for itself).
    needs_user_message: str | None = None

    def __post_init__(self) -> None:
        _stable_id(self.fix_loop_id, "FixLoopReport.fix_loop_id")
        if not isinstance(self.status, FixLoopStatus):
            raise FixLoopInputError("FixLoopReport.status must be a FixLoopStatus.")
        if type(self.attempts_used) is not int or self.attempts_used < 0:
            raise FixLoopInputError("FixLoopReport.attempts_used must be a non-negative integer.")
        if not isinstance(self.reason, str) or not self.reason:
            raise FixLoopInputError("FixLoopReport.reason must be a non-empty string.")
        if self.needs_user_message is not None and not isinstance(self.needs_user_message, str):
            raise FixLoopInputError("FixLoopReport.needs_user_message must be a string or None.")


def new_fix_loop_id() -> str:
    return f"fix_{uuid.uuid4()}"


def new_fix_attempt_id() -> str:
    return f"fixatt_{uuid.uuid4()}"


def new_fix_execution_id() -> str:
    return f"exec_fix_{uuid.uuid4()}"


def validate_fix_loop_id(value: Any) -> str:
    return _stable_id(value, "fix_loop_id")
