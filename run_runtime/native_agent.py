"""Canonical RunEvent adapter for the transient native AgentEvent port."""

from __future__ import annotations

from dataclasses import asdict
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
from run_runtime.events import RunEventSpec, RunEventType
from run_runtime.models import RunStatus
from run_runtime.service import RunRuntime


SOURCE = "native_agent"


def _error_payload(event: ModelFailed | ToolFailed | ExecutionFailed) -> dict[str, Any]:
    payload = {
        "error_type": event.error_type,
        "message": event.message[:2000],
    }
    if isinstance(event, ToolFailed):
        payload.update({
            "stage": event.stage,
            "recoverable": event.recoverable,
        })
    return payload


class CanonicalAgentEventSink:
    """Maps transient AgentSession events to one optimistic canonical trajectory."""

    def __init__(
        self,
        runtime: RunRuntime,
        run_id: str,
        *,
        execution_id: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._run_id = run_id
        run = runtime.get_run(run_id)
        if run.status is not RunStatus.RUNNING:
            raise ValueError(f"Native agent sink requires RUNNING run, got {run.status}")
        self._execution_id = execution_id
        self._expected_seq = run.last_event_seq

    @property
    def execution_id(self) -> str | None:
        return self._execution_id

    @property
    def expected_last_event_seq(self) -> int:
        return self._expected_seq

    def emit(self, event: AgentEvent) -> None:
        if not isinstance(event, AgentEvent):
            raise TypeError("CanonicalAgentEventSink accepts AgentEvent values only")
        if self._execution_id is None:
            self._execution_id = event.execution_id
        if event.execution_id != self._execution_id:
            raise ValueError("Agent event execution_id does not match this sink")
        specs = self._specs(event)
        if not specs:
            raise ValueError(f"Unsupported native agent event: {type(event).__name__}")
        committed, _ = self._runtime.record_many(
            run_id=self._run_id,
            specs=tuple(specs),
            expected_last_event_seq=self._expected_seq,
        )
        self._expected_seq = committed[-1].seq

    def _spec(
        self,
        event: AgentEvent,
        type: str,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
        item_id: str | None = None,
    ) -> RunEventSpec:
        return RunEventSpec(
            type=type,
            payload=payload,
            execution_id=event.execution_id,
            turn_id=turn_id,
            item_id=item_id,
            correlation_id=event.execution_id,
            source=SOURCE,
        )

    def _specs(self, event: AgentEvent) -> list[RunEventSpec]:
        if isinstance(event, ExecutionStarted):
            return [self._spec(event, RunEventType.EXECUTION_STARTED, {"task": event.task})]
        if isinstance(event, TurnStarted):
            return [self._spec(event, RunEventType.TURN_STARTED, {
                "turn_index": event.turn_index,
            }, turn_id=event.turn_id)]
        if isinstance(event, ModelStarted):
            return [self._spec(event, RunEventType.MODEL_STARTED, {
                "turn_index": event.turn_index,
            }, turn_id=event.turn_id, item_id=event.item_id)]
        if isinstance(event, ModelCompleted):
            specs = [self._spec(event, RunEventType.MODEL_COMPLETED, {
                "text": event.text,
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
            return [
                self._spec(event, RunEventType.PERMISSION_REQUESTED, {
                    "call_id": event.call_id,
                    "tool_name": event.tool_name,
                    "fingerprint": event.fingerprint,
                    "permission_requests": [asdict(request) for request in event.permission_requests],
                }, turn_id=event.turn_id, item_id=event.item_id),
                self._spec(event, RunEventType.RUN_WAITING_USER, {}, turn_id=event.turn_id),
            ]
        if isinstance(event, ApprovalResolved):
            return [
                self._spec(event, RunEventType.PERMISSION_RESOLVED, {
                    "call_id": event.call_id,
                    "tool_name": event.tool_name,
                    "fingerprint": event.fingerprint,
                    "approved": event.approved,
                }, turn_id=event.turn_id, item_id=event.item_id),
                self._spec(event, RunEventType.RUN_RESUMED, {}, turn_id=event.turn_id),
            ]
        if isinstance(event, TurnCompleted):
            return [self._spec(event, RunEventType.TURN_COMPLETED, {}, turn_id=event.turn_id)]
        if isinstance(event, ExecutionCompleted):
            return [self._spec(event, RunEventType.EXECUTION_COMPLETED, {
                "final_text": event.final_text,
                "model_turns": event.model_turns,
                "tool_calls": event.tool_calls,
                "tool_errors": event.tool_errors,
                "input_tokens": event.input_tokens,
                "output_tokens": event.output_tokens,
                "cost_usd": event.cost_usd,
            })]
        if isinstance(event, ExecutionFailed):
            return [self._spec(event, RunEventType.EXECUTION_FAILED, _error_payload(event))]
        return []
