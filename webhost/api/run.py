"""run.* — multi-agent koşu köprüsü.

desktop.py Worker(QThread) deseninin portu: `project_runner.run_project_task`
generator'ının olayları DEĞİŞMEDEN `run.event` kanalına forward edilir
(stage/info/output/metric/diff/verdict/proposal). Proposal içerikleri sunucu
tarafında tutulur → applyProposals yalnız yol listesi alır (içerik köprüden
geri taşınmaz).

2F (kanonik run runtime entegrasyonu): project_runner hâlâ DEĞİŞMEDEN, ham
legacy dict event'ler üretiyor. Her legacy event, arayüze iletilmeden ÖNCE
LegacyRunCoordinator aracılığıyla SQLite'a (run_events) DAYANIKLI biçimde
kaydedilir (bkz. run_runtime.legacy). Kanonik kalıcılık başarısız olursa
orijinal legacy event ASLA `run.event` kanalına iletilmez ve koşu, host
tarafında "failed" olarak sonlandırılır. run.start/run.event/run.finished'ın
tel (wire) sözleşmesi TAMAMEN AYNI kalır — mevcut React arayüzü bu geçişten
habersizdir.

2F sertleştirme geçişi: run.finished artık YALNIZCA kanonik yerleşim (terminal
settlement) BAŞARILI olduğunda "done"/"cancelled" bildirir — coordinator.
finish_normal()/finish_cancelled() başarısız olursa "failed" olarak raporlanır
(bkz. on_worker_finished/on_worker_cancelled). Böylece UI, SQLite hâlâ RUNNING
derken asla "başarılı" bir bildirim almaz.

2G (kanonik read model göçü): Bu modül ARTIK ReceiptStore/HistoryStore'a
YAZMAZ — makbuz (receipt) ve geçmiş (history), run_runtime.readmodels
aracılığıyla kanonik run_events'ten TALEP ÜZERİNE (on-demand) yeniden inşa
edilir (bkz. webhost/api/history.py). on_event'in tek işi artık: kanonik
kalıcılık (canonical-before-UI sıralaması KORUNARAK), bellek-içi proposal
durumunu güncellemek ve orijinal legacy event'i arayüze iletmektir —
plan/diff/metric/verdict toplama ARTIK BURADA YAPILMAZ. Legacy .imece/
history.json ve .imece/receipts/*.json dosyaları SALT-OKUNUR kanonik-öncesi
uyumluluk verisi olarak kalır (silinmez).

BİLİNEN GEÇİCİ SINIRLAMALAR: tek seferde yalnızca bir aktif legacy worker;
süreç sahipliği/kira/heartbeat YOK; başlangıçta otomatik kurtarma YOK
(recover_running_runs açık bir primitif olarak kalır); Apply/Reject bekleyen
bir proposal, süreç yeniden başlatıldıktan sonra bellek-içi `_active`
durumundan yeniden kurulamaz (yalnızca SQLite'taki kanonik WAITING_USER
durumu kalıcıdır); checkpoint restore bu dilimde kanonikleştirilmedi (bkz.
webhost/api/checkpoint.py, DEĞİŞTİRİLMEDİ).
"""

from PySide6.QtCore import QThread, Signal

from checkpoints import CheckpointStore
from project import Project
from project_runner import run_project_task
import providers as provider_registry
from agents import DEFAULT_ROUTING
from run_runtime.legacy import LegacyRunCoordinator
from run_runtime.models import RunStatus
from webhost import state
from webhost.bridge import handler, BridgeError

_active: dict = {
    "worker": None, "coordinator": None, "run_id": None, "proposals": [],
}


class _Worker(QThread):
    event = Signal(dict)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, root, task, routing):
        super().__init__()
        self.root, self.task, self.routing = root, task, routing
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        gen = run_project_task(self.root, self.task, self.routing)
        try:
            for ev in gen:
                if self._cancel:
                    gen.close()
                    self.cancelled.emit()
                    return
                self.event.emit(ev)
        except Exception as e:  # motor hatası UI'a düzgün gitsin
            self.failed.emit(str(e))


def _require_project() -> Project:
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    return proj


_TERMINAL_RUN_STATUSES = frozenset({
    RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED,
})


def _active_canonical_run_blocks_start() -> bool:
    """Yeni bir koşu başlatmadan önce, önceki koşunun kanonik olarak hâlâ
    TERMİNAL-OLMAYAN bir durumda olup olmadığını kontrol eder.

    Yalnızca WAITING_USER (bekleyen proposal) YETERSİZDİR: bir worker'ın
    terminal yerleşimi (settlement) başarısız olabilir — örn. finish_normal()
    başarısız olur, en iyi çaba finish_failed() de başarısız olur. UI doğru
    biçimde "failed" görür, ama SQLite'ta Run hâlâ RUNNING kalmış olabilir.
    QThread durduktan sonra yalnızca WAITING_USER kontrol eden eski bir
    koruma, RUNNING != WAITING_USER olduğu için yeni bir koşuya izin verip
    çözülmemiş bir kanonik Run'ı sessizce terk ederdi. Bu yüzden CREATED,
    RUNNING, WAITING_USER dahil TERMİNAL OLMAYAN her durum engelleyicidir;
    yalnızca SUCCEEDED/FAILED/CANCELLED/INTERRUPTED yeni bir koşuya izin verir.

    Bellek-içi `_active["proposals"]` listesi BOŞ olsa bile (örn. beklenmedik
    bir durum) bu kontrol bağımsız olarak uygulanır — yalnızca bellek-içi
    duruma GÜVENİLMEZ. Süreçler arası (process yeniden başlatma sonrası)
    yeniden inşa BU MİLESTONDA YOKTUR; bu yalnızca AYNI süreç içindeki bir
    tutarlılık kontrolüdür (kira/heartbeat/çapraz-süreç tarama EKLENMEDİ).

    KAPALI BAŞARISIZ (fail closed): bir coordinator VARSA ama kanonik durumu
    OKUNAMIYORSA, "bekleyen bir Run yok" diye SESSİZCE VARSAYILMAZ — önceki
    koşunun durumu doğrulanamadığından run.start tipli bir BridgeError ile
    reddedilir (bkz. çağrı yeri).
    """
    if _active.get("proposals"):
        return True
    coordinator = _active.get("coordinator")
    if coordinator is None:
        return False
    try:
        current = coordinator.get_run()
    except Exception as exc:
        raise BridgeError(
            "canonical_state_unavailable",
            f"Önceki koşunun kanonik durumu doğrulanamadı; yeni koşu güvenli "
            f"biçimde başlatılamaz: {exc}",
        )
    return current.status not in _TERMINAL_RUN_STATUSES


def _safe_restore_checkpoint(proj: Project, checkpoint_id: str) -> tuple[bool, Exception | None]:
    """Checkpoint'i geri yüklemeyi (restore) dener.

    (restored_ok, error) döner. restore BAŞARILI olursa checkpoint dosyası
    DÜŞÜRÜLÜR (drop); restore BAŞARISIZ olursa checkpoint KASITLI OLARAK
    düşürülmez (tanı/olası manuel kurtarma için saklanır).
    """
    store = CheckpointStore(proj.root)
    try:
        store.restore(proj, checkpoint_id)
    except Exception as e:
        return False, e
    try:
        store.drop(checkpoint_id)
    except Exception:
        pass  # düşürme en iyi çabadır; restore zaten başarılı oldu
    return True, None


@handler("run.providers")
def _providers(params, ctx):
    # Rol menüsü: kullanıma hazır sağlayıcılar + varsayılan atamalar (anahtar
    # eksikse bile listede kalır; Composer eksik-anahtar uyarısını gösterir).
    ready = [e["id"] for e in provider_registry.catalog() if provider_registry.is_ready(e)]
    ids = list(dict.fromkeys([*DEFAULT_ROUTING.values(), *ready]))
    return {"providers": ids, "defaultRouting": dict(DEFAULT_ROUTING)}


@handler("run.start")
def _start(params, ctx):
    proj = _require_project()
    task = (params.get("task") or "").strip()
    if not task:
        raise BridgeError("empty_task", "Görev boş.")
    if _active["worker"] is not None and _active["worker"].isRunning():
        raise BridgeError("busy", "Zaten bir koşu sürüyor.")
    if _active_canonical_run_blocks_start():
        # Kanonik Run hâlâ terminal-olmayan bir durumda (CREATED/RUNNING/
        # WAITING_USER) olabilir; yeni bir koşu başlatmak bu durumu sessizce
        # terk ederdi.
        raise BridgeError("pending_proposals", "Bekleyen öneriler var; önce uygula veya reddet.")

    routing = params.get("routing") or {}
    runtime = state.get_run_runtime()
    # run.created/run.started BURADA, QThread BAŞLAMADAN ÖNCE kalıcı olur.
    # Başarısız olursa (Task/Run kalıcılığı) BridgeError doğal olarak
    # yukarı taşınır — hiçbir yerel worker/host durumu KURULMAMIŞ olur.
    coordinator = LegacyRunCoordinator.start(
        runtime, project_root=proj.root, task=task, routing=routing,
    )
    run_id = coordinator.run_id

    _active["proposals"] = []
    _active["coordinator"] = coordinator
    _active["run_id"] = run_id

    try:
        bridge = ctx._bridge  # ana thread'e sinyalle taşınır (queued connection)
        # Worker'a TAM OLARAK kanonik Run'da saklanan routing verilir —
        # build_agents'ın örtük ikinci bir DEFAULT_ROUTING türetmesine
        # GÜVENİLMEZ; kalıcı routing ile fiilen kullanılan routing AYNI olur.
        worker = _Worker(proj.root, task, coordinator.routing)

        ended = {"flag": False}  # failed/cancelled sonrası ikinci "done" yayınlanmasın

        def settle_canonical_failure(message: str) -> None:
            # En iyi çaba: kanonik yerleşimin KENDİSİ de başarısız olabilir; yine
            # de arayüze bir run.finished bildirimi GÖNDERİLECEK (aşağıda), ama
            # kalıcılığın başarılı olduğu ASLA iddia edilmez.
            try:
                coordinator.finish_failed(message)
            except Exception:
                pass

        def on_event(ev: dict):
            try:
                coordinator.handle_legacy_event(ev)
            except Exception as exc:
                if ended["flag"]:
                    return
                worker.cancel()
                message = f"Kanonik olay kalıcılığı başarısız: {exc}"
                settle_canonical_failure(message)
                finish("failed", message)
                return

            # BURAYA yalnızca kanonik kalıcılık BAŞARILI olduysa ulaşılır.
            # Makbuz/geçmiş toplama ARTIK BURADA YAPILMAZ — bkz.
            # run_runtime.readmodels (talep üzerine yeniden inşa edilir).
            if ev.get("type") == "proposal":
                _active["proposals"] = ev.get("proposals", [])
            bridge.emit_event("run.event", {"runId": run_id, "ev": ev})

        def finish(status: str, error: str | None = None):
            if ended["flag"]:
                return
            ended["flag"] = True
            # ReceiptStore/HistoryStore YAZIMI YOK: kanonik Run zaten SQLite'a
            # COMMIT olmuştur (ya da hiç olmamıştır — o karar bu satırdan ÖNCE,
            # aşağıdaki status parametresiyle belirlenmiştir). run.finished
            # yalnızca mevcut host/UI yaşam döngüsünü sonlandırıp bildirir.
            payload = {"runId": run_id, "status": status}
            if error:
                payload["error"] = error
            bridge.emit_event("run.finished", payload)

        def on_worker_failed(msg: str):
            if ended["flag"]:
                return
            # FAILED sinyali: kanonik yerleşim en iyi çabadır; UI HER ZAMAN
            # "failed" görür (zaten olumsuz bir sonuç raporlanıyor).
            settle_canonical_failure(msg)
            finish("failed", msg)

        def on_worker_cancelled():
            if ended["flag"]:
                return
            try:
                coordinator.finish_cancelled()
            except Exception as exc:
                # Kanonik iptal yerleşimi BAŞARISIZ oldu: UI'a "cancelled"
                # DEĞİL, "failed" bildirilir — SQLite hâlâ RUNNING derken
                # asla bir başarı/iptal iddia edilmez. En iyi çaba olarak
                # run.failed ile durum kapatılmaya çalışılır.
                message = f"Kanonik iptal kalıcılığı başarısız: {exc}"
                settle_canonical_failure(message)
                finish("failed", message)
                return
            finish("cancelled")

        def on_worker_finished():
            if ended["flag"]:
                return
            try:
                coordinator.finish_normal()
            except Exception as exc:
                # Kanonik normal sonlanma BAŞARISIZ oldu: UI'a "done" DEĞİL,
                # "failed" bildirilir.
                message = f"Kanonik normal sonlanma başarısız: {exc}"
                settle_canonical_failure(message)
                finish("failed", message)
                return
            finish("done")

        worker.event.connect(on_event)
        worker.failed.connect(on_worker_failed)
        worker.cancelled.connect(on_worker_cancelled)
        worker.finished.connect(on_worker_finished)

        worker.start()
    except Exception as exc:
        # Kanonik run.created/run.started ZATEN commit oldu ama yerel QThread
        # kurulumu/başlatılması başarısız oldu: Run'ı kalıcı olarak RUNNING
        # bırakmak yerine en iyi çaba bir run.failed yerleştirmesi deneriz,
        # host durumunu SIFIRLARIZ ve BridgeError fırlatırız. Hiçbir zaman
        # hiç başlamamış bir worker için run.finished YAYINLANMAZ — bu istek
        # zaten BridgeError ile başarısız olacaktır.
        try:
            coordinator.finish_failed(f"Yerel worker başlatılamadı: {exc}")
        except Exception:
            pass
        _active["worker"] = None
        _active["coordinator"] = None
        _active["run_id"] = None
        _active["proposals"] = []
        raise BridgeError("worker_start_failed", f"Koşu başlatılamadı: {exc}")

    _active["worker"] = worker
    return {"runId": run_id}


@handler("run.cancel")
def _cancel(params, ctx):
    w = _active.get("worker")
    if w is not None and w.isRunning():
        w.cancel()
    return {}


@handler("run.applyProposals")
def _apply(params, ctx):
    proj = _require_project()
    wanted = set(params.get("paths") or [])
    proposals = [p for p in _active.get("proposals", []) if p.get("path") in wanted]
    if not proposals:
        return {"applied": [], "errors": [], "checkpointId": None}

    coordinator = _active.get("coordinator")
    if coordinator is None:
        # Bekleyen öneriler var ama kanonik koordinatör yok: KAPALI BAŞARISIZ
        # olunur — dosya sistemine DOKUNULMAZ, öneriler TEMİZLENMEZ.
        raise BridgeError("no_active_run", "Aktif bir kanonik koşu yok; öneri uygulanamaz.")

    try:
        checkpoint = CheckpointStore(proj.root).create(
            proj, [p["path"] for p in proposals], _active.get("run_id"),
        )
    except Exception as e:
        raise BridgeError("checkpoint", f"Checkpoint oluşturulamadı: {e}")

    applied, errors = [], []
    for p in proposals:
        try:
            proj.apply(p["path"], p.get("new", ""), backup=False)
            applied.append(p["path"])
        except Exception as e:
            errors.append({"path": p.get("path", ""), "message": str(e)})

    if errors:
        # Kısmi apply'da checkpoint geri yüklenir; kullanıcı hiçbir yarım
        # değişiklik görmez. proposal.applied HİÇ eklenmez; kanonik Run
        # WAITING_USER kalır.
        restored_ok, restore_err = _safe_restore_checkpoint(proj, checkpoint["id"])
        if not restored_ok:
            # Geri alma da başarısız: dosya sistemi durumu BİLİNMİYOR/TUTARSIZ
            # olabilir. Checkpoint'i KASITLI OLARAK düşürmüyoruz (tanı için).
            ctx._bridge.emit_event(
                "fs.changed", {"kind": "modified", "paths": [p["path"] for p in proposals]},
            )
            raise BridgeError(
                "apply_rollback_failed",
                f"Kısmi apply başarısız oldu VE geri alma (rollback) da başarısız oldu "
                f"(checkpoint={checkpoint['id']}); dosya sistemi durumu tutarsız olabilir: {restore_err}",
            )
        return {"applied": [], "errors": errors, "checkpointId": None}

    # Dosya yazımları TAMAMEN başarılı. Şimdi kanonik yerleşimi (settlement)
    # dene — bu, dosya değişikliklerinin "gerçek" sayılıp sayılmayacağına
    # karar veren ADIMDIR.
    try:
        coordinator.record_proposal_applied(applied_paths=applied, checkpoint_id=checkpoint["id"])
    except Exception as e:
        # KRİTİK: dosya sistemi zaten değişti ama kanonik geçmiş bunu
        # yansıtamadı. Dosya sistemini GERİ ALMAYI DENE; fs.changed
        # YAYINLAMA (restore başarılıysa); aktif önerileri TEMİZLEME;
        # başarılı bir apply RAPORLAMA.
        restored_ok, restore_err = _safe_restore_checkpoint(proj, checkpoint["id"])
        if not restored_ok:
            ctx._bridge.emit_event("fs.changed", {"kind": "modified", "paths": applied})
            raise BridgeError(
                "canonical_apply_rollback_failed",
                f"Kanonik onay kalıcılığı başarısız oldu VE dosya sistemi geri alma "
                f"(rollback) da başarısız oldu (checkpoint={checkpoint['id']}); dosya "
                f"sistemi durumu tutarsız olabilir: {restore_err} (orijinal hata: {e})",
            )
        raise BridgeError(
            "canonical_apply_failed",
            f"Dosyalar geri alındı: kanonik onay kalıcılığı başarısız oldu: {e}",
        )

    # Kanonik onay BAŞARILI: checkpoint Undo için SAKLANIR, mantıksal öneri
    # kararı durumu (bu milestonda kısmi çoklu-karar desteklenmediğinden)
    # TAMAMEN temizlenir. ReceiptStore YAZIMI YOK — receipt.get artık bu
    # Run'ı kanonik run_events'ten (proposal.applied) doğrudan okur.
    _active["proposals"] = []
    ctx._bridge.emit_event("fs.changed", {"kind": "modified", "paths": applied})
    return {
        "applied": applied,
        "errors": [],
        "checkpointId": checkpoint["id"],
    }


@handler("run.rejectProposals")
def _reject(params, ctx):
    _require_project()
    active_proposals = _active.get("proposals") or []
    if not active_proposals:
        return {}

    coordinator = _active.get("coordinator")
    if coordinator is None:
        # Bekleyen öneriler var ama kanonik koordinatör yok: KAPALI BAŞARISIZ
        # olunur — öneriler TEMİZLENMEZ.
        raise BridgeError("no_active_run", "Aktif bir kanonik koşu yok; öneri reddedilemez.")

    rejected_paths = [p.get("path", "") for p in active_proposals]
    try:
        coordinator.record_proposal_rejected(rejected_paths=rejected_paths)
    except Exception as e:
        raise BridgeError("canonical_reject_failed", f"Kanonik ret kalıcılığı başarısız oldu: {e}")

    # ReceiptStore YAZIMI YOK — receipt.get artık bu Run'ı kanonik
    # run_events'ten (proposal.rejected) doğrudan okur.
    _active["proposals"] = []
    return {}


def shutdown():
    """Uygulama kapanırken koşuyu iptal et (zombi thread önleme)."""
    w = _active.get("worker")
    if w is not None and w.isRunning():
        w.cancel()
        w.wait(2000)
