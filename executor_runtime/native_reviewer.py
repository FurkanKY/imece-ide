"""NativeReviewAttemptAdapter — thin HOW-adapter binding
fix_runtime.ports.ReviewAttemptRunner to a caller-configured ReviewerRunner.

Semantic reviewer configuration (backend/context/limits/prompt/parsing) is
never duplicated here; the caller builds a ReviewerRunner and hands it in.
"""

from __future__ import annotations

from agent_runtime.cancellation import CancellationToken, OperationCancelledError
from context_runtime import load_project_rules
from review_runtime.models import ReviewReport, ReviewRequest
from review_runtime.runner import ReviewerRunner

from run_runtime.reviewer import CanonicalReviewEventSink
from run_runtime.service import RunRuntime

from executor_runtime.errors import (
    ExecutorAdapterCancelledError,
    ExecutorAdapterExecutionError,
    ExecutorAdapterInputError,
)


class NativeReviewAttemptAdapter:
    """Runs exactly one fresh semantic review attempt.

    Concrete production implementation of fix_runtime.ports.ReviewAttemptRunner.
    """

    def __init__(self, runtime: RunRuntime, run_id: str, reviewer: ReviewerRunner) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ExecutorAdapterInputError("NativeReviewAttemptAdapter.run_id must be a non-empty string.")
        if not isinstance(reviewer, ReviewerRunner):
            raise ExecutorAdapterInputError("NativeReviewAttemptAdapter requires a ReviewerRunner.")
        self._runtime = runtime
        self._run_id = run_id
        self._reviewer = reviewer

    @property
    def run_id(self) -> str:
        return self._run_id

    def run(
        self, workspace, request: ReviewRequest, *, review_id: str,
        cancel_token: CancellationToken | None = None,
    ) -> ReviewReport:
        if not isinstance(request, ReviewRequest):
            raise ExecutorAdapterInputError("NativeReviewAttemptAdapter.run requires a ReviewRequest.")

        # Reading rules here (in addition to ReviewerRunner.run()'s own read
        # of the same workspace root) only costs a cheap, read-only file
        # check; it lets the canonical sink attach provenance (rules_sha256)
        # to review.started without ReviewerRunner needing to report it back.
        rules = load_project_rules(workspace.root)
        try:
            sink = CanonicalReviewEventSink(
                self._runtime, self._run_id, review_id=review_id,
                rules_sha256=rules.sha256 if rules is not None else None,
            )
        except Exception as exc:
            raise ExecutorAdapterInputError(f"Cannot construct canonical Reviewer sink: {exc}") from exc

        try:
            report = self._reviewer.run(
                workspace, request, recorder=sink, review_id=review_id, cancel_token=cancel_token,
            )
        except OperationCancelledError as exc:
            raise ExecutorAdapterCancelledError(f"Reviewer port cancelled: {exc}") from exc
        except Exception as exc:
            raise ExecutorAdapterExecutionError(f"Reviewer port failed: {exc}") from exc

        if not isinstance(report, ReviewReport) or report.review_id != review_id:
            raise ExecutorAdapterExecutionError(
                "ReviewerRunner did not return the requested review_id."
            )
        return report
