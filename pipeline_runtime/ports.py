"""The one small port PipelineRunner needs that fix_runtime does not already
provide: running exactly one fresh Planner attempt.

PipelineRunner otherwise depends only on fix_runtime.ports
(WorkerAttemptRunner/VerificationAttemptRunner/ReviewAttemptRunner) plus
change_runtime.ChangeProvider — the same small ports FixLoopRunner already
depends on. This mirrors that style for the Planner.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from agent_runtime.cancellation import CancellationToken
from planner_runtime.models import PlanReport


class PlanAttemptRunner(Protocol):
    """Runs exactly ONE fresh Planner attempt.

    Contract:
      - the implementation MUST use the supplied `plan_id` for this
        attempt's canonical plan.* lifecycle (it does not invent its own).
      - the returned PlanReport.plan_id MUST equal the requested `plan_id`.
      - the concrete adapter owns recording that attempt's own normal
        canonical plan.* lifecycle; PipelineRunner does not do this for it.
      - Planner activity NEVER counts as execution activity (see
        run_runtime.planner.CanonicalPlannerEventSink) — this is unchanged
        by PipelineRunner using this port.
      - `pinned_paths` (F6, @-mentions; optional, default empty) is an
        ordered sequence of workspace-relative paths the USER explicitly
        referenced for this run. When present, the implementation SHOULD
        feed it through to its ContextEngine.build(..., pinned_paths=...)
        call so those files/folders are included at the highest priority.
        This is purely additive: omitting it reproduces prior behavior.
    """

    def run(
        self, workspace, task: str, *, plan_id: str, cancel_token: CancellationToken | None = None,
        pinned_paths: Sequence[str] = (),
    ) -> PlanReport: ...
