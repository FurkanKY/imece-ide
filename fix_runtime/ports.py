"""Orchestration ports the FixLoopRunner depends on instead of concrete harnesses.

FixLoopRunner is orchestration ("who/when"), not a second Agent harness
("how"). It never touches ModelBackend, AgentSession internals, ToolRegistry
construction, or ProcessRunner internals directly — it only calls these
three small ports, each representing one already-existing execution
capability owned elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from review_runtime.models import ReviewReport, ReviewRequest
from verification_runtime.models import VerificationPlan, VerificationReport

from fix_runtime.errors import FixLoopInputError
from fix_runtime.models import FixWorkerRequest, _stable_id


@dataclass(frozen=True, slots=True)
class WorkerAttemptResult:
    """Confirms the execution_id the adapter actually used for this attempt."""

    execution_id: str

    def __post_init__(self) -> None:
        _stable_id(self.execution_id, "WorkerAttemptResult.execution_id")


class WorkerAttemptRunner(Protocol):
    """Runs exactly ONE fresh Worker execution.

    Contract:
      - the implementation MUST feed `request.rendered_input` verbatim to the
        underlying harness as the actual fix instruction/input for this
        attempt. `rendered_input` is the trust-boundary-enforced string
        already produced by fix_runtime.prompt.render_fix_worker_input() —
        it must not be discarded, and the adapter must not reconstruct an
        unrelated prompt from `request.trigger` instead. Structured fields
        on `request` (task/trigger/attempt_index/plan) MAY additionally be
        used as metadata, but `rendered_input` is the input of record.
      - the implementation MUST use the supplied `execution_id` for this
        execution's canonical lifecycle (it does not invent its own).
      - this call represents ONE fresh execution — never a resumed or reused
        AgentSession. FixLoopRunner always passes a brand-new execution_id
        per attempt (see fix_runtime.models.new_fix_execution_id).
      - a successful return means the worker EXECUTION itself completed
        (e.g. its canonical execution.completed was recorded); it does NOT
        assert the fix was correct — that is Verification/Reviewer's job.
      - the concrete adapter owns recording that execution's own normal
        canonical execution.* lifecycle; FixLoopRunner does not do this for
        it and does not inspect AgentSession/ModelBackend state directly.
    """

    def run(
        self, workspace, request: FixWorkerRequest, *, execution_id: str
    ) -> WorkerAttemptResult: ...


class VerificationAttemptRunner(Protocol):
    """Runs exactly ONE verification attempt with the given fresh verification_id.

    The returned VerificationReport.verification_id MUST equal the requested
    id. The concrete adapter owns recording normal canonical verification.*
    events for that attempt.
    """

    def run(
        self, workspace, plan: VerificationPlan, *, verification_id: str
    ) -> VerificationReport: ...


class ReviewAttemptRunner(Protocol):
    """Runs exactly ONE semantic review attempt with the given fresh review_id.

    The returned ReviewReport.review_id MUST equal the requested id. This
    does not duplicate review_runtime.ReviewerRunner's parser/prompt/context/
    read-only-tool-policy logic — it is expected to be a thin adapter over
    the existing ReviewerRunner.
    """

    def run(self, workspace, request: ReviewRequest, *, review_id: str) -> ReviewReport: ...
