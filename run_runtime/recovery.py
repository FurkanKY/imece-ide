"""run_runtime.recovery — MUHAFAZAKÂR (conservative) çökme/kesinti kurtarma.

Bu milestondaki kurtarma yalnızca DURUMU DÜRÜST biçimde SETTLE eder: RUNNING
statüsünde takılı kalmış Run'lar için tek bir `run.interrupted` event'i
eklenir. Hiçbir şey YENİDEN ÇALIŞTIRILMAZ, YENİDEN DENENMEZ, otomatik olarak
SÜRDÜRÜLMEZ ve yeni bir Run OLUŞTURULMAZ (bkz. modül sonu "BİLİNEN
SINIRLAMALAR"). WAITING_USER Run'lara HİÇ dokunulmaz.

Yalnızca RunStatus.RUNNING adayları ele alınır. CREATED, WAITING_USER,
SUCCEEDED, FAILED, CANCELLED, INTERRUPTED durumundaki Run'lara ASLA dokunulmaz.

expected_last_event_seq=candidate.last_event_seq kullanılır: tarama sırasında
görülen anlık görüntü (snapshot) o andan beri değişmişse (örn. hâlâ aktif bir
üretici tarafından ilerletilmişse), RunStore.append_event EventSequenceError
fırlatır ve bu Run MUHAFAZAKÂR biçimde ATLANIR — kör bir yeniden deneme
YAPILMAZ; değişen Run aktif bir üretici tarafından sahiplenilmiş olabilir.

Tüm kanonik event yazımları RunStore.append_event'i DOĞRUDAN DEĞİL,
RunRuntime.record() üzerinden yapılır — bu, tek kanonik yazma noktasıdır
(bkz. run_runtime.service). Bu sayede kurtarma tarafından eklenen
`run.interrupted` event'i de, tıpkı diğer tüm event'ler gibi, canlı
DurableEventTail abonelerine COMMIT'ten SONRA bildirilir.

------------------------------------------------------------------------------
ÇOK-SÜREÇLİ / DAĞITIK SINIRLAMA (bu milestonda KASITLI OLARAK yok):
------------------------------------------------------------------------------
    - süreç sahipliği (process ownership)
    - kira (lease)
    - heartbeat
    - bayat-sahip dışlama (stale-owner fencing)
    - dağıtık yürütme sahipliği (distributed execution ownership)

expected_last_event_seq yalnızca YEREL, tek okumalık bir anlık görüntüyü
(snapshot) bayatlığa karşı korur — dağıtık bir kira (distributed lease)
DEĞİLDİR ve öyleymiş gibi davranılmamalıdır.
------------------------------------------------------------------------------

Bu fonksiyon uygulama başlangıcına (shell.py) veya mevcut project_runner
akışına BAĞLANMAZ; bu, sonraki bir "legacy entegrasyon" diliminin işidir.
"""

from __future__ import annotations

from dataclasses import dataclass

from run_runtime.errors import EventSequenceError
from run_runtime.events import RunEventSpec, RunEventType
from run_runtime.models import RunStatus
from run_runtime.service import RunRuntime

RECOVERY_SOURCE = "recovery"
RECOVERY_INTERRUPT_REASON = "process_restart"


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    interrupted_run_ids: tuple[str, ...]
    skipped_changed_run_ids: tuple[str, ...]


def recover_running_runs(runtime: RunRuntime) -> RecoveryReport:
    """RunStatus.RUNNING durumundaki her Run'ı MUHAFAZAKÂR biçimde tarar.

    İlk tarama runtime.store.active_runs() ile yapılır, ancak TÜM kanonik
    event yazımları runtime.record() üzerinden gider (store.append_event'i
    DOĞRUDAN ÇAĞIRMAZ) — böylece kurtarma event'i de diğer tüm event'ler
    gibi COMMIT'ten SONRA canlı DurableEventTail abonelerine bildirilir.

    Her aday için, tarama anındaki last_event_seq'i expected_last_event_seq
    olarak kullanarak tek bir `run.interrupted` event'i eklemeyi dener. Aday
    o andan beri değişmişse (EventSequenceError), Run'ı yeniden okuyup
    ATLAR — kör bir yeniden deneme YAPMAZ.

    Beklenmeyen RunStore hataları (örn. bağlantı/şema sorunları) YUTULMAZ;
    yalnızca beklenen bayat-sıra (stale-sequence) durumu muhafazakâr bir
    atlamaya çevrilir. İkinci bir tarama, ilk taramada kesintiye uğratılmış
    Run'ları bir daha ELE ALMAZ (artık RUNNING değildir) — bu doğal olarak
    idempotenttir.
    """
    interrupted: list[str] = []
    skipped: list[str] = []

    candidates = [run for run in runtime.store.active_runs() if run.status == RunStatus.RUNNING]

    for candidate in candidates:
        try:
            started_items = {}
            terminal_items = set()
            history = []
            after_seq = 0
            while True:
                page = runtime.events(candidate.run_id, after_seq=after_seq, limit=200)
                for event in page.events:
                    after_seq = event.seq
                    history.append(event)
                    if event.type == RunEventType.TOOL_STARTED and event.item_id:
                        started_items[event.item_id] = event
                    elif event.type in {
                        RunEventType.TOOL_COMPLETED,
                        RunEventType.TOOL_FAILED,
                        RunEventType.TOOL_INTERRUPTED,
                    } and event.item_id:
                        terminal_items.add(event.item_id)
                if not page.has_more:
                    break
            specs = []
            for started in started_items.values():
                if started.item_id in terminal_items:
                    continue
                specs.append(
                    RunEventSpec(
                        type=RunEventType.TOOL_INTERRUPTED,
                        payload={
                            "reason": RECOVERY_INTERRUPT_REASON,
                            "outcome_unknown": True,
                            "call_id": started.payload.get("call_id"),
                            "tool_name": started.payload.get("tool_name"),
                        },
                        execution_id=started.execution_id,
                        turn_id=started.turn_id,
                        item_id=started.item_id,
                        correlation_id=started.correlation_id,
                        source=RECOVERY_SOURCE,
                    )
                )
            verification_starts = [
                event for event in history
                if event.type == RunEventType.VERIFICATION_STARTED
                and isinstance(event.payload.get("verification_id"), str)
            ]
            for index, started in enumerate(verification_starts):
                verification_id = started.payload["verification_id"]
                next_start_seq = None
                for later in verification_starts[index + 1:]:
                    if later.payload["verification_id"] == verification_id:
                        next_start_seq = later.seq
                        break

                def in_attempt(event, *, after: int) -> bool:
                    return (
                        event.seq > after
                        and (next_start_seq is None or event.seq < next_start_seq)
                        and event.payload.get("verification_id") == verification_id
                    )

                check_starts = [
                    event for event in history
                    if event.type == RunEventType.VERIFICATION_CHECK_STARTED
                    and event.item_id
                    and in_attempt(event, after=started.seq)
                ]
                for check_started in check_starts:
                    has_terminal = any(
                        event.item_id == check_started.item_id
                        and event.type in {
                            RunEventType.VERIFICATION_CHECK_COMPLETED,
                            RunEventType.VERIFICATION_CHECK_FAILED,
                            RunEventType.VERIFICATION_CHECK_INTERRUPTED,
                        }
                        and in_attempt(event, after=check_started.seq)
                        for event in history
                    )
                    if has_terminal:
                        continue
                    specs.append(
                        RunEventSpec(
                            type=RunEventType.VERIFICATION_CHECK_INTERRUPTED,
                            payload={
                                "verification_id": verification_id,
                                "check_id": check_started.payload.get("check_id"),
                                "reason": RECOVERY_INTERRUPT_REASON,
                                "outcome_unknown": True,
                            },
                            turn_id=check_started.turn_id,
                            item_id=check_started.item_id,
                            correlation_id=check_started.correlation_id,
                            source=RECOVERY_SOURCE,
                        )
                    )

                has_verification_terminal = any(
                    event.type in {
                        RunEventType.VERIFICATION_COMPLETED,
                        RunEventType.VERIFICATION_INTERRUPTED,
                    }
                    and in_attempt(event, after=started.seq)
                    for event in history
                )
                if not has_verification_terminal:
                    specs.append(
                        RunEventSpec(
                            type=RunEventType.VERIFICATION_INTERRUPTED,
                            payload={
                                "verification_id": verification_id,
                                "reason": RECOVERY_INTERRUPT_REASON,
                            },
                            correlation_id=started.correlation_id or verification_id,
                            source=RECOVERY_SOURCE,
                        )
                    )
            review_starts = [
                event for event in history
                if event.type == RunEventType.REVIEW_STARTED
                and isinstance(event.payload.get("review_id"), str)
            ]
            for index, started in enumerate(review_starts):
                review_id = started.payload["review_id"]
                next_start_seq = None
                for later in review_starts[index + 1:]:
                    if later.payload["review_id"] == review_id:
                        next_start_seq = later.seq
                        break
                has_review_terminal = any(
                    event.seq > started.seq
                    and (next_start_seq is None or event.seq < next_start_seq)
                    and event.payload.get("review_id") == review_id
                    and event.type in {
                        RunEventType.REVIEW_COMPLETED,
                        RunEventType.REVIEW_FAILED,
                        RunEventType.REVIEW_INTERRUPTED,
                    }
                    for event in history
                )
                if not has_review_terminal:
                    specs.append(
                        RunEventSpec(
                            type=RunEventType.REVIEW_INTERRUPTED,
                            payload={
                                "review_id": review_id,
                                "reason": RECOVERY_INTERRUPT_REASON,
                            },
                            correlation_id=started.correlation_id or review_id,
                            source=RECOVERY_SOURCE,
                        )
                    )
            if not specs:
                # Preserve the established single-event recovery seam when no
                # unfinished tool exists; this also keeps existing store-level
                # conflict/error behavior unchanged.
                runtime.record(
                    run_id=candidate.run_id,
                    type=RunEventType.RUN_INTERRUPTED,
                    payload={"reason": RECOVERY_INTERRUPT_REASON},
                    source=RECOVERY_SOURCE,
                    expected_last_event_seq=candidate.last_event_seq,
                )
            else:
                specs.append(
                    RunEventSpec(
                        type=RunEventType.RUN_INTERRUPTED,
                        payload={"reason": RECOVERY_INTERRUPT_REASON},
                        source=RECOVERY_SOURCE,
                    )
                )
                runtime.record_many(
                    run_id=candidate.run_id,
                    specs=tuple(specs),
                    expected_last_event_seq=candidate.last_event_seq,
                )
        except EventSequenceError:
            # Aday, tarama anından beri değişti (hâlâ aktif bir üretici
            # tarafından ilerletilmiş olabilir). Neden değiştiğini doğrulamak
            # için Run yeniden okunur, ancak kör bir yeniden deneme YAPILMAZ —
            # yalnızca muhafazakâr biçimde atlanır.
            runtime.get_run(candidate.run_id)
            skipped.append(candidate.run_id)
            continue
        interrupted.append(candidate.run_id)

    return RecoveryReport(
        interrupted_run_ids=tuple(interrupted),
        skipped_changed_run_ids=tuple(skipped),
    )
