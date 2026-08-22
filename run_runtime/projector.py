"""run_runtime.projector — saf, deterministik Run projeksiyon mantığı.

SQLite BİLMEZ. `project_run(current, event) -> RunRecord` biçiminde saf bir
fonksiyondur: aynı (current, event) çifti için her zaman aynı sonucu üretir,
hiçbir I/O yapmaz, `datetime.now()` ÇAĞIRMAZ (zaman damgaları event.created_at
üzerinden alınır — böylece testler deterministik kalır).

last_event_seq bilerek DOKUNULMAZ: sıra numarası atama/ilerletme yalnızca
kalıcılık katmanının (RunStore.append_event) sorumluluğundadır. Bilinmeyen bir
event türü projeksiyonu OLDUĞU GİBİ bırakır — böylece gelecekteki yeni event
türleri, bu modül güncellenmeden de durabilir/depolanabilir (ileriye dönük
uyumluluk). Bilinen bir event türü ile geçersiz/eksik bir payload görülürse
RunProjectionError fırlatılır.

Aşırı katı bir sonlu durum makinesi (FSM) KASITLI OLARAK kurulmaz — terminal
bir durumdan sonra gelen olaylar (örn. checkpoint.restored) yine de sunum
aşamasını güncelleyebilir; bu basitlik gelecekteki event evrimini kolaylaştırır.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Callable

from run_runtime.errors import RunProjectionError
from run_runtime.events import RunEvent, RunEventType
from run_runtime.models import RunPhase, RunRecord, RunStatus

_Handler = Callable[[RunRecord, RunEvent], RunRecord]


def project_run(current: RunRecord, event: RunEvent) -> RunRecord:
    """`event` türüne göre yeni bir RunRecord üretir; `current` DEĞİŞTİRİLMEZ."""
    handler = _HANDLERS.get(event.type)
    if handler is None:
        return current
    return handler(current, event)


def _phase_from_payload(payload: dict[str, Any]) -> RunPhase:
    raw = payload.get("phase")
    if not isinstance(raw, str):
        raise RunProjectionError(f"run.phase_changed 'phase' bir string olmalı: {raw!r}")
    try:
        return RunPhase(raw)
    except ValueError as exc:
        raise RunProjectionError(f"Geçersiz phase: {raw!r}") from exc


def _optional_str(payload: dict[str, Any], key: str, *, event_type: str) -> str | None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise RunProjectionError(f"{event_type} '{key}' bir string olmalı: {value!r}")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RunProjectionError(f"usage.recorded '{field}' bir tam sayı olmalı: {value!r}")
    if value < 0:
        raise RunProjectionError(f"usage.recorded '{field}' negatif olamaz: {value!r}")
    return value


def _non_negative_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunProjectionError(f"usage.recorded '{field}' bir sayı olmalı: {value!r}")
    as_float = float(value)
    if not math.isfinite(as_float):
        raise RunProjectionError(f"usage.recorded '{field}' sonlu olmayan bir sayı: {value!r}")
    if as_float < 0:
        raise RunProjectionError(f"usage.recorded '{field}' negatif olamaz: {value!r}")
    return as_float


def _on_run_created(current: RunRecord, event: RunEvent) -> RunRecord:
    return dataclasses.replace(current, status=RunStatus.CREATED, phase=RunPhase.CREATED)


def _on_run_started(current: RunRecord, event: RunEvent) -> RunRecord:
    return dataclasses.replace(
        current,
        status=RunStatus.RUNNING,
        phase=RunPhase.STARTING,
        started_at=current.started_at or event.created_at,
    )


def _on_phase_changed(current: RunRecord, event: RunEvent) -> RunRecord:
    return dataclasses.replace(current, phase=_phase_from_payload(event.payload))


def _on_waiting_user(current: RunRecord, event: RunEvent) -> RunRecord:
    return dataclasses.replace(current, status=RunStatus.WAITING_USER, phase=RunPhase.READY)


def _on_resumed(current: RunRecord, event: RunEvent) -> RunRecord:
    if current.status is not RunStatus.WAITING_USER:
        raise RunProjectionError(
            f"run.resumed yalnızca WAITING_USER durumunda geçerli (status={current.status})"
        )
    return dataclasses.replace(current, status=RunStatus.RUNNING, phase=RunPhase.EXECUTING)


def _on_completed(current: RunRecord, event: RunEvent) -> RunRecord:
    return dataclasses.replace(
        current,
        status=RunStatus.SUCCEEDED,
        phase=RunPhase.DONE,
        finished_at=current.finished_at or event.created_at,
    )


def _on_failed(current: RunRecord, event: RunEvent) -> RunRecord:
    code = _optional_str(event.payload, "error_code", event_type="run.failed")
    message = _optional_str(event.payload, "error_message", event_type="run.failed")
    return dataclasses.replace(
        current,
        status=RunStatus.FAILED,
        phase=RunPhase.ERROR,
        finished_at=current.finished_at or event.created_at,
        error_code=code,
        error_message=message,
    )


def _on_cancelled(current: RunRecord, event: RunEvent) -> RunRecord:
    return dataclasses.replace(
        current,
        status=RunStatus.CANCELLED,
        phase=RunPhase.DONE,
        finished_at=current.finished_at or event.created_at,
    )


def _on_interrupted(current: RunRecord, event: RunEvent) -> RunRecord:
    return dataclasses.replace(
        current,
        status=RunStatus.INTERRUPTED,
        phase=RunPhase.ERROR,
        finished_at=current.finished_at or event.created_at,
    )


def _on_proposal_applied(current: RunRecord, event: RunEvent) -> RunRecord:
    return dataclasses.replace(
        current,
        status=RunStatus.SUCCEEDED,
        phase=RunPhase.APPLIED,
        finished_at=current.finished_at or event.created_at,
    )


def _on_proposal_rejected(current: RunRecord, event: RunEvent) -> RunRecord:
    return dataclasses.replace(
        current,
        status=RunStatus.SUCCEEDED,
        phase=RunPhase.REJECTED,
        finished_at=current.finished_at or event.created_at,
    )


def _on_checkpoint_restored(current: RunRecord, event: RunEvent) -> RunRecord:
    return dataclasses.replace(current, status=RunStatus.SUCCEEDED, phase=RunPhase.RESTORED)


def _on_usage_recorded(current: RunRecord, event: RunEvent) -> RunRecord:
    payload = event.payload
    prompt_tokens = _non_negative_int(payload.get("prompt_tokens", 0), "prompt_tokens")
    completion_tokens = _non_negative_int(payload.get("completion_tokens", 0), "completion_tokens")
    total_tokens = _non_negative_int(
        payload.get("total_tokens", prompt_tokens + completion_tokens), "total_tokens"
    )
    raw_cost = payload.get("cost_usd", 0)
    cost_usd = 0.0 if raw_cost is None else _non_negative_number(raw_cost, "cost_usd")
    latency_s = _non_negative_number(payload.get("latency_s", 0), "latency_s")
    return dataclasses.replace(
        current,
        prompt_tokens=current.prompt_tokens + prompt_tokens,
        completion_tokens=current.completion_tokens + completion_tokens,
        total_tokens=current.total_tokens + total_tokens,
        cost_usd=current.cost_usd + cost_usd,
        latency_s=current.latency_s + latency_s,
    )


_HANDLERS: dict[str, _Handler] = {
    RunEventType.RUN_CREATED: _on_run_created,
    RunEventType.RUN_STARTED: _on_run_started,
    RunEventType.RUN_PHASE_CHANGED: _on_phase_changed,
    RunEventType.RUN_WAITING_USER: _on_waiting_user,
    RunEventType.RUN_RESUMED: _on_resumed,
    RunEventType.RUN_COMPLETED: _on_completed,
    RunEventType.RUN_FAILED: _on_failed,
    RunEventType.RUN_CANCELLED: _on_cancelled,
    RunEventType.RUN_INTERRUPTED: _on_interrupted,
    RunEventType.PROPOSAL_APPLIED: _on_proposal_applied,
    RunEventType.PROPOSAL_REJECTED: _on_proposal_rejected,
    RunEventType.CHECKPOINT_RESTORED: _on_checkpoint_restored,
    RunEventType.USAGE_RECORDED: _on_usage_recorded,
}
