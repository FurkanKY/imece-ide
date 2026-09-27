"""Orchestration ports the FixLoopRunner depends on instead of concrete harnesses.

FixLoopRunner is orchestration ("who/when"), not a second Agent harness
("how"). It never touches ModelBackend, AgentSession internals, ToolRegistry
construction, or ProcessRunner internals directly — it only calls these
three small ports, each representing one already-existing execution
capability owned elsewhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agent_runtime.cancellation import CancellationToken
from review_runtime.models import ReviewReport, ReviewRequest
from verification_runtime.models import VerificationPlan, VerificationReport

from fix_runtime.errors import FixLoopInputError
from fix_runtime.models import FixWorkerRequest, InitialWorkerRequest, _stable_id


@dataclass(frozen=True, slots=True)
class WorkerAttemptResult:
    """Confirms the execution_id the adapter actually used for this attempt."""

    execution_id: str

    def __post_init__(self) -> None:
        _stable_id(self.execution_id, "WorkerAttemptResult.execution_id")


class WorkerAttemptRunner(Protocol):
    """Runs exactly ONE fresh Worker execution.

    `request` is either a FixWorkerRequest (a bounded fix attempt driven by
    a FixTrigger) or an InitialWorkerRequest (the FIRST implementation
    attempt of a Run, before any Verification/Review evidence exists).
    Both shapes carry the same `rendered_input`/`task`/`plan` fields and are
    handled identically by every WorkerAttemptRunner implementation.

    Contract:
      - the implementation MUST feed `request.rendered_input` verbatim to the
        underlying harness as the actual instruction/input for this attempt.
        `rendered_input` is the trust-boundary-enforced string already
        produced by fix_runtime.prompt.render_fix_worker_input() (for a
        FixWorkerRequest) or fix_runtime.prompt.render_initial_worker_input()
        (for an InitialWorkerRequest) — it must not be discarded, and the
        adapter must not reconstruct an unrelated prompt from
        `request.trigger` instead. Structured fields on `request`
        (task/trigger/attempt_index/plan, where present) MAY additionally be
        used as metadata, but `rendered_input` is the input of record for
        BOTH request shapes.
      - the implementation MUST use the supplied `execution_id` for this
        execution's canonical lifecycle (it does not invent its own).
      - this call represents ONE fresh execution — never a resumed or reused
        AgentSession. Callers always pass a brand-new execution_id per
        attempt (see fix_runtime.models.new_fix_execution_id).
      - a successful return means the worker EXECUTION itself completed
        (e.g. its canonical execution.completed was recorded); it does NOT
        assert the work is correct — that is Verification/Reviewer's job.
      - the concrete adapter owns recording that execution's own normal
        canonical execution.* lifecycle; the caller does not do this for it
        and does not inspect AgentSession/ModelBackend state directly.
    """

    def run(
        self, workspace, request: FixWorkerRequest | InitialWorkerRequest, *, execution_id: str,
        cancel_token: CancellationToken | None = None,
    ) -> WorkerAttemptResult: ...


class VerificationAttemptRunner(Protocol):
    """Runs exactly ONE verification attempt with the given fresh verification_id.

    The returned VerificationReport.verification_id MUST equal the requested
    id. The concrete adapter owns recording normal canonical verification.*
    events for that attempt.
    """

    def run(
        self, workspace, plan: VerificationPlan, *, verification_id: str,
        cancel_token: CancellationToken | None = None,
    ) -> VerificationReport: ...


class ReviewAttemptRunner(Protocol):
    """Runs exactly ONE semantic review attempt with the given fresh review_id.

    The returned ReviewReport.review_id MUST equal the requested id. This
    does not duplicate review_runtime.ReviewerRunner's parser/prompt/context/
    read-only-tool-policy logic — it is expected to be a thin adapter over
    the existing ReviewerRunner.

    `pinned_paths` (F6, @-mentions; optional, default empty): see
    pipeline_runtime.ports.PlanAttemptRunner's docstring for the same
    contract — purely additive, fed through to ContextEngine.build.
    """

    def run(
        self, workspace, request: ReviewRequest, *, review_id: str,
        cancel_token: CancellationToken | None = None, pinned_paths: Sequence[str] = (),
    ) -> ReviewReport: ...
