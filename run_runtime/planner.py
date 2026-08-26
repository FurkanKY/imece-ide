"""Canonical RunEvent adapter for the transient native semantic Planner.

CRITICAL invariant: Planner canonical events NEVER use execution.started/
execution.completed/execution.failed and NEVER set RunEvent.execution_id.
This protects RunCompletionGate's execution-evidence semantics exactly as
CanonicalReviewEventSink already does for the Reviewer (see
run_runtime.reviewer) — Planner activity must never be mistaken for Worker
execution activity by any completion-gate staleness check.

Transient AgentSession.ExecutionCompleted only means "the generic agent loop
returned final text" — it does NOT mean a valid structured plan exists. This
sink therefore does NOT append plan.completed for ExecutionCompleted; only
an explicit, successfully-parsed PlannerRunner.complete(report) call does.

plan.completed != run.completed: this sink never writes run.completed or
run.failed — Run terminal ownership remains entirely with RunCompletionGate,
untouched by this milestone.

task_sha256 has exactly ONE authority: PlannerRunner computes it from the
original task and places it on PlanReport.task_sha256, which flows into
plan.completed's payload. This sink deliberately does NOT accept a
caller-supplied task_sha256 at construction time and does NOT include it in
plan.started — a second, independently-supplied SHA at start time would let
canonical history claim two different task hashes for the same plan_id.
plan_id alone is sufficient correlation for crash recovery.

Legacy code already emits an OLDER plan.completed payload shape (see
run_runtime.legacy._on_plan: {"summary", "files"}, execution_id set, source
"legacy"). This sink's native payload is intentionally richer and always
carries execution_id=None and source="planner" — the two are trivially
distinguishable by provenance, and existing readmodels.py code already reads
plan.completed defensively via payload.get(...), so no schema migration is
required or introduced here.
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
from planner_runtime.errors import PlannerRecordingError
from planner_runtime.models import PlanReport, validate_plan_id
from planner_runtime.parser import MAX_MODEL_OUTPUT_CHARS
from run_runtime.events import RunEventSpec, RunEventType
from run_runtime.models import RunStatus
from run_runtime.service import RunRuntime

SOURCE = "planner"
_MAX_MESSAGE = 2000


def _bounded_message(value: str) -> str:
    return value.replace("\x00", "")[:_MAX_MESSAGE]


def _step_payload(step) -> dict[str, Any]:
    return {"title": step.title, "objective": step.objective}


def _error_payload(event: ModelFailed | ToolFailed) -> dict[str, Any]:
    payload = {
        "error_type": event.error_type,
        "message": event.message[:2000],
    }
    if isinstance(event, ToolFailed):
        payload.update({"stage": event.stage, "recoverable": event.recoverable})
    return payload


class CanonicalPlannerEventSink:
    """Maps one transient AgentSession planning attempt to canonical plan.* events."""

    def __init__(
        self, runtime: RunRuntime, run_id: str, *, plan_id: str,
    ) -> None:
        plan_id = validate_plan_id(plan_id)
        run = runtime.get_run(run_id)
        if run.status is not RunStatus.RUNNING:
            raise ValueError(f"Planner sink requires RUNNING run, got {run.status}")
        self._runtime = runtime
        self._run_id = run_id
        self._plan_id = plan_id
        self._expected_seq = run.last_event_seq
        self._transient_execution_id: str | None = None
        self._terminal_recorded = False
        # Process-local by design (no Planner resume API in 3I): a freshly
        # constructed sink — even one built with an already-used plan_id —
        # must never be able to terminal-settle an attempt it did not itself
        # observe starting/completing. Canonical plan_id-reuse rejection
        # (see _reject_reused_plan_id) is a separate, additional guard.
        self._started_persisted = False
        self._execution_completed_observed = False

    @property
    def plan_id(self) -> str:
        return self._plan_id

    @property
    def expected_last_event_seq(self) -> int:
        return self._expected_seq

    # ---------------- AgentEventSink protocol ----------------

    def emit(self, event: AgentEvent) -> None:
        if not isinstance(event, AgentEvent):
            raise TypeError("CanonicalPlannerEventSink accepts AgentEvent values only")
        if self._transient_execution_id is None:
            self._transient_execution_id = event.execution_id
        elif event.execution_id != self._transient_execution_id:
            raise ValueError("Agent event execution_id does not match this plan sink")

        if self._terminal_recorded:
            raise PlannerRecordingError(
                f"Plan attempt already has a terminal event; rejecting further "
                f"lifecycle event {type(event).__name__}: {self._plan_id}"
            )
        if not self._started_persisted and not isinstance(event, ExecutionStarted):
            raise PlannerRecordingError(
                f"plan.started has not been persisted yet; rejecting "
                f"{type(event).__name__} out of order: {self._plan_id}"
            )

        if isinstance(event, ExecutionStarted):
            self._reject_reused_plan_id()
            self._commit([self._spec(event, RunEventType.PLAN_STARTED, {
                "plan_id": self._plan_id,
            })])
            self._started_persisted = True
            return
        if isinstance(event, ExecutionCompleted):
            # Transient completion only means the agent loop returned final
            # text; it is NOT a valid structured plan by itself. It IS,
            # however, the precondition complete() requires: a report can
            # only be constructed from a final answer that actually arrived.
            self._execution_completed_observed = True
            return
        if isinstance(event, ExecutionFailed):
            self.fail(self._plan_id, event.error_type, event.message)
            return

        specs = self._specs(event)
        if not specs:
            raise ValueError(f"Unsupported planner event: {type(event).__name__}")
        self._commit(specs)

    # ---------------- PlanRecorder protocol ----------------

    def complete(self, report: PlanReport) -> None:
        if not isinstance(report, PlanReport):
            raise TypeError("CanonicalPlannerEventSink.complete requires a PlanReport")
        if report.plan_id != self._plan_id:
            raise ValueError("PlanReport.plan_id does not match this plan sink")
        if not self._started_persisted:
            raise PlannerRecordingError(
                f"complete() requires this sink to have persisted plan.started first: {self._plan_id}"
            )
        if not self._execution_completed_observed:
            raise PlannerRecordingError(
                f"complete() requires this sink to have observed a transient "
                f"ExecutionCompleted before a PlanReport can be trusted: {self._plan_id}"
            )
        self._append_terminal(
            RunEventType.PLAN_COMPLETED,
            {
                "plan_id": report.plan_id,
                "summary": report.summary,
                "steps": [_step_payload(step) for step in report.steps],
                "acceptance_criteria": list(report.acceptance_criteria),
                "risks": list(report.risks),
                "task_profile": {
                    "complexity": report.task_profile.complexity.value,
                    "scope": report.task_profile.scope.value,
                },
                "repository_fingerprint": report.repository_fingerprint,
                "task_sha256": report.task_sha256,
            },
        )

    def fail(self, plan_id: str, error_type: str, message: str) -> None:
        if plan_id != self._plan_id:
            raise ValueError("plan_id does not match this plan sink")
        if not self._started_persisted:
            raise PlannerRecordingError(
                f"fail() requires this sink to have persisted plan.started first: {self._plan_id}"
            )
        self._append_terminal(
            RunEventType.PLAN_FAILED,
            {
                "plan_id": plan_id,
                "error_type": _bounded_message(str(error_type)),
                "error_message": _bounded_message(str(message)),
            },
        )

    # ---------------- internals ----------------

    def _append_terminal(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._terminal_recorded:
            raise PlannerRecordingError(
                f"Plan attempt already has a terminal event: {self._plan_id}"
            )
        self._commit([RunEventSpec(type=event_type, payload=payload, correlation_id=self._plan_id, source=SOURCE)])
        self._terminal_recorded = True

    def _commit(self, specs: list[RunEventSpec]) -> None:
        committed, _ = self._runtime.record_many(
            run_id=self._run_id,
            specs=tuple(specs),
            expected_last_event_seq=self._expected_seq,
        )
        self._expected_seq = committed[-1].seq

    def _reject_reused_plan_id(self) -> None:
        after_seq = 0
        while True:
            page = self._runtime.events(self._run_id, after_seq=after_seq, limit=200)
            for existing in page.events:
                if (
                    existing.type == RunEventType.PLAN_STARTED
                    and existing.payload.get("plan_id") == self._plan_id
                ):
                    raise ValueError(f"plan_id already started in run: {self._plan_id}")
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
            correlation_id=self._plan_id,
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
            # Deliberately no run.waiting_user: the Planner's fail-closed
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
