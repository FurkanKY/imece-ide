"""run_runtime — kalıcı Task/Run/Event alan modeli, SQLite mağazası ve projeksiyon motoru.

Bu milestone SADECE altyapıdır: mevcut project_runner.py/webhost/UI akışına
HENÜZ BAĞLANMAZ. Temel kural:

    Kanonik yürütme geçmişi, ekleme-only (append-only) bir olay günlüğüdür.
    Bir Run'ın güncel durumu, bu günlüğün bir izdüşümüdür.

Saf event-sourcing çatısı KURULMAZ; pragmatik bir model kullanılır:
tasks tablosu + runs tablosu (hızlı-okuma izdüşümü) + run_events tablosu
(kanonik, ekleme-only geçmiş). Bir event'in eklenmesi ile Run izdüşümünün
güncellenmesi her zaman aynı SQLite işleminde atomik olarak gerçekleşir
(bkz. run_runtime.store.RunStore.append_event).
"""

from run_runtime.bus import EventBus, EventNotice, EventSubscription
from run_runtime.errors import (
    EventConflictError,
    EventSequenceError,
    EventStreamClosedError,
    EventValidationError,
    InvalidRunStateError,
    RunNotFoundError,
    RunProjectionError,
    RunRuntimeError,
    RunStoreError,
    TaskNotFoundError,
)
from run_runtime.events import CURRENT_SCHEMA_VERSION, RunEvent, RunEventType, build_event
from run_runtime.models import RunPhase, RunRecord, RunStatus, TaskRecord
from run_runtime.projector import project_run
from run_runtime.recovery import RecoveryReport, recover_running_runs
from run_runtime.service import DurableEventTail, RunRuntime
from run_runtime.store import EventPage, RunStore

__all__ = [
    "RunRuntimeError",
    "RunNotFoundError",
    "TaskNotFoundError",
    "EventValidationError",
    "EventSequenceError",
    "EventConflictError",
    "EventStreamClosedError",
    "InvalidRunStateError",
    "RunStoreError",
    "RunProjectionError",
    "RunEvent",
    "RunEventType",
    "CURRENT_SCHEMA_VERSION",
    "build_event",
    "RunPhase",
    "RunRecord",
    "RunStatus",
    "TaskRecord",
    "project_run",
    "EventPage",
    "RunStore",
    "EventBus",
    "EventNotice",
    "EventSubscription",
    "RunRuntime",
    "DurableEventTail",
    "RecoveryReport",
    "recover_running_runs",
]
