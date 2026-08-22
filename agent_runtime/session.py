"""Native provider-independent worker agent loop."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent_runtime.backend import ModelBackend, ModelSession
from agent_runtime.errors import (
    AgentApprovalError,
    AgentBackendError,
    AgentIncompleteError,
    AgentInputError,
    AgentLifecycleError,
    AgentLimitError,
    AgentProtocolError,
    AgentRefusalError,
    AgentToolRuntimeError,
)
from agent_runtime.models import (
    AgentLimits,
    AgentOutcome,
    ApprovalDecision,
    ApprovalPause,
    ModelInputItem,
    ModelStopReason,
    ModelToolCall,
    ModelToolDefinition,
    ModelToolResult,
    ModelTurn,
    ModelUsage,
    ToolResultInput,
    UserInput,
)
from tool_runtime import (
    ApprovalGrant,
    Dispatcher,
    PermissionEffect,
    ToolCall,
    ToolExecutionContext,
    ToolRegistry,
)
from tool_runtime.errors import (
    ToolApprovalMismatchError,
    ToolApprovalRequiredError,
    ToolCallConsumedError,
    ToolDeniedError,
    ToolExecutionError,
    ToolInputValidationError,
    ToolNotFoundError,
    ToolPolicyError,
    ToolPreparedCallError,
)
from tool_runtime.policy import PolicyEvaluator


class AgentSessionState(StrEnum):
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class _PendingApproval:
    prepared: Any
    model_call: ModelToolCall
    tool_calls: tuple[ModelToolCall, ...]
    next_index: int
    results: list[ModelToolResult]


def _thaw_json(value: Any) -> Any:
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json(child) for child in value]
    return value


def _tool_definitions(registry: ToolRegistry) -> tuple[ModelToolDefinition, ...]:
    definitions = []
    for spec in registry.list_specs():
        raw_schema = _thaw_json(spec.input_schema)
        canonical = json.dumps(
            raw_schema,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        definitions.append(ModelToolDefinition(spec.name, spec.description, json.loads(canonical)))
    return tuple(definitions)


_RECOVERABLE_TOOL_ERRORS = (
    ToolNotFoundError,
    ToolInputValidationError,
    ToolDeniedError,
    ToolExecutionError,
    ToolPolicyError,
)
_INTERNAL_TOOL_ERRORS = (
    ToolPreparedCallError,
    ToolCallConsumedError,
    ToolApprovalMismatchError,
)


class AgentSession:
    def __init__(
        self,
        *,
        backend: ModelBackend,
        registry: ToolRegistry,
        policy: PolicyEvaluator,
        context: ToolExecutionContext,
        instructions: str = "",
        limits: AgentLimits | None = None,
    ) -> None:
        if not isinstance(instructions, str):
            raise AgentInputError("AgentSession.instructions string olmalı.")
        self._backend = backend
        self._registry = registry
        self._policy = policy
        self._context = context
        self._instructions = instructions
        self._limits = limits or AgentLimits()
        self._session_id = uuid.uuid4().hex
        self._dispatcher = Dispatcher(registry, policy)
        self._state = AgentSessionState.READY
        self._model: ModelSession | None = None
        self._pending: _PendingApproval | None = None
        self._last_turn: ModelTurn | None = None
        self._model_turns = 0
        self._tool_calls = 0
        self._tool_errors = 0
        self._consecutive_tool_errors = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_usd: float | None = None
        self._cost_complete = True

    @property
    def state(self) -> AgentSessionState:
        return self._state

    @property
    def dispatcher(self) -> Dispatcher:
        """The session-local Dispatcher, exposed for diagnostics/tests."""
        return self._dispatcher

    def start(self, task: str) -> AgentOutcome | ApprovalPause:
        if self._state is not AgentSessionState.READY:
            raise AgentLifecycleError(f"Session başlatılamaz; durum: {self._state.value}")
        if not isinstance(task, str) or not task.strip():
            raise AgentInputError("Agent task boş olmayan bir string olmalı.")
        try:
            self._model = self._backend.open_session(
                instructions=self._instructions,
                tools=_tool_definitions(self._registry),
                allow_parallel_tool_calls=False,
            )
        except Exception as exc:
            self._state = AgentSessionState.FAILED
            raise AgentBackendError(f"Model session açılamadı: {exc}") from exc
        if self._model is None or not callable(getattr(self._model, "respond", None)):
            self._state = AgentSessionState.FAILED
            raise AgentProtocolError("ModelBackend.open_session respond() sağlayan bir session döndürmeli.")
        self._state = AgentSessionState.RUNNING
        return self._respond((UserInput(task),))

    def resume(self, decision: ApprovalDecision) -> AgentOutcome | ApprovalPause:
        if self._state is not AgentSessionState.WAITING_APPROVAL or self._pending is None:
            raise AgentLifecycleError("Session şu anda approval beklemiyor.")
        pending = self._pending
        pause = self._approval_pause(pending.prepared)
        if (
            decision.call_id != pause.call_id
            or decision.fingerprint != pause.approval_fingerprint
            or decision.session_id != pause.session_id
        ):
            raise AgentApprovalError("ApprovalDecision bekleyen tool çağrısıyla eşleşmiyor.")
        prepared = pending.prepared
        if prepared is None:  # pragma: no cover - approval state invariant
            self._state = AgentSessionState.FAILED
            raise AgentToolRuntimeError("Approval pending çağrısında PreparedToolCall bulunamadı.")
        self._pending = None
        pending.prepared = None
        self._state = AgentSessionState.RUNNING
        if decision.approved:
            try:
                result = self._execute_prepared(pending.model_call, prepared, grant=True)
            except ToolApprovalRequiredError as exc:
                self._fail_internal_tool(exc)
            except _INTERNAL_TOOL_ERRORS as exc:
                self._fail_internal_tool(exc)
            except _RECOVERABLE_TOOL_ERRORS as exc:
                result = self._recoverable_result(pending.model_call, exc)
        else:
            result = ModelToolResult(
                pending.model_call.call_id,
                "Tool execution was denied by the user.",
                True,
            )
            self._record_tool_error()
        pending.results.append(result)
        pending.next_index += 1
        return self._continue_tool_batch(pending)

    def _respond(self, inputs: tuple[ModelInputItem, ...]) -> AgentOutcome | ApprovalPause:
        if self._model is None:  # pragma: no cover - lifecycle invariant
            raise AgentBackendError("Model session başlatılmamış.")
        if self._model_turns >= self._limits.max_model_turns:
            self._fail_limit("max_model_turns")
        self._model_turns += 1
        try:
            turn = self._model.respond(inputs)
        except Exception as exc:
            self._state = AgentSessionState.FAILED
            raise AgentBackendError(f"Model response başarısız: {exc}") from exc
        if not isinstance(turn, ModelTurn):
            self._state = AgentSessionState.FAILED
            raise AgentProtocolError("ModelSession.respond ModelTurn döndürmeli.")
        self._account_usage(turn.usage)
        self._last_turn = turn
        return self._process_turn(turn)

    def _process_turn(self, turn: ModelTurn) -> AgentOutcome | ApprovalPause:
        if turn.tool_calls:
            if turn.stop_reason is not ModelStopReason.TOOL_USE:
                self._fail_protocol("Tool çağrılı turn stop_reason=TOOL_USE olmalı.")
            return self._process_tool_batch(turn.tool_calls)
        if turn.stop_reason is ModelStopReason.COMPLETED:
            if not turn.text.strip():
                self._fail_protocol("Boş COMPLETED model turn kabul edilmiyor.")
            return self._complete(turn.text)
        if turn.stop_reason is ModelStopReason.TOOL_USE:
            self._fail_protocol("TOOL_USE turn en az bir tool call içermeli.")
        if turn.stop_reason is ModelStopReason.LENGTH:
            self._state = AgentSessionState.FAILED
            raise AgentIncompleteError("Model output length sınırına ulaştı.")
        if turn.stop_reason is ModelStopReason.REFUSAL:
            self._state = AgentSessionState.FAILED
            raise AgentRefusalError("Model görevi reddetti.")
        self._fail_protocol(f"Devam edilemeyen model stop reason: {turn.stop_reason.value}")

    def _process_tool_batch(self, calls: tuple[ModelToolCall, ...]) -> AgentOutcome | ApprovalPause:
        pending = _PendingApproval(
            prepared=None,
            model_call=calls[0],
            tool_calls=calls,
            next_index=0,
            results=[],
        )
        return self._continue_tool_batch(pending)

    def _continue_tool_batch(self, pending: _PendingApproval) -> AgentOutcome | ApprovalPause:
        while pending.next_index < len(pending.tool_calls):
            model_call = pending.tool_calls[pending.next_index]
            if pending.prepared is not None:
                prepared = pending.prepared
                pending.prepared = None
            else:
                if self._tool_calls >= self._limits.max_tool_calls:
                    self._fail_limit("max_tool_calls")
                self._tool_calls += 1
                try:
                    prepared = self._dispatcher.prepare(
                        ToolCall(model_call.call_id, model_call.name, model_call.arguments),
                        self._context,
                    )
                except _INTERNAL_TOOL_ERRORS as exc:
                    self._fail_internal_tool(exc)
                except _RECOVERABLE_TOOL_ERRORS as exc:
                    pending.results.append(self._recoverable_result(model_call, exc))
                    pending.next_index += 1
                    continue
                except Exception as exc:
                    self._fail_internal_tool(exc)

            try:
                result = self._execute_prepared(model_call, prepared, grant=False)
            except ToolApprovalRequiredError:
                pending.prepared = prepared
                pending.model_call = model_call
                self._pending = pending
                self._state = AgentSessionState.WAITING_APPROVAL
                return self._approval_pause(prepared)
            except _INTERNAL_TOOL_ERRORS as exc:
                self._fail_internal_tool(exc)
            except _RECOVERABLE_TOOL_ERRORS as exc:
                result = self._recoverable_result(model_call, exc)
            pending.results.append(result)
            pending.next_index += 1
        return self._respond(tuple(ToolResultInput(result) for result in pending.results))

    def _execute_prepared(self, model_call: ModelToolCall, prepared, *, grant: bool):
        approval_grant = None
        if grant:
            approval_grant = ApprovalGrant(
                prepared.call_id, prepared.approval_fingerprint
            )
        observation = self._dispatcher.execute(prepared, self._context, approval_grant)
        self._consecutive_tool_errors = 0
        return ModelToolResult(
            model_call.call_id,
            observation.content,
            False,
            observation.metadata,
        )

    def _recoverable_result(self, model_call: ModelToolCall, exc: Exception) -> ModelToolResult:
        self._record_tool_error()
        message = str(exc).replace("\x00", "")[:2000]
        return ModelToolResult(model_call.call_id, f"Tool error: {message}", True)

    def _record_tool_error(self) -> None:
        self._tool_errors += 1
        self._consecutive_tool_errors += 1
        if self._consecutive_tool_errors > self._limits.max_consecutive_tool_errors:
            self._fail_limit("max_consecutive_tool_errors")

    def _account_usage(self, usage: ModelUsage) -> None:
        self._input_tokens += usage.input_tokens
        self._output_tokens += usage.output_tokens
        if usage.cost_usd is None:
            self._cost_complete = False
            self._cost_usd = None
        elif self._cost_complete:
            self._cost_usd = (self._cost_usd or 0.0) + float(usage.cost_usd)

    def _approval_pause(self, prepared) -> ApprovalPause:
        return ApprovalPause(
            prepared.call_id,
            prepared.tool_name,
            prepared.approval_fingerprint,
            prepared.permission_requests,
            self._session_id,
        )

    def _complete(self, final_text: str) -> AgentOutcome:
        self._state = AgentSessionState.COMPLETED
        return AgentOutcome(
            final_text=final_text,
            model_turns=self._model_turns,
            tool_calls=self._tool_calls,
            tool_errors=self._tool_errors,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cost_usd=self._cost_usd,
        )

    def _fail_protocol(self, message: str):
        self._state = AgentSessionState.FAILED
        raise AgentProtocolError(message)

    def _fail_internal_tool(self, exc: Exception):
        self._state = AgentSessionState.FAILED
        raise AgentToolRuntimeError(f"Tool runtime invariant failed: {exc}") from exc

    def _fail_limit(self, name: str):
        self._state = AgentSessionState.FAILED
        raise AgentLimitError(f"Agent limit exceeded: {name}")
