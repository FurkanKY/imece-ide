"""Canonical RunEvent adapter for the transient native semantic Reviewer.

CRITICAL invariant: Reviewer canonical events NEVER use execution.started/
execution.completed/execution.failed and NEVER set RunEvent.execution_id.
RunCompletionGate.complete_verified() treats newer execution.* activity as
evidence that verification is stale (see run_runtime.completion); Reviewer
activity must never trigger that check.

Transient AgentSession.ExecutionCompleted only means "the generic agent loop
returned final text" — it does NOT mean a valid semantic review exists. This
sink therefore does NOT append review.completed for ExecutionCompleted; only
an explicit, successfully-parsed ReviewerRunner.complete(report) call does.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.events import (
    AgentEvent,
    ApprovalRequested,
    ApprovalResolved,
    ExecutionCompleted,
    ExecutionFailed,
    ExecutionStarted,
    ModelCompleted,
    ModelFailed,
    ModelStarted,
    ToolCompleted,
    ToolFailed,
    ToolRequested,
    ToolStarted,
    TurnCompleted,
    TurnStarted,
)
from review_runtime.errors import ReviewRecordingError
from review_runtime.models import ReviewReport, validate_review_id
from review_runtime.parser import MAX_MODEL_OUTPUT_CHARS
from run_runtime.events import RunEventSpec, RunEventType
from run_runtime.models import RunStatus
from run_runtime.service import RunRuntime

SOURCE = "reviewer"
_MAX_MESSAGE = 2000


def _bounded_message(value: str) -> str:
    return value.replace("\x00", "")[:_MAX_MESSAGE]


def _finding_payload(finding) -> dict[str, Any]:
    return {
        "severity": finding.severity.value,
        "message": finding.message,
        "path": finding.path,
        "start_line": finding.start_line,
        "end_line": finding.end_line,
    }


def _error_payload(event: ModelFailed | ToolFailed) -> dict[str, Any]:
    payload = {
        "error_type": event.error_type,
        "message": event.message[:2000],
    }
    if isinstance(event, ToolFailed):
        payload.update({"stage": event.stage, "recoverable": event.recoverable})
    return payload


class CanonicalReviewEventSink:
    """Maps one transient AgentSession review attempt to canonical review.* events."""

    def __init__(self, runtime: RunRuntime, run_id: str, *, review_id: str) -> None:
        review_id = validate_review_id(review_id)
        run = runtime.get_run(run_id)
        if run.status is not RunStatus.RUNNING:
            raise ValueError(f"Review sink requires RUNNING run, got {run.status}")
        self._runtime = runtime
        self._run_id = run_id
        self._review_id = review_id
        self._expected_seq = run.last_event_seq
        self._transient_execution_id: str | None = None
        self._terminal_recorded = False
        # These two flags are process-local (per sink instance) by design:
        # there is intentionally no Reviewer resume API in 3G, so a freshly
        # constructed sink — even one built with an already-used review_id —
        # must never be able to terminal-settle an attempt it did not itself
        # observe starting/completing. Canonical review_id-reuse rejection
        # (see _reject_reused_review_id) is a separate, additional guard.
        self._started_persisted = False
        self._execution_completed_observed = False

    @property
    def review_id(self) -> str:
        return self._review_id

    @property
    def expected_last_event_seq(self) -> int:
        return self._expected_seq

    # ---------------- AgentEventSink protocol ----------------

    def emit(self, event: AgentEvent) -> None:
        if not isinstance(event, AgentEvent):
            raise TypeError("CanonicalReviewEventSink accepts AgentEvent values only")
        if self._transient_execution_id is None:
            self._transient_execution_id = event.execution_id
        elif event.execution_id != self._transient_execution_id:
            raise ValueError("Agent event execution_id does not match this review sink")

        if self._terminal_recorded:
            raise ReviewRecordingError(
                f"Review attempt already has a terminal event; rejecting further "
                f"lifecycle event {type(event).__name__}: {self._review_id}"
            )
        if not self._started_persisted and not isinstance(event, ExecutionStarted):
            raise ReviewRecordingError(
                f"review.started has not been persisted yet; rejecting "
                f"{type(event).__name__} out of order: {self._review_id}"
            )

        if isinstance(event, ExecutionStarted):
            self._reject_reused_review_id()
            self._commit([self._spec(event, RunEventType.REVIEW_STARTED, {"review_id": self._review_id})])
            self._started_persisted = True
            return
        if isinstance(event, ExecutionCompleted):
            # Transient completion only means the agent loop returned final
            # text; it is NOT a valid semantic review outcome by itself. It
            # IS, however, the precondition complete() requires: a report
            # can only be constructed from a final answer that actually
            # arrived.
            self._execution_completed_observed = True
            return
        if isinstance(event, ExecutionFailed):
            self.fail(self._review_id, event.error_type, event.message)
            return

        specs = self._specs(event)
        if not specs:
            raise ValueError(f"Unsupported reviewer event: {type(event).__name__}")
        self._commit(specs)

    # ---------------- ReviewRecorder protocol ----------------

    def complete(self, report: ReviewReport) -> None:
        if not isinstance(report, ReviewReport):
            raise TypeError("CanonicalReviewEventSink.complete requires a ReviewReport")
        if report.review_id != self._review_id:
            raise ValueError("ReviewReport.review_id does not match this review sink")
        if not self._started_persisted:
            raise ReviewRecordingError(
                f"complete() requires this sink to have persisted review.started first: {self._review_id}"
            )
        if not self._execution_completed_observed:
            raise ReviewRecordingError(
                f"complete() requires this sink to have observed a transient "
                f"ExecutionCompleted before a ReviewReport can be trusted: {self._review_id}"
            )
        self._append_terminal(
            RunEventType.REVIEW_COMPLETED,
            {
                "review_id": report.review_id,
                "verdict": report.verdict.value,
                "note": report.summary,
                "summary": report.summary,
                "findings": [_finding_payload(finding) for finding in report.findings],
                "repository_fingerprint": report.repository_fingerprint,
                "diff_sha256": report.diff_sha256,
                "verification_id": report.verification_id,
                "verification_status": report.verification_status,
            },
        )

    def fail(self, review_id: str, error_type: str, message: str) -> None:
        if review_id != self._review_id:
            raise ValueError("review_id does not match this review sink")
        if not self._started_persisted:
            raise ReviewRecordingError(
                f"fail() requires this sink to have persisted review.started first: {self._review_id}"
            )
        self._append_terminal(
            RunEventType.REVIEW_FAILED,
            {
                "review_id": review_id,
                "error_type": _bounded_message(str(error_type)),
                "error_message": _bounded_message(str(message)),
            },
        )

    # ---------------- internals ----------------

    def _append_terminal(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._terminal_recorded:
            raise ReviewRecordingError(
                f"Review attempt already has a terminal event: {self._review_id}"
            )
        self._commit([RunEventSpec(type=event_type, payload=payload, correlation_id=self._review_id, source=SOURCE)])
        self._terminal_recorded = True

    def _commit(self, specs: list[RunEventSpec]) -> None:
        committed, _ = self._runtime.record_many(
            run_id=self._run_id,
            specs=tuple(specs),
            expected_last_event_seq=self._expected_seq,
        )
        self._expected_seq = committed[-1].seq

    def _reject_reused_review_id(self) -> None:
        after_seq = 0
        while True:
            page = self._runtime.events(self._run_id, after_seq=after_seq, limit=200)
            for existing in page.events:
                if (
                    existing.type == RunEventType.REVIEW_STARTED
                    and existing.payload.get("review_id") == self._review_id
                ):
                    raise ValueError(f"review_id already started in run: {self._review_id}")
            if not page.has_more:
                return
            after_seq = page.events[-1].seq

    def _spec(
        self,
        event: AgentEvent,
        event_type: str,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
        item_id: str | None = None,
    ) -> RunEventSpec:
        return RunEventSpec(
            type=event_type,
            payload=payload,
            execution_id=None,
            turn_id=turn_id,
            item_id=item_id,
            correlation_id=self._review_id,
            source=SOURCE,
        )

    def _specs(self, event: AgentEvent) -> list[RunEventSpec]:
        if isinstance(event, TurnStarted):
            return [self._spec(event, RunEventType.TURN_STARTED, {
                "turn_index": event.turn_index,
            }, turn_id=event.turn_id)]
        if isinstance(event, ModelStarted):
            return [self._spec(event, RunEventType.MODEL_STARTED, {
                "turn_index": event.turn_index,
            }, turn_id=event.turn_id, item_id=event.item_id)]
        if isinstance(event, ModelCompleted):
            text_truncated = len(event.text) > MAX_MODEL_OUTPUT_CHARS
            bounded_text = event.text[:MAX_MODEL_OUTPUT_CHARS] if text_truncated else event.text
            specs = [self._spec(event, RunEventType.MODEL_COMPLETED, {
                "text": bounded_text,
                "text_truncated": text_truncated,
                "stop_reason": event.stop_reason,
                "tool_calls": [
                    {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                    for call in event.tool_calls
                ],
            }, turn_id=event.turn_id, item_id=event.item_id)]
            usage_payload = {
                "prompt_tokens": event.usage.input_tokens,
                "completion_tokens": event.usage.output_tokens,
                "total_tokens": event.usage.input_tokens + event.usage.output_tokens,
                "cost_usd": event.usage.cost_usd,
            }
            specs.append(self._spec(event, RunEventType.USAGE_RECORDED, usage_payload,
                                    turn_id=event.turn_id, item_id=event.item_id))
            return specs
        if isinstance(event, ModelFailed):
            return [self._spec(event, RunEventType.MODEL_FAILED, _error_payload(event), turn_id=event.turn_id, item_id=event.item_id)]
        if isinstance(event, ToolStarted):
            return [self._spec(event, RunEventType.TOOL_STARTED, {
                "call_id": event.call_id,
                "tool_name": event.tool_name,
            }, turn_id=event.turn_id, item_id=event.item_id)]
        if isinstance(event, ToolRequested):
            return [self._spec(event, RunEventType.TOOL_REQUESTED, {
                "call_id": event.call_id,
                "tool_name": event.tool_name,
                "arguments": event.arguments,
            }, turn_id=event.turn_id, item_id=event.item_id)]
        if isinstance(event, ToolCompleted):
            return [self._spec(event, RunEventType.TOOL_COMPLETED, {
                "call_id": event.call_id,
                "tool_name": event.tool_name,
                "content": event.content,
                "metadata": event.metadata,
            }, turn_id=event.turn_id, item_id=event.item_id)]
        if isinstance(event, ToolFailed):
            return [self._spec(event, RunEventType.TOOL_FAILED, {
                **_error_payload(event),
                "call_id": event.call_id,
                "tool_name": event.tool_name,
            }, turn_id=event.turn_id, item_id=event.item_id)]
        if isinstance(event, ApprovalRequested):
            # Deliberately no run.waiting_user: the Reviewer's fail-closed
            # policy makes this impossible under normal registered tools; if
            # it ever happens it is a configuration failure, not a pause.
            return [self._spec(event, RunEventType.PERMISSION_REQUESTED, {
                "call_id": event.call_id,
                "tool_name": event.tool_name,
                "fingerprint": event.fingerprint,
            }, turn_id=event.turn_id, item_id=event.item_id)]
        if isinstance(event, ApprovalResolved):
            return []
        if isinstance(event, TurnCompleted):
            return [self._spec(event, RunEventType.TURN_COMPLETED, {}, turn_id=event.turn_id)]
        return []
