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
from run_runtime.events import RunEventType
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
            runtime.record(
                run_id=candidate.run_id,
                type=RunEventType.RUN_INTERRUPTED,
                payload={"reason": RECOVERY_INTERRUPT_REASON},
                source=RECOVERY_SOURCE,
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
