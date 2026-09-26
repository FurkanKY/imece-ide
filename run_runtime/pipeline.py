"""run_runtime.pipeline — canonical events owned by the top-level end-to-end
pipeline orchestration (pipeline_runtime.PipelineRunner), for Run outcomes
that don't belong to any single existing per-attempt recorder (Planner/
Worker/Verification/Reviewer/FixLoop already own their own plan.*/
execution.*/verification.*/review.*/fix_loop.* trails) and that
RunCompletionGate's evidence-gated complete_verified/complete_reviewed paths
don't cover either.

CanonicalPipelineRecorder is used from two places:

  1. Directly by pipeline_runtime.PipelineRunner for outcomes that are never
     gated by Verification/Review evidence at all: the advisory "no
     deterministic verification plan was detected" proposal, a cooperative
     cancellation, a logical (non-infrastructure) failure, and the
     terminal "the initial Worker attempt made no changes" outcome.
  2. Internally by run_runtime.completion.RunCompletionGate, when
     constructed with settlement="await_user": the SAME evidence-gated
     complete_verified/complete_reviewed checks run unchanged, but instead
     of a terminal run.completed, the Run is left WAITING_USER with a
     proposal.ready carrying the exact same provenance payload a
     "complete" settlement would have used for run.completed — so the
     history still shows WHY the proposal is ready, and the user's
     Apply/Reject decision (run_runtime.legacy.LegacyRunCoordinator.
     record_proposal_applied/record_proposal_rejected, both of which
     require WAITING_USER) can actually be recorded for a pipeline Run.

needs_user() deliberately does NOT also emit change.proposed: the legacy
project_runner bridge's own "ready to decide" moment (run_runtime.legacy.
LegacyEventAdapter._on_proposal) records only proposal.ready + (when there
are proposals) run.waiting_user in that same atomic step — change.proposed
there is a separate, per-diff-hunk narration event emitted earlier and
independently (LegacyEventAdapter._on_diff), not part of the terminal
decision fact itself, and the gate/recorder here has no per-file diff
payload to narrate one from (only the aggregate diff_sha256/verification/
review provenance already established by canonical evidence).

Every event here has correlation_id=run_id and NEVER execution_id
(pipeline-level bookkeeping is not an execution attempt).
"""

from __future__ import annotations

from typing import Any

from run_runtime.events import RunEventSpec, RunEventType
from run_runtime.models import RunStatus
from run_runtime.service import RunRuntime

SOURCE = "pipeline"


class CanonicalPipelineRecorder:
    """Records the small set of Run-level canonical events owned directly by
    the pipeline orchestration layer rather than by a per-attempt recorder."""

    def __init__(self, runtime: RunRuntime, run_id: str) -> None:
        self._runtime = runtime
        self._run_id = run_id

    def needs_user(
        self,
        *,
        payload: dict[str, Any],
        expected_last_event_seq: int | None = None,
        source: str = SOURCE,
    ) -> None:
        """Record proposal.ready + run.waiting_user atomically.

        When `expected_last_event_seq` is omitted (the direct
        PipelineRunner call site), this is a best-effort, idempotent no-op
        if the Run is no longer RUNNING. When it IS supplied (the
        RunCompletionGate await_user call site, whose evidence checks
        already required RUNNING), a stale sequence surfaces as
        EventSequenceError exactly like every other gate-authored write —
        it is never silently swallowed here.
        """
        if expected_last_event_seq is None:
            run = self._runtime.get_run(self._run_id)
            if run.status is not RunStatus.RUNNING:
                return
            expected_last_event_seq = run.last_event_seq
        self._runtime.record_many(
            run_id=self._run_id,
            specs=(
                RunEventSpec(
                    type=RunEventType.PROPOSAL_READY, payload=dict(payload),
                    source=source, correlation_id=self._run_id,
                ),
                RunEventSpec(
                    type=RunEventType.RUN_WAITING_USER, payload={}, source=source, correlation_id=self._run_id,
                ),
            ),
            expected_last_event_seq=expected_last_event_seq,
        )

    def completed_no_changes(self) -> None:
        """Terminal run.completed, reason "no_changes" — the initial Worker
        attempt produced no workspace changes at all, so there is nothing to
        verify or review. Best-effort, idempotent no-op if not RUNNING."""
        run = self._runtime.get_run(self._run_id)
        if run.status is not RunStatus.RUNNING:
            return
        self._runtime.record(
            run_id=self._run_id, type=RunEventType.RUN_COMPLETED,
            payload={"reason": "no_changes"}, source=SOURCE, correlation_id=self._run_id,
            expected_last_event_seq=run.last_event_seq,
        )

    def cancelled(self) -> None:
        """Best-effort, idempotent no-op if not RUNNING (cooperative
        cancellation is only ever observed between pipeline stages)."""
        run = self._runtime.get_run(self._run_id)
        if run.status is not RunStatus.RUNNING:
            return
        self._runtime.record(
            run_id=self._run_id, type=RunEventType.RUN_CANCELLED, payload={},
            source=SOURCE, correlation_id=self._run_id, expected_last_event_seq=run.last_event_seq,
        )

    def failed(self, *, error_code: str, error_message: str | None = None) -> None:
        """A logical (non-infrastructure) pipeline failure. Best-effort,
        idempotent no-op if not RUNNING."""
        run = self._runtime.get_run(self._run_id)
        if run.status is not RunStatus.RUNNING:
            return
        self._runtime.record(
            run_id=self._run_id, type=RunEventType.RUN_FAILED,
            payload={"error_code": error_code, "error_message": error_message or error_code},
            source=SOURCE, correlation_id=self._run_id, expected_last_event_seq=run.last_event_seq,
        )
