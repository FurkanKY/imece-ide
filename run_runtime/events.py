"""run_runtime.events — kanonik RunEvent modeli ve event türü sabitleri.

Kalıcı yürütme geçmişinin biricik doğruluk kaynağı budur: run_events tablosu
bu türden satırları ekleme-only (append-only) tutar. Run projeksiyonu
(RunRecord) bu akışın türetilmiş, hızlı-okuma bir izdüşümüdür (bkz.
run_runtime.projector).

Yalnızca bu temel ve bir sonraki "legacy adapter" dilimi için gereken event
türleri tanımlanır — düzinelerce varsayımsal gelecek olay türü EKLENMEZ.
Depolama katmanı bilinmeyen/gelecekteki event type string'leriyle de ileriye
dönük uyumlu kalır (bkz. store.append_event, projector.project_run).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from run_runtime.errors import EventValidationError
from run_runtime.jsonutil import canonical_dict_copy
from run_runtime.models import new_event_id, require_canonical_utc, utcnow

CURRENT_SCHEMA_VERSION = 1


class RunEventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_PHASE_CHANGED = "run.phase_changed"

    RUN_WAITING_USER = "run.waiting_user"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_INTERRUPTED = "run.interrupted"
    RUN_RESUMED = "run.resumed"

    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    MODEL_STARTED = "model.started"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_INTERRUPTED = "tool.interrupted"
    PERMISSION_REQUESTED = "permission.requested"
    PERMISSION_RESOLVED = "permission.resolved"

    EXECUTION_STARTED = "execution.started"
    EXECUTION_OUTPUT = "execution.output"
    EXECUTION_COMPLETED = "execution.completed"
    EXECUTION_FAILED = "execution.failed"

    PLAN_STARTED = "plan.started"
    PLAN_COMPLETED = "plan.completed"
    PLAN_FAILED = "plan.failed"
    PLAN_INTERRUPTED = "plan.interrupted"

    USAGE_RECORDED = "usage.recorded"

    CHANGE_PROPOSED = "change.proposed"
    PROPOSAL_READY = "proposal.ready"
    PROPOSAL_APPLIED = "proposal.applied"
    PROPOSAL_REJECTED = "proposal.rejected"

    REVIEW_STARTED = "review.started"
    REVIEW_COMPLETED = "review.completed"
    REVIEW_FAILED = "review.failed"
    REVIEW_INTERRUPTED = "review.interrupted"

    VERIFICATION_STARTED = "verification.started"
    VERIFICATION_CHECK_STARTED = "verification.check_started"
    VERIFICATION_CHECK_COMPLETED = "verification.check_completed"
    VERIFICATION_CHECK_FAILED = "verification.check_failed"
    VERIFICATION_CHECK_INTERRUPTED = "verification.check_interrupted"
    VERIFICATION_COMPLETED = "verification.completed"
    VERIFICATION_INTERRUPTED = "verification.interrupted"

    CHECKPOINT_CREATED = "checkpoint.created"
    CHECKPOINT_RESTORED = "checkpoint.restored"

    FIX_LOOP_STARTED = "fix_loop.started"
    FIX_ATTEMPT_STARTED = "fix_attempt.started"
    FIX_ATTEMPT_COMPLETED = "fix_attempt.completed"
    FIX_ATTEMPT_INTERRUPTED = "fix_attempt.interrupted"
    FIX_LOOP_COMPLETED = "fix_loop.completed"
    FIX_LOOP_EXHAUSTED = "fix_loop.exhausted"
    FIX_LOOP_FAILED = "fix_loop.failed"
    FIX_LOOP_INTERRUPTED = "fix_loop.interrupted"

@dataclass(frozen=True, slots=True)
class RunEventSpec:
    """A validated event request used by atomic batch append operations."""

    type: str
    payload: dict[str, Any]
    schema_version: int = CURRENT_SCHEMA_VERSION
    execution_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    source: str = "system"
    created_at: datetime | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type:
            raise EventValidationError("RunEventSpec.type boş olmayan bir string olmalı.")
        if not isinstance(self.source, str) or not self.source:
            raise EventValidationError("RunEventSpec.source boş olmayan bir string olmalı.")
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise EventValidationError("RunEventSpec.schema_version integer olmalı.")
        if self.schema_version < 1:
            raise EventValidationError("RunEventSpec.schema_version >= 1 olmalı.")
        object.__setattr__(self, "payload", validate_event_payload(self.payload))

@dataclass(frozen=True, slots=True)
class RunEvent:
    event_id: str
    run_id: str
    seq: int

    type: str
    schema_version: int

    created_at: datetime

    execution_id: str | None
    turn_id: str | None
    item_id: str | None

    causation_id: str | None
    correlation_id: str | None

    source: str

    payload: dict[str, Any]

    def __post_init__(self) -> None:
        """Nihai değişmez (invariant) sınırı — build_event() gibi fabrikalara BAĞIMLI DEĞİLDİR.

        Doğrudan `RunEvent(...)` çağrısıyla oluşturulan bir olay bile burada
        aynı kanonik UTC + katı-JSON doğrulamasından/derin kopyalamadan geçer;
        örn. `RunEvent(..., payload={"x": float("nan")})` EventValidationError
        ile reddedilir.
        """
        object.__setattr__(self, "created_at", require_canonical_utc(self.created_at, "created_at"))
        object.__setattr__(self, "payload", canonical_dict_copy(self.payload, field="payload"))


def validate_event_payload(payload: Any) -> dict[str, Any]:
    """payload'ın kanonik JSON sözleşmesine uyan bir dict olduğunu doğrular.

    pickle veya keyfi Python nesneleri, NaN/Infinity, str olmayan anahtarlar
    KABUL EDİLMEZ (bkz. run_runtime.jsonutil). Döndürülen değer, çağıranın
    elindeki orijinal nesneyle paylaşılmayan derin bir kopyadır.
    """
    return canonical_dict_copy(payload, field="payload")


def build_event(
    *,
    run_id: str,
    seq: int,
    type: str,
    payload: dict[str, Any],
    schema_version: int = CURRENT_SCHEMA_VERSION,
    execution_id: str | None = None,
    turn_id: str | None = None,
    item_id: str | None = None,
    causation_id: str | None = None,
    correlation_id: str | None = None,
    source: str = "system",
    created_at: datetime | None = None,
    event_id: str | None = None,
) -> RunEvent:
    """Doğrulanmış, kalıcılığa hazır bir RunEvent üretir (henüz hiçbir yere YAZMAZ).

    `seq` çağıran tarafından (normalde RunStore.append_event içinde,
    last_event_seq + 1 olarak) belirlenir — sıralama yalnızca (run_id, seq)
    çiftinden gelir, event_id'nin UUID'sinden ASLA türetilmez.
    """
    if not run_id:
        raise EventValidationError("run_id boş olamaz.")
    if not isinstance(type, str) or not type:
        raise EventValidationError(f"type geçerli, boş olmayan bir string olmalı: {type!r}")
    if not isinstance(source, str) or not source:
        raise EventValidationError(f"source geçerli, boş olmayan bir string olmalı: {source!r}")
    # bool, int'in alt sınıfı olduğundan `isinstance(True, int)` True döner;
    # seq/schema_version'ın gerçek tam sayı olmasını (True/False DEĞİL) ayrıca
    # doğrulamak gerekir.
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise EventValidationError(f"seq bir tam sayı olmalı: {seq!r}")
    if seq < 1:
        raise EventValidationError(f"seq >= 1 olmalı: {seq}")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise EventValidationError(f"schema_version bir tam sayı olmalı: {schema_version!r}")
    if schema_version < 1:
        raise EventValidationError(f"schema_version >= 1 olmalı: {schema_version}")
    canonical_payload = validate_event_payload(payload)
    return RunEvent(
        event_id=event_id or new_event_id(),
        run_id=run_id,
        seq=seq,
        type=type,
        schema_version=schema_version,
        created_at=created_at or utcnow(),
        execution_id=execution_id,
        turn_id=turn_id,
        item_id=item_id,
        causation_id=causation_id,
        correlation_id=correlation_id,
        source=source,
        payload=canonical_payload,
    )
