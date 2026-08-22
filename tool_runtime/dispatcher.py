"""Validation, authorization, and exactly-once tool execution ordering."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from tool_runtime.errors import (
    ToolApprovalMismatchError,
    ToolApprovalRequiredError,
    ToolCallConsumedError,
    ToolDeniedError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolPreparedCallError,
    ToolPolicyError,
)
from tool_runtime.models import (
    PermissionEffect,
    PermissionRequest,
    ToolCall,
    ToolExecutionContext,
    ToolObservation,
)
from tool_runtime.policy import PolicyDecision, PolicyEvaluator
from tool_runtime.registry import ToolRegistry
from tool_runtime.schema import validate_arguments


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    call_id: str
    fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id.strip():
            raise ToolApprovalMismatchError("ApprovalGrant.call_id boş olmayan bir string olmalı.")
        if not isinstance(self.fingerprint, str) or not self.fingerprint.strip():
            raise ToolApprovalMismatchError("ApprovalGrant.fingerprint boş olmayan bir string olmalı.")


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    call_id: str
    tool_name: str
    arguments_json: str
    permission_requests: tuple[PermissionRequest, ...]
    policy_decision: PolicyDecision
    approval_fingerprint: str


def _fingerprint(tool_name: str, arguments_json: str, requests: tuple[PermissionRequest, ...]) -> str:
    payload = {
        "tool_name": tool_name,
        "arguments_json": arguments_json,
        "permission_requests": sorted(
            (request.action, request.resource) for request in requests
        ),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class _PreparedRecord:
    prepared: PreparedToolCall
    context: ToolExecutionContext
    consumed: bool = False


class Dispatcher:
    def __init__(self, registry: ToolRegistry, policy: PolicyEvaluator) -> None:
        self._registry = registry
        self._policy = policy
        self._state_lock = threading.Lock()
        # Records are keyed by call_id so an approval identity cannot be
        # reused by a second prepared call. Each record strongly retains the
        # exact prepared object for identity verification.
        self._prepared_records: dict[str, _PreparedRecord] = {}

    def prepare(self, call: ToolCall, context: ToolExecutionContext) -> PreparedToolCall:
        try:
            spec = self._registry.get(call.tool_name)
        except ToolNotFoundError:
            raise
        arguments_json, _ = validate_arguments(spec.input_schema, call.arguments)
        try:
            resolved = spec.permission_resolver(json.loads(arguments_json), context)
        except Exception as exc:
            raise ToolPolicyError(f"Tool izinleri çözümlenemedi ({call.tool_name}): {exc}") from exc
        try:
            requests = tuple(resolved)
        except (TypeError, ValueError) as exc:
            raise ToolPolicyError(f"Tool izin resolver sonucu iterable olmalı: {exc}") from exc
        if not requests:
            raise ToolPolicyError(f"Tool en az bir PermissionRequest bildirmeli: {call.tool_name}")
        if any(not isinstance(request, PermissionRequest) for request in requests):
            raise ToolPolicyError(f"Tool resolver yalnızca PermissionRequest döndürmeli: {call.tool_name}")
        try:
            decision = self._policy.evaluate(requests)
        except Exception as exc:
            if isinstance(exc, ToolPolicyError):
                raise
            raise ToolPolicyError(f"Tool policy değerlendirmesi başarısız: {exc}") from exc
        prepared = PreparedToolCall(
            call_id=call.call_id,
            tool_name=call.tool_name,
            arguments_json=arguments_json,
            permission_requests=requests,
            policy_decision=decision,
            approval_fingerprint=_fingerprint(call.tool_name, arguments_json, requests),
        )
        with self._state_lock:
            if call.call_id in self._prepared_records:
                raise ToolPreparedCallError(
                    f"call_id bu Dispatcher içinde zaten hazırlanmış: {call.call_id}"
                )
            self._prepared_records[call.call_id] = _PreparedRecord(
                prepared=prepared, context=context
            )
        return prepared

    @staticmethod
    def _same_context(left: ToolExecutionContext, right: ToolExecutionContext) -> bool:
        return (
            left.workspace is right.workspace
            and left.run_id == right.run_id
            and left.execution_id == right.execution_id
        )

    def _record_for_locked(
        self, prepared: PreparedToolCall, context: ToolExecutionContext
    ) -> _PreparedRecord:
        if not isinstance(context, ToolExecutionContext):
            raise ToolPreparedCallError("Yürütme bağlamı ToolExecutionContext olmalı.")
        record = self._prepared_records.get(prepared.call_id)
        if record is None or record.prepared is not prepared:
            raise ToolPreparedCallError(
                "PreparedToolCall bu Dispatcher tarafından üretilmemiş."
            )
        if not self._same_context(record.context, context):
            raise ToolPreparedCallError(
                "PreparedToolCall farklı bir ToolExecutionContext ile yürütülemez."
            )
        if record.consumed:
            raise ToolCallConsumedError(
                f"PreparedToolCall zaten tüketilmiş: {prepared.call_id}"
            )
        return record

    def execute(
        self,
        prepared: PreparedToolCall,
        context: ToolExecutionContext,
        grant: ApprovalGrant | None = None,
    ) -> ToolObservation:
        with self._state_lock:
            record = self._record_for_locked(prepared, context)
            effect = prepared.policy_decision.effect
            if effect is PermissionEffect.DENY:
                raise ToolDeniedError(
                    f"Tool çağrısı policy tarafından reddedildi: {prepared.tool_name}"
                )
            if effect is PermissionEffect.ASK:
                if grant is None:
                    raise ToolApprovalRequiredError(
                        f"Tool çağrısı için açık onay gerekli: {prepared.tool_name}"
                    )
                if (
                    grant.call_id != prepared.call_id
                    or grant.fingerprint != prepared.approval_fingerprint
                ):
                    raise ToolApprovalMismatchError(
                        "ApprovalGrant hazırlanan tool çağrısıyla eşleşmiyor."
                    )

            arguments = json.loads(prepared.arguments_json)
            executor = self._registry._executor_for(prepared.tool_name)
            # Consume before releasing the lock and entering executor code.
            # Any external side effect followed by an exception must not be
            # replayed automatically.
            record.consumed = True

        try:
            observation = executor.execute(arguments, context)
        except Exception as exc:
            raise ToolExecutionError(
                f"Tool yürütmesi başarısız ({prepared.tool_name}): {exc}"
            ) from exc
        if not isinstance(observation, ToolObservation):
            raise ToolExecutionError(
                f"Tool geçersiz sonuç döndürdü ({prepared.tool_name}); ToolObservation gerekli."
            )
        return observation
