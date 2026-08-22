"""run_runtime.models — saf Python alan modeli (Task/Run kayıtları, durum/aşama enumları).

Bu modül sqlite3, PySide6, webhost, project_runner, providers veya herhangi bir
UI kodu import ETMEZ; tamamen bağımsız, deterministik, test edilebilir bir
alan (domain) katmanıdır.

RunStatus (kullanıcıya görünen genel akıbet) ile RunPhase (yürütmenin şu anki
adımı) kasıtlı olarak AYRI enumlardır — tek bir enuma sıkıştırılmaz, çünkü
örn. 'running' durumu 'planning'/'executing'/'reviewing' gibi birden çok
aşamayı kapsayabilir.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from run_runtime.jsonutil import canonical_dict_copy, canonical_optional_dict_copy


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_task_id() -> str:
    return f"task_{uuid.uuid4()}"


def new_run_id() -> str:
    return f"run_{uuid.uuid4()}"


def new_event_id() -> str:
    return f"evt_{uuid.uuid4()}"


def to_canonical_utc(dt: datetime | None, field_name: str) -> datetime | None:
    """`dt`yi kanonik UTC'ye normalize eder; None ise None döner.

    Bir datetime yalnızca `utcoffset() is not None` iken gerçekten "aware"
    sayılır — salt `tzinfo is not None` kontrolü YETERSİZDİR (bazı özel tzinfo
    uygulamaları None utcoffset() döndürebilir ve yine de naive davranır).
    Aware ama UTC olmayan bir girdi (örn. +03:00) sessizce eşdeğer UTC'ye
    çevrilir; naive girdi REDDEDİLİR. Sıralama UUID'lerden DEĞİL yalnızca
    (run_id, seq) çiftinden türetilir; bu fonksiyon yalnızca depolanan zaman
    damgalarının yerel saat dilimine bağımlı hale gelmesini engeller.
    """
    if dt is None:
        return None
    if dt.utcoffset() is None:
        raise ValueError(f"{field_name} tz-aware (UTC) olmalı; naive datetime kabul edilmiyor.")
    return dt.astimezone(timezone.utc)


def require_canonical_utc(dt: datetime, field_name: str) -> datetime:
    """to_canonical_utc'nin None'a izin VERMEYEN sürümü (zorunlu alanlar için)."""
    if dt is None:
        raise ValueError(f"{field_name} None olamaz.")
    return to_canonical_utc(dt, field_name)


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class RunPhase(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    READY = "ready"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    RESTORED = "restored"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    project_root: str
    prompt: str
    created_at: datetime

    def __post_init__(self) -> None:
        # object.__setattr__: frozen dataclass'ta post_init normalizasyonu için
        # standart, belgelenen kaçış yolu.
        object.__setattr__(self, "created_at", require_canonical_utc(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    task_id: str

    status: RunStatus
    phase: RunPhase

    attempt: int
    retry_of_run_id: str | None

    routing: dict[str, Any]
    budget: dict[str, Any] | None
    workspace_snapshot: dict[str, Any] | None

    last_event_seq: int

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    latency_s: float

    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    error_code: str | None
    error_message: str | None

    def __post_init__(self) -> None:
        """Nihai değişmez (invariant) sınırı — RunRecord.new() gibi fabrikalara BAĞIMLI DEĞİLDİR.

        Doğrudan `RunRecord(...)` çağrısıyla oluşturulan bir kayıt bile burada
        aynı kanonik UTC + katı-JSON doğrulamasından/derin kopyalamadan geçer;
        örn. `RunRecord(..., routing={1: "bad"})` EventValidationError ile
        reddedilir.
        """
        object.__setattr__(self, "created_at", require_canonical_utc(self.created_at, "created_at"))
        object.__setattr__(self, "started_at", to_canonical_utc(self.started_at, "started_at"))
        object.__setattr__(self, "finished_at", to_canonical_utc(self.finished_at, "finished_at"))
        object.__setattr__(self, "routing", canonical_dict_copy(self.routing, field="routing"))
        object.__setattr__(self, "budget", canonical_optional_dict_copy(self.budget, field="budget"))
        object.__setattr__(
            self,
            "workspace_snapshot",
            canonical_optional_dict_copy(self.workspace_snapshot, field="workspace_snapshot"),
        )

    @classmethod
    def new(
        cls,
        *,
        run_id: str,
        task_id: str,
        attempt: int = 1,
        retry_of_run_id: str | None = None,
        routing: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        workspace_snapshot: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> "RunRecord":
        """Yeni bir Run'ın başlangıç izdüşümü: status=created, phase=created, last_event_seq=0.

        routing/budget/workspace_snapshot kanonik JSON sözleşmesine göre
        doğrulanır (bkz. run_runtime.jsonutil) ve derin bir kopyası saklanır —
        örn. routing=[] veya routing={1: "x"} EventValidationError ile
        reddedilir; json.dumps'tan sızan ham bir TypeError ASLA görülmez.
        """
        return cls(
            run_id=run_id,
            task_id=task_id,
            status=RunStatus.CREATED,
            phase=RunPhase.CREATED,
            attempt=attempt,
            retry_of_run_id=retry_of_run_id,
            routing=canonical_dict_copy(routing, field="routing") if routing is not None else {},
            budget=canonical_optional_dict_copy(budget, field="budget"),
            workspace_snapshot=canonical_optional_dict_copy(
                workspace_snapshot, field="workspace_snapshot"
            ),
            last_event_seq=0,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cost_usd=0.0,
            latency_s=0.0,
            created_at=created_at or utcnow(),
            started_at=None,
            finished_at=None,
            error_code=None,
            error_message=None,
        )
