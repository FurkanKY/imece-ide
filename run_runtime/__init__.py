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
    RunCompletionError,
    RunNotFoundError,
    RunProjectionError,
    RunRuntimeError,
    RunStoreError,
    TaskNotFoundError,
)
from run_runtime.events import CURRENT_SCHEMA_VERSION, RunEvent, RunEventSpec, RunEventType, build_event
from run_runtime.models import RunPhase, RunRecord, RunStatus, TaskRecord
from run_runtime.native_agent import CanonicalAgentEventSink
from run_runtime.completion import RunCompletionGate
from run_runtime.verification import CanonicalVerificationEventSink
from run_runtime.reviewer import CanonicalReviewEventSink
from run_runtime.planner import CanonicalPlannerEventSink
from run_runtime.fix_loop import CanonicalFixLoopRecorder
from run_runtime.acp import CanonicalAcpEventSink
from run_runtime.projector import project_run
from run_runtime.readmodels import (
    HISTORY_MAX_ITEMS,
    RunReadService,
    RunReadSnapshot,
    build_history_item,
    build_receipt,
    canonical_status_string,
    load_full_event_history,
    merge_canonical_and_legacy_history,
    render_receipt_markdown,
)
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
    "RunCompletionError",
    "RunStoreError",
    "RunProjectionError",
    "RunEvent",
    "RunEventSpec",
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
    "CanonicalAgentEventSink",
    "RunCompletionGate",
    "CanonicalVerificationEventSink",
    "CanonicalReviewEventSink",
    "CanonicalPlannerEventSink",
    "CanonicalFixLoopRecorder",
    "CanonicalAcpEventSink",
    "DurableEventTail",
    "RecoveryReport",
    "recover_running_runs",
    "HISTORY_MAX_ITEMS",
    "RunReadService",
    "RunReadSnapshot",
    "build_history_item",
    "build_receipt",
    "canonical_status_string",
    "load_full_event_history",
    "merge_canonical_and_legacy_history",
    "render_receipt_markdown",
]
