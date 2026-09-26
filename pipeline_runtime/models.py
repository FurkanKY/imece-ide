"""Immutable, provider-neutral models for the end-to-end pipeline.

PipelineRunner composes the already-existing canonical pieces (Planner,
Worker, Verification, Reviewer, FixLoop) into a single "plan -> initial
attempt -> verify -> review -> (fix loop) -> terminal outcome" flow. This
module only defines the pipeline's own outcome/status vocabulary; it never
redefines any of the models owned by planner_runtime/fix_runtime/
verification_runtime/review_runtime/change_runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from change_runtime.models import WorkspaceChangeSet
from fix_runtime.models import FixLoopReport
from planner_runtime.models import PlanReport
from review_runtime.models import ReviewReport
from verification_runtime.models import VerificationReport

from pipeline_runtime.errors import PipelineInputError


class PipelineStatus(StrEnum):
    """The pipeline's own terminal-outcome vocabulary.

    COMPLETED: the Run reached a Reviewer-APPROVED, Verification-PASSed
        state (directly, or via the bounded Fix Loop) AND the
        RunCompletionGate in use was configured with settlement="complete".
        PipelineRunner's own default gate uses settlement="await_user"
        instead (the user always has the final Apply/Reject word in the
        IDE — see the module docstring in pipeline_runtime.runner), so in
        practice a fully-approved Run normally reports NEEDS_USER, not
        COMPLETED.
    NEEDS_USER: the Run is WAITING_USER for a human decision — either
        because it was Reviewer-APPROVED/Verification-PASSed and is now
        awaiting Apply/Reject (reason "reviewed"), or because no
        deterministic VerificationPlan could be detected for the workspace,
        so an advisory review ran instead and the proposed diff needs a
        human decision (reason "no_verification_plan_detected"; see module
        docstring in pipeline_runtime.runner for why a synthetic PASS is
        refused).
    NO_CHANGES: the initial Worker attempt produced no workspace changes at
        all, so there is nothing to verify/review. Terminal: run.completed,
        reason "no_changes".
    FAILED: a logical (non-infrastructure) failure occurred — e.g. the
        workspace changed between Reviewer approval and completion, or the
        run was cancelled via the cooperative cancel_event.
    EXHAUSTED: the bounded Fix Loop ran out of attempts without reaching an
        approved, verified state.
    """

    COMPLETED = "completed"
    NEEDS_USER = "needs_user"
    NO_CHANGES = "no_changes"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"


def _optional_type(value, expected, field: str):
    if value is not None and not isinstance(value, expected):
        raise PipelineInputError(f"{field} must be a {expected.__name__} or None.")
    return value


@dataclass(frozen=True, slots=True)
class PipelineReport:
    """The pipeline's single terminal outcome for one `PipelineRunner.run()` call."""

    run_id: str
    status: PipelineStatus
    reason: str
    plan_report: PlanReport | None = None
    change_set: WorkspaceChangeSet | None = None
    verification_report: VerificationReport | None = None
    review_report: ReviewReport | None = None
    fix_loop_report: FixLoopReport | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise PipelineInputError("PipelineReport.run_id must be a non-empty string.")
        if not isinstance(self.status, PipelineStatus):
            raise PipelineInputError("PipelineReport.status must be a PipelineStatus.")
        if not isinstance(self.reason, str) or not self.reason:
            raise PipelineInputError("PipelineReport.reason must be a non-empty string.")
        _optional_type(self.plan_report, PlanReport, "PipelineReport.plan_report")
        _optional_type(self.change_set, WorkspaceChangeSet, "PipelineReport.change_set")
        _optional_type(self.verification_report, VerificationReport, "PipelineReport.verification_report")
        _optional_type(self.review_report, ReviewReport, "PipelineReport.review_report")
        _optional_type(self.fix_loop_report, FixLoopReport, "PipelineReport.fix_loop_report")
