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

import os
import threading
import time

from PySide6.QtCore import QThread, Signal

import ui_prefs
import engine_factory
from checkpoints import CheckpointStore
from project import IGNORE_EXT, Project
from project_runner import run_project_task
import providers as provider_registry
from agents import DEFAULT_ROUTING
from run_runtime.events import RunEventType
from run_runtime.legacy import LegacyRunCoordinator
from run_runtime.models import RunStatus
from webhost import state
from webhost.api.activity import ActivityStreamer
from webhost.bridge import handler, BridgeError

try:
    from pipeline_runtime import PipelineRunner, PipelineStatus
except Exception:  # pragma: no cover - pipeline_runtime her zaman mevcut olmalı
    PipelineRunner = None
    PipelineStatus = None

_active: dict = {
    "worker": None, "coordinator": None, "run_id": None, "proposals": [],
    # T1.2 — yeni (pipeline) motor durumu:
    "engine": "legacy", "workspace": None, "cancel_event": None,
    # F1 (live agent activity) — yalnızca pipeline motorunda kullanılır;
    # legacy koşularda hep None kalır (bkz. _stop_activity_streamer).
    "activity_streamer": None,
    # F2 (takip isteği / follow-up): run.start sırasında sabitlenen, sonraki
    # run.followUp çağrılarının PipelineRunner.continue_with_feedback'e
    # AYNEN geçirdiği bağlam. "plan_text" yalnızca gerçek bir Planner
    # denemesi ürettiğinde (ilk koşu) güncellenir -- bir takip isteği
    # continuation'ı KENDİ planını üretmez, bu yüzden önceki plan metni
    # sonraki takip isteklerinde de KORUNUR (bkz. on_pipeline_finished).
    "pipeline_ports": None, "task": None, "plan_text": None, "pinned_paths": [],
}

# F6 (@-mentions): backend cap independent of (and enforced regardless of)
# the Composer's own UI-level cap — a client is never trusted blindly.
_MAX_MENTIONS = 10

# kanonik pipeline rolü -> legacy routing anahtarı (bkz. agents.DEFAULT_ROUTING).
_ROLE_ROUTING_KEY = {"planner": "planner", "worker": "coder", "reviewer": "reviewer"}
_ROLE_LEGACY_STAGE = {"planner": "plan", "worker": "code", "reviewer": "review"}


class _Worker(QThread):
    event = Signal(dict)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, root, task, routing, mentions=None):
        super().__init__()
        self.root, self.task, self.routing = root, task, routing
        self.mentions = mentions or []
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        gen = run_project_task(self.root, self.task, self.routing, mentions=self.mentions)
        try:
            for ev in gen:
                if self._cancel:
                    gen.close()
                    self.cancelled.emit()
                    return
                self.event.emit(ev)
        except Exception as e:  # motor hatası UI'a düzgün gitsin
            self.failed.emit(str(e))


class _PipelineWorker(QThread):
    """Yeni (pipeline_runtime tabanlı) motoru arka planda çalıştırır.

    _Worker'ın (legacy) QThread desenini birebir izler: PipelineRunner.run()
    BU thread'de senkron çalışır; on_stage/olay iletimi Signal'lerle ana
    thread'e (queued connection ile) taşınır."""

    stage = Signal(str, dict)
    finished_ok = Signal(object)  # PipelineReport
    failed = Signal(str)

    def __init__(self, runtime, run_id, workspace, ports, task, cancel_event, pinned_paths=None):
        super().__init__()
        self.runtime = runtime
        self.run_id = run_id
        self.workspace = workspace
        self.ports = ports
        self.task = task
        self.cancel_event = cancel_event
        self.pinned_paths = pinned_paths or []

    def run(self):
        try:
            pipeline = PipelineRunner(
                self.runtime,
                planner=self.ports.planner, worker=self.ports.worker,
                verification=self.ports.verification, reviewer=self.ports.reviewer,
                change_provider=self.ports.change_provider,
            )
            report = pipeline.run(
                self.run_id, self.workspace, self.task,
                cancel_event=self.cancel_event,
                on_stage=lambda s, info: self.stage.emit(s, dict(info)),
                pinned_paths=self.pinned_paths,
            )
            self.finished_ok.emit(report)
        except Exception as e:  # motor hatası UI'a düzgün gitsin
            self.failed.emit(str(e))


class _FollowUpWorker(QThread):
    """F2 (takip isteği): _PipelineWorker'ın AYNI QThread desenini izler, ama
    PipelineRunner.run() yerine continue_with_feedback() çağırır -- AYNI
    worktree'den, YENİ bir Planner denemesi OLMADAN devam eder."""

    stage = Signal(str, dict)
    finished_ok = Signal(object)  # PipelineReport
    failed = Signal(str)

    def __init__(
        self, runtime, run_id, workspace, ports, task, feedback, plan_text, cancel_event,
        pinned_paths=None, max_fix_attempts=None,
    ):
        super().__init__()
        self.runtime = runtime
        self.run_id = run_id
        self.workspace = workspace
        self.ports = ports
        self.task = task
        self.feedback = feedback
        self.plan_text = plan_text
        self.cancel_event = cancel_event
        self.pinned_paths = pinned_paths or []
        self.max_fix_attempts = max_fix_attempts

    def run(self):
        try:
            pipeline = PipelineRunner(
                self.runtime,
                planner=self.ports.planner, worker=self.ports.worker,
                verification=self.ports.verification, reviewer=self.ports.reviewer,
                change_provider=self.ports.change_provider,
            )
            kwargs = {}
            if self.max_fix_attempts is not None:
                kwargs["max_fix_attempts"] = self.max_fix_attempts
            report = pipeline.continue_with_feedback(
                self.run_id, self.workspace, self.feedback,
                task=self.task, plan_report_or_text=self.plan_text, pinned_paths=self.pinned_paths,
                cancel_event=self.cancel_event,
                on_stage=lambda s, info: self.stage.emit(s, dict(info)),
                **kwargs,
            )
            self.finished_ok.emit(report)
        except Exception as e:  # motor hatası UI'a düzgün gitsin
            self.failed.emit(str(e))


def _effective_routing(params: dict) -> dict:
    return {**DEFAULT_ROUTING, **(params.get("routing") or {})}


def _validate_mentions(proj: Project, raw: list) -> tuple[list[str], list[str]]:
    """F6 (@-mentions): normalize + validate `run.start`'s optional `mentions`
    param against the project.

    Returns (valid, invalid): `valid` holds project-relative, forward-slash
    paths that exist inside the project (file or folder) and, for files,
    aren't an ignored binary extension (see project.IGNORE_EXT) -- a client
    is never trusted blindly, this re-validates independently of whatever
    the Composer already checked. `invalid` holds the ORIGINAL raw strings
    that failed, for the caller to surface as an info event ("Bahsedilen
    dosya bulunamadı: ..."). Capped at _MAX_MENTIONS regardless of how many
    were sent.
    """
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, str) or not item.strip():
            invalid.append(item if isinstance(item, str) else str(item))
            continue
        rel = item.strip().replace("\\", "/").strip("/")
        if not rel or rel in (".", ".."):
            invalid.append(item)
            continue
        try:
            full = proj._safe(rel)
        except ValueError:
            invalid.append(item)
            continue
        is_dir = os.path.isdir(full)
        is_file = os.path.isfile(full)
        if not is_dir and not is_file:
            invalid.append(item)
            continue
        if is_file and os.path.splitext(rel)[1].lower() in IGNORE_EXT:
            invalid.append(item)
            continue
        if rel in seen:
            continue
        seen.add(rel)
        valid.append(rel)
        if len(valid) >= _MAX_MENTIONS:
            break
    return valid, invalid


def _stop_activity_streamer() -> None:
    """F1: idempotent, best-effort stop+drain of the pipeline activity
    streamer (no-op for legacy runs, where this is always None)."""
    streamer = _active.get("activity_streamer")
    _active["activity_streamer"] = None
    if streamer is None:
        return
    try:
        streamer.request_stop()
        streamer.wait(2000)
    except Exception:
        pass


def _dispose_workspace() -> None:
    ws = _active.get("workspace")
    _active["workspace"] = None
    if ws is None:
        return
    try:
        ws.dispose()
    except Exception:
        pass  # en iyi çaba: bir sonraki başlangıçta prune_startup_workspaces devreye girer


def _read_worktree_text(workspace, rel: str) -> tuple[bool, str | None]:
    """(okunabildi mi, metin) — ikili/decode edilemeyen dosyalar için (True, None)."""
    full = workspace.root / rel
    if not full.is_file():
        return False, None
    try:
        return True, full.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return True, None


def _build_pipeline_proposals(proj: Project, workspace, change_set) -> tuple[list[dict], list[str]]:
    """change_set.changed_paths -> mevcut UI proposal şekli (path/new/diff/is_new).

    Silme (deletion) desteklenir: CheckpointStore.restore zaten silme öncesi
    içeriği geri yazabiliyor (bkz. checkpoints.py), bu yüzden proj.delete()
    ile uygulanabilir hale getirilir ("is_deleted": True). İkili/decode
    edilemeyen dosyalar bir bilgi notuyla ATLANIR (önizleme desteklenmiyor).
    """
    proposals: list[dict] = []
    skipped_binary: list[str] = []
    for rel in change_set.changed_paths:
        found, text = _read_worktree_text(workspace, rel)
        if found:
            if text is None:
                skipped_binary.append(rel)
                continue
            is_new = not proj.exists(rel)
            diff = proj.make_diff(rel, text)
            # Taban durum: kullanıcının PROJE dosyasının (worktree DEĞİL) BU
            # ANKİ hali — Apply anında yeniden hesaplanıp karşılaştırılır
            # ("stale apply" koruması, bkz. _apply).
            base_hash = proj.hash_file(rel)
            proposals.append({
                "path": rel, "new": text, "diff": diff, "is_new": is_new, "baseHash": base_hash,
            })
        else:
            # worktree'de yok: taban durumuna göre silinmiş.
            if proj.exists(rel):
                diff = proj.make_diff(rel, "")
                base_hash = proj.hash_file(rel)
                proposals.append({
                    "path": rel, "new": "", "diff": diff, "is_new": False, "is_deleted": True,
                    "baseHash": base_hash,
                })
            # proj'da da yoksa: çalışma sırasında oluşturulup silinmiş, gösterecek bir şey yok.
    return proposals, skipped_binary


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
    # Rol menüsü: TÜM katalog sağlayıcıları + durumları (anahtar/CLI eksikse
    # bile listede kalır — Composer "Hesap ile"/"API anahtarı ile" gruplarını
    # ve eksik-anahtar/CLI uyarısını buradan oluşturur) + kullanılabilirliğe
    # göre önerilen routing (bkz. providers.recommended_routing).
    items = [provider_registry.status_of(e) for e in provider_registry.catalog()]
    return {"providers": items, "recommendedRouting": provider_registry.recommended_routing()}


_MAX_WORKER_FINAL_MESSAGE_CHARS = 1_500


def _bounded_text(text: str, *, limit: int = _MAX_WORKER_FINAL_MESSAGE_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _last_worker_final_message(runtime, run_id: str) -> str | None:
    """The last Worker execution's own final text, if any exists in the
    canonical event log -- shown as an explanatory item alongside "Değişiklik
    önerisi çıkmadı." (or a failure/cancellation) so the user sees WHAT the
    agent actually said/attempted, not just that nothing changed.

    Native executions carry their final text directly on execution.completed
    (`final_text`, see run_runtime.native_agent.ExecutionCompleted). ACP
    executions never get a synthesized final_text -- their own
    execution.completed instead carries only transport/session facts, so this
    reconstructs the agent's last message by concatenating every
    `agent_message_chunk` session update recorded for that same execution_id
    (see run_runtime.acp.CanonicalAcpEventSink)."""
    try:
        events = []
        page = runtime.events(run_id, limit=500)
        events.extend(page.events)
        while page.has_more:
            page = runtime.events(run_id, after_seq=events[-1].seq, limit=500)
            events.extend(page.events)
    except Exception:
        return None

    last_completed = None
    for event in events:
        if event.type == RunEventType.EXECUTION_COMPLETED:
            last_completed = event
    if last_completed is None:
        return None

    final_text = last_completed.payload.get("final_text")
    if isinstance(final_text, str) and final_text.strip():
        return _bounded_text(final_text)

    if last_completed.payload.get("transport") != "acp":
        return None

    execution_id = last_completed.execution_id
    chunks: list[str] = []
    for event in events:
        if event.type != RunEventType.EXECUTION_OUTPUT or event.execution_id != execution_id:
            continue
        update = event.payload.get("update")
        if not isinstance(update, dict) or update.get("sessionUpdate") != "agent_message_chunk":
            continue
        content = update.get("content")
        text = content.get("text") if isinstance(content, dict) else None
        if isinstance(text, str):
            chunks.append(text)
    joined = "".join(chunks).strip()
    return _bounded_text(joined) if joined else None


def _emit_pipeline_report(emit_ui, proj: Project, workspace, report, *, runtime=None, run_id=None) -> None:
    """PipelineReport'u legacy UI olay sözlüğüne (plan/verdict/info/proposal) çevirir."""
    if report.plan_report is not None:
        pr = report.plan_report
        emit_ui({
            "type": "plan", "summary": pr.summary, "files": [],
            "assumptions": [], "risks": list(pr.risks or []),
        })
    if report.verification_report is not None:
        vr = report.verification_report
        emit_ui({"type": "info", "text": f"Doğrulama sonucu: {vr.status.value}"})
    if report.review_report is not None:
        rr = report.review_report
        emit_ui({"type": "verdict", "verdict": rr.verdict.value, "note": rr.summary})

    change_set = report.change_set
    if change_set is None or not change_set.changed_paths:
        emit_ui({"type": "info", "text": "Değişiklik önerisi çıkmadı."})
        if runtime is not None and run_id is not None:
            final_message = _last_worker_final_message(runtime, run_id)
            if final_message:
                emit_ui({"type": "summary", "text": final_message})
        return

    proposals, skipped_binary = _build_pipeline_proposals(proj, workspace, change_set)
    for rel in skipped_binary:
        emit_ui({
            "type": "info",
            "text": f"{rel}: ikili dosya, önizleme desteklenmiyor (öneriye eklenmedi).",
        })
    _active["proposals"] = proposals
    verdict = report.review_report.verdict.value if report.review_report is not None else None
    if proposals:
        emit_ui({
            "type": "proposal", "proposals": proposals,
            "totals": {"latency_s": 0, "tokens": 0, "cost_usd": 0}, "verdict": verdict,
        })
    else:
        emit_ui({"type": "info", "text": "Değişiklik önerisi çıkmadı."})
        if runtime is not None and run_id is not None:
            final_message = _last_worker_final_message(runtime, run_id)
            if final_message:
                emit_ui({"type": "summary", "text": final_message})


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

    routing = _effective_routing(params)
    mentions, invalid_mentions = _validate_mentions(proj, params.get("mentions") or [])
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
    _active["workspace"] = None
    _active["cancel_event"] = None
    _active["engine"] = "legacy"
    _active["activity_streamer"] = None
    # F2 (takip isteği): her yeni run.start bu bağlamı SIFIRLAR -- önceki
    # koşudan kalan bir plan/ports/pinned_paths bir sonraki run.followUp'a
    # ASLA sızmaz (bkz. run.followUp'ın kendi run_id/coordinator kontrolü de
    # buna ek bir bağımsız koruma sağlar).
    _active["pipeline_ports"] = None
    _active["task"] = task
    _active["plan_text"] = None
    _active["pinned_paths"] = list(mentions)

    bridge = ctx._bridge  # ana thread'e sinyalle taşınır (queued connection)
    ended = {"flag": False}  # failed/cancelled sonrası ikinci "done" yayınlanmasın

    def emit_ui(ev: dict) -> None:
        bridge.emit_event("run.event", {"runId": run_id, "ev": ev})

    def settle_canonical_failure(message: str) -> None:
        # En iyi çaba: kanonik yerleşimin KENDİSİ de başarısız olabilir; yine
        # de arayüze bir run.finished bildirimi GÖNDERİLECEK (aşağıda), ama
        # kalıcılığın başarılı olduğu ASLA iddia edilmez.
        try:
            coordinator.finish_failed(message)
        except Exception:
            pass

    def finish(status: str, error: str | None = None) -> None:
        if ended["flag"]:
            return
        ended["flag"] = True
        # ReceiptStore/HistoryStore YAZIMI YOK: kanonik Run zaten SQLite'a
        # COMMIT olmuştur (ya da hiç olmamıştır — o karar bu satırdan ÖNCE,
        # aşağıdaki status parametresiyle belirlenmiştir). run.finished
        # yalnızca mevcut host/UI yaşam döngüsünü sonlandırıp bildirir.
        _stop_activity_streamer()
        # F2: UI'ın takip isteği modunu yalnızca GERÇEKTEN pipeline motoruyla
        # yürütülmüş bir koşuda açabilmesi için (bkz. web/ui state/run.ts).
        payload = {"runId": run_id, "status": status, "engine": _active.get("engine")}
        if error:
            payload["error"] = error
        bridge.emit_event("run.finished", payload)

    # ---------------- motor seçimi (T1.2) ----------------
    ai_engine_pref = ui_prefs.load().get("ai_engine", "auto")
    selection = engine_factory.select_engine(proj.root, coordinator.routing, ai_engine_pref=ai_engine_pref)
    fallback_reason = selection.reason
    pipeline_ports = None
    workspace = None
    if selection.engine == "pipeline" and PipelineRunner is not None:
        try:
            workspace = engine_factory.create_pipeline_workspace(proj.root, run_id)
            pipeline_ports = engine_factory.build_pipeline_ports(runtime, run_id, coordinator.routing)
        except Exception as exc:
            if workspace is not None:
                try:
                    workspace.dispose()
                except Exception:
                    pass
                workspace = None
            pipeline_ports = None
            fallback_reason = f"Yeni motor kullanılamadı ({exc}); klasik motor kullanılıyor."
    _active["pipeline_ports"] = pipeline_ports
    # F2 (takip isteği) sırasında bir GERÇEK bug fark edildi ve BURADA
    # düzeltildi: `_active["workspace"]` başarılı bir pipeline koşusunda
    # daha önce HİÇBİR ZAMAN gerçek workspace nesnesine atanmıyordu --
    # yalnızca hata yollarında None'a ayarlanıyordu. Bu yüzden
    # _dispose_workspace() (run.applyProposals/run.rejectProposals/
    # on_pipeline_finished/on_pipeline_failed/shutdown'da çağrılan) HER ZAMAN
    # no-op'tu; worktree'ler yalnızca bir sonraki uygulama başlangıcında
    # prune_startup_workspaces() ile temizleniyordu (ARCHITECTURE.md'nin
    # "run sona erdiğinde worktree kaldırılır" iddiasıyla ÇELİŞEN bir
    # durum). F2'nin worktree yaşam döngüsü kararı (#1) tam olarak bu alanın
    # DOĞRU ayarlanmasına bağlı olduğundan burada düzeltiliyor.
    _active["workspace"] = workspace

    try:
        if pipeline_ports is not None:
            _active["engine"] = "pipeline"
            worker = _start_pipeline_run(
                runtime, coordinator, workspace, pipeline_ports, task,
                emit_ui=emit_ui, finish=finish, settle_canonical_failure=settle_canonical_failure,
                ended=ended, bridge=bridge, pinned_paths=mentions,
            )
        else:
            _active["engine"] = "legacy"
            worker = _start_legacy_run(
                proj, coordinator, task, emit_ui=emit_ui, finish=finish,
                settle_canonical_failure=settle_canonical_failure, ended=ended, mentions=mentions,
            )
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
        if workspace is not None:
            try:
                workspace.dispose()
            except Exception:
                pass
        _stop_activity_streamer()
        _active["worker"] = None
        _active["coordinator"] = None
        _active["run_id"] = None
        _active["proposals"] = []
        _active["workspace"] = None
        _active["cancel_event"] = None
        raise BridgeError("worker_start_failed", f"Koşu başlatılamadı: {exc}")

    _active["worker"] = worker
    # F6 (@-mentions): invalid mentions are surfaced as an info event AFTER
    # the worker actually started, exactly like fallback_reason below --
    # the run itself still proceeds with whatever valid mentions remain.
    for bad in invalid_mentions:
        emit_ui({"type": "info", "text": f"Bahsedilen dosya bulunamadı: {bad}"})
    if fallback_reason and ai_engine_pref == "auto":
        emit_ui({"type": "info", "text": fallback_reason})
    return {"runId": run_id}


def _start_legacy_run(proj, coordinator, task, *, emit_ui, finish, settle_canonical_failure, ended, mentions=None):
    # Worker'a TAM OLARAK kanonik Run'da saklanan routing verilir — build_agents'ın
    # örtük ikinci bir DEFAULT_ROUTING türetmesine GÜVENİLMEZ; kalıcı routing ile
    # fiilen kullanılan routing AYNI olur.
    worker = _Worker(proj.root, task, coordinator.routing, mentions=mentions)

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
        emit_ui(ev)

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
    return worker


def _start_pipeline_run(runtime, coordinator, workspace, ports, task, *, emit_ui, finish, settle_canonical_failure,
                         ended, bridge, pinned_paths=None):
    run_id = coordinator.run_id
    cancel_event = threading.Event()
    # F7: this MUST be the same Event instance run.cancel()/shutdown() act
    # on -- previously it was created here but never published to _active,
    # so run.cancel() found _active["cancel_event"] still None and silently
    # cancelled nothing. Stored here (before the QThread starts) so a
    # cancel requested the instant after run.start() returns is never lost.
    _active["cancel_event"] = cancel_event
    proj = _require_project()
    worker = _PipelineWorker(runtime, run_id, workspace, ports, task, cancel_event, pinned_paths=pinned_paths)
    _wire_pipeline_worker(
        worker, runtime=runtime, run_id=run_id, coordinator=coordinator, workspace=workspace, proj=proj,
        emit_ui=emit_ui, finish=finish, settle_canonical_failure=settle_canonical_failure,
        ended=ended, bridge=bridge, activity_after_seq=0,
    )
    return worker


def _wire_pipeline_worker(worker, *, runtime, run_id, coordinator, workspace, proj, emit_ui, finish,
                           settle_canonical_failure, ended, bridge, activity_after_seq=0):
    """Shared stage/finished/failed wiring + F1 activity streaming for BOTH
    _PipelineWorker (run.start) and _FollowUpWorker (run.followUp, F2) --
    they share the exact same stage/finished_ok/failed Signal shapes."""
    stage_state = {"role": None, "start_ts": None, "last_seq": 0}

    def flush_role_metrics():
        role = stage_state["role"]
        if role is None:
            return
        events = []
        page = runtime.events(run_id, after_seq=stage_state["last_seq"], limit=500)
        events.extend(page.events)
        while page.has_more:
            page = runtime.events(run_id, after_seq=events[-1].seq, limit=500)
            events.extend(page.events)
        if events:
            stage_state["last_seq"] = events[-1].seq
        total_tokens, cost_usd = 0, 0.0
        for e in events:
            if e.type == RunEventType.USAGE_RECORDED:
                total_tokens += int(e.payload.get("total_tokens") or 0)
                cost_usd += float(e.payload.get("cost_usd") or 0.0)
        started = stage_state["start_ts"]
        latency_s = max(0.0, time.monotonic() - started) if started else 0.0
        provider_id = coordinator.routing.get(_ROLE_ROUTING_KEY[role])
        emit_ui({
            "type": "metric", "stage": _ROLE_LEGACY_STAGE[role], "provider": provider_id,
            "model": provider_id, "latency_s": latency_s, "tokens": total_tokens, "cost_usd": cost_usd,
        })

    def enter_role_stage(role: str, legacy_stage: str):
        flush_role_metrics()
        stage_state["role"] = role
        stage_state["start_ts"] = time.monotonic()
        provider_id = coordinator.routing.get(_ROLE_ROUTING_KEY[role])
        emit_ui({"type": "stage", "stage": legacy_stage, "provider": provider_id})
        entry = provider_registry.get(provider_id) if provider_id else None
        if entry and entry.get("kind") == "cli":
            # ACP (hesap) yolunda ara adım/olay akmaz — bkz. run_runtime.acp.
            emit_ui({"type": "info", "text": f"{entry.get('label', provider_id)} hesap oturumu çalışıyor…"})

    def on_stage(stage: str, info: dict):
        if ended["flag"]:
            return
        if stage == "planning":
            enter_role_stage("planner", "plan")
        elif stage in ("working", "fixing"):
            enter_role_stage("worker", "code")
            if stage == "fixing":
                emit_ui({"type": "info", "text": "Otomatik düzeltme deneniyor…"})
        elif stage == "verifying":
            flush_role_metrics()
            stage_state["role"] = None
            emit_ui({"type": "info", "text": "Doğrulama çalıştırılıyor…"})
        elif stage == "reviewing":
            enter_role_stage("reviewer", "review")
            if info.get("advisory"):
                emit_ui({
                    "type": "info",
                    "text": "Bu değişiklik için belirlenimci bir doğrulama adımı bulunamadı; "
                            "inceleme yalnızca danışma niteliğindedir.",
                })
        elif stage == "done":
            flush_role_metrics()

    def on_pipeline_failed(msg: str):
        if ended["flag"]:
            return
        settle_canonical_failure(msg)
        _dispose_workspace()
        finish("failed", msg)

    def on_pipeline_finished(report):
        if ended["flag"]:
            return
        try:
            # Öneri içeriği ("new") worktree'den BURADA okunup proposal
            # sözlüğüne gömülür — Apply daha sonra yalnızca bu sözlükten
            # yazar, worktree'yi bir daha OKUMAZ.
            #
            # F2 (takip isteği): worktree ARTIK burada koşulsuz dispose
            # EDİLMEZ. Run kanonik olarak GERÇEKTEN WAITING_USER'da (bekleyen
            # bir proposal var) kalıyorsa, worktree KORUNUR -- bir sonraki
            # run.followUp AYNI worktree'den devam edebilsin diye. Run
            # terminal bir duruma ulaştıysa (NO_CHANGES/FAILED/EXHAUSTED/
            # CANCELLED/COMPLETED) worktree burada dispose edilir; Apply/
            # Reject/cancel/yeni run.start/kapanış diğer dispose noktalarıdır
            # (bkz. _apply/_reject/on_pipeline_failed/shutdown).
            _emit_pipeline_report(emit_ui, proj, workspace, report, runtime=runtime, run_id=run_id)
        except Exception as exc:
            settle_canonical_failure(f"Sonuç işlenemedi: {exc}")
            _dispose_workspace()
            finish("failed", str(exc))
            return

        # F2: a fresh Planner attempt's summary becomes the plan context for
        # any SUBSEQUENT follow-up. A follow-up continuation's own report
        # never carries a plan_report (see PipelineRunner.continue_with_
        # feedback), so this deliberately does NOT clear plan_text then --
        # the ORIGINAL plan stays available across multiple follow-ups.
        if report.plan_report is not None:
            _active["plan_text"] = report.plan_report.summary

        try:
            still_waiting = coordinator.get_run().status == RunStatus.WAITING_USER
        except Exception:
            still_waiting = False
        if not still_waiting:
            _dispose_workspace()

        status = report.status
        if PipelineStatus is not None and status is PipelineStatus.CANCELLED:
            finish("cancelled")
        elif PipelineStatus is not None and status in (PipelineStatus.FAILED, PipelineStatus.EXHAUSTED):
            finish("failed", report.reason or "pipeline_failed")
        else:
            # NO_CHANGES / NEEDS_USER / COMPLETED: hepsi "done" — proposal
            # varsa (NEEDS_USER) Run WAITING_USER kalır, Apply/Reject bekler.
            finish("done")

    worker.stage.connect(on_stage)
    worker.failed.connect(on_pipeline_failed)
    worker.finished_ok.connect(on_pipeline_finished)
    worker.start()

    # F1 (live agent activity): tails the SAME canonical event log this run
    # writes to (run_runtime is the single source of truth) and emits
    # `run.activity` items live, independent of on_stage's coarse stage
    # boundaries. Best-effort: a streaming failure never affects the run
    # itself (see webhost.api.activity.ActivityStreamer).
    #
    # F2 (takip isteği): the caller stops any PREVIOUS streamer for this run
    # before calling this function again, and passes activity_after_seq =
    # the run's current last_event_seq so the restarted streamer only emits
    # NEW items (the prior streamer already delivered everything up to that
    # point; the UI keeps its own history across a follow-up).
    _stop_activity_streamer()
    streamer = ActivityStreamer(runtime, run_id, after_seq=activity_after_seq)

    def on_activity(item: dict) -> None:
        bridge.emit_event("run.activity", item)

    streamer.activity.connect(on_activity)
    _active["activity_streamer"] = streamer
    streamer.start()

    return worker


@handler("run.followUp")
def _follow_up(params, ctx):
    """F2 (takip isteği): bekleyen bir proposal varken kullanıcının yazdığı
    takip talimatıyla AYNI (kanonik) Run'ı, AYNI worktree'den devam ettirir.

    Yalnızca aktif koşu bir PİPELİNE koşusuysa VE kanonik Run şu anda
    WAITING_USER'daysa VE hâlâ canlı bir worktree'si varsa geçerlidir --
    aksi halde BridgeError (klasik motorda Türkçe, kullanıcıya gösterilecek
    özel bir mesajla)."""
    proj = _require_project()
    feedback = (params.get("feedback") or "").strip()
    if not feedback:
        raise BridgeError("empty_feedback", "Takip isteği boş.")

    if _active.get("engine") != "pipeline":
        raise BridgeError(
            "follow_up_unsupported",
            "Klasik motorda takip isteği desteklenmiyor; yeni bir görev başlatın.",
        )
    w = _active.get("worker")
    if w is not None and w.isRunning():
        raise BridgeError("busy", "Zaten bir koşu sürüyor.")

    coordinator = _active.get("coordinator")
    workspace = _active.get("workspace")
    ports = _active.get("pipeline_ports")
    if coordinator is None or workspace is None or ports is None:
        raise BridgeError("no_active_run", "Takip isteği için bekleyen bir öneri yok.")
    try:
        current = coordinator.get_run()
    except Exception as exc:
        raise BridgeError("canonical_state_unavailable", f"Koşu durumu doğrulanamadı: {exc}")
    if current.status != RunStatus.WAITING_USER:
        raise BridgeError("not_waiting_user", "Takip isteği için bekleyen bir öneri yok.")

    run_id = coordinator.run_id
    runtime = state.get_run_runtime()
    mentions, invalid_mentions = _validate_mentions(proj, params.get("mentions") or [])
    # F6 (@-mentions): önceki koşudan/takip isteklerinden pinlenmiş yollar
    # KORUNUR, yeni bahisler EKLENİR (sırayı bozmadan, yinelenenler atlanır).
    pinned_paths = list(dict.fromkeys(list(_active.get("pinned_paths") or []) + mentions))
    _active["pinned_paths"] = pinned_paths

    task = _active.get("task") or ""
    plan_text = _active.get("plan_text")

    # F2: mevcut öneriler ARTIK GEÇERSİZ -- Uygula/Reddet bu andan itibaren
    # devre dışı kalmalı (yeni bir proposal.ready gelene kadar). Akış/sohbet
    # geçmişi (flow) KORUNUR -- yalnızca diff/proposal durumu sıfırlanır.
    _active["proposals"] = []

    cancel_event = threading.Event()
    _active["cancel_event"] = cancel_event

    bridge = ctx._bridge
    ended = {"flag": False}

    def emit_ui(ev: dict) -> None:
        bridge.emit_event("run.event", {"runId": run_id, "ev": ev})

    def settle_canonical_failure(message: str) -> None:
        try:
            coordinator.finish_failed(message)
        except Exception:
            pass

    def finish(status: str, error: str | None = None) -> None:
        if ended["flag"]:
            return
        ended["flag"] = True
        _stop_activity_streamer()
        payload = {"runId": run_id, "status": status, "engine": "pipeline"}
        if error:
            payload["error"] = error
        bridge.emit_event("run.finished", payload)

    activity_after_seq = current.last_event_seq
    worker = _FollowUpWorker(
        runtime, run_id, workspace, ports, task, feedback, plan_text, cancel_event,
        pinned_paths=pinned_paths,
    )
    _wire_pipeline_worker(
        worker, runtime=runtime, run_id=run_id, coordinator=coordinator, workspace=workspace, proj=proj,
        emit_ui=emit_ui, finish=finish, settle_canonical_failure=settle_canonical_failure,
        ended=ended, bridge=bridge, activity_after_seq=activity_after_seq,
    )
    _active["worker"] = worker

    emit_ui({"type": "followUpStarted", "feedback": feedback})
    for bad in invalid_mentions:
        emit_ui({"type": "info", "text": f"Bahsedilen dosya bulunamadı: {bad}"})
    return {"runId": run_id}


@handler("run.cancel")
def _cancel(params, ctx):
    w = _active.get("worker")
    if w is None or not w.isRunning():
        return {}
    if _active.get("engine") == "pipeline":
        # F7 (gerçek iptal): cancel_token artık yalnızca aşama sınırlarında
        # DEĞİL, devam eden bir Worker/Verification/Reviewer denemesinin
        # İÇİNDEN de gözlemlenir (bkz. agent_runtime.session.AgentSession —
        # her model turn'ünden ve tool çağrısından önce kontrol edilir;
        # process_runtime.ProcessRunner — beklerken 150ms dilimlerle
        # sorgular; acp_runtime.client.AcpClientRuntime — devam eden bir
        # prompt sırasında token'ı izler ve session/cancel gönderir). Bu
        # yüzden Durdur artık gerçekten "mevcut adımı" da kesintiye
        # uğratabilir — yalnızca devam eden TEK bir model turn'ü/HTTP
        # çağrısı istisnadır (bkz. AgentSession._check_cancel docstring).
        cancel_event = _active.get("cancel_event")
        if cancel_event is not None:
            cancel_event.set()
        ctx._bridge.emit_event(
            "run.event",
            {"runId": _active.get("run_id"), "ev": {
                "type": "info", "text": "Durduruluyor…",
            }},
        )
    else:
        w.cancel()
    return {}


def _stale_apply_conflicts(proj: Project, proposals: list[dict]) -> list[dict]:
    """Her önerinin kaydedilmiş taban durumunu (baseHash) BU ANKİ proje dosya
    durumuyla karşılaştırır. Koşu sürerken kullanıcı bir dosyayı (editörde
    kaydederek veya diskte doğrudan) değiştirmiş/oluşturmuş/silmişse, o dosya
    için bir çakışma (conflict) kaydı üretir. Öneri sözlüğünde "baseHash"
    ANAHTARI yoksa (eski/bilinmeyen bir kaynak) GÜVENLİ TARAFTA kalınır ve
    dosya DEĞİŞMİŞ SAYILMAZ — yalnızca gerçekten kaydedilmiş bir taban durumu
    varsa karşılaştırma yapılır."""
    conflicts = []
    for p in proposals:
        if "baseHash" not in p:
            continue
        current = proj.hash_file(p["path"])
        if current != p.get("baseHash"):
            conflicts.append({
                "path": p["path"],
                "reason": (
                    f"Dosya koşu sırasında değişti: {p['path']}. Öneriyi yeniden "
                    "oluşturmak için görevi tekrar çalıştırın."
                ),
            })
    return conflicts


@handler("run.applyProposals")
def _apply(params, ctx):
    proj = _require_project()
    wanted = set(params.get("paths") or [])
    proposals = [p for p in _active.get("proposals", []) if p.get("path") in wanted]
    if not proposals:
        return {"applied": [], "errors": [], "conflicts": [], "checkpointId": None}

    coordinator = _active.get("coordinator")
    if coordinator is None:
        # Bekleyen öneriler var ama kanonik koordinatör yok: KAPALI BAŞARISIZ
        # olunur — dosya sistemine DOKUNULMAZ, öneriler TEMİZLENMEZ.
        raise BridgeError("no_active_run", "Aktif bir kanonik koşu yok; öneri uygulanamaz.")

    # "Stale apply" koruması: checkpoint OLUŞTURULMADAN ÖNCE, seçilen her
    # önerinin taban durumu bu anki proje dosyasıyla karşılaştırılır. Koşu
    # dakikalarca sürebildiği için kullanıcı bu süre içinde proposal'daki bir
    # dosyayı değiştirmiş olabilir (editörde kaydetmiş veya diskte elle
    # düzenlemiş) — bu durumda apply SESSİZCE üzerine yazmaz: hiçbir dosyaya
    # DOKUNULMAZ, checkpoint OLUŞTURULMAZ, öneriler bekleyen (pending) kalır
    # (kullanıcı Reddet'i kullanabilir ya da görevi tekrar çalıştırabilir).
    # Kanonik Run WAITING_USER'da KALIR (proposal.applied kaydedilmez).
    conflicts = _stale_apply_conflicts(proj, proposals)
    if conflicts:
        return {"applied": [], "errors": [], "conflicts": conflicts, "checkpointId": None}

    try:
        checkpoint = CheckpointStore(proj.root).create(
            proj, [p["path"] for p in proposals], _active.get("run_id"),
        )
    except Exception as e:
        raise BridgeError("checkpoint", f"Checkpoint oluşturulamadı: {e}")

    applied, errors = [], []
    for p in proposals:
        try:
            if p.get("is_deleted"):
                # T1.2 (yeni motor): worktree'de silinmiş dosya — checkpoint
                # zaten önceki içeriği sakladığı için restore ile geri
                # dönülebilir (bkz. CheckpointStore.restore).
                if proj.exists(p["path"]):
                    proj.delete(p["path"])
            else:
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
        return {"applied": [], "errors": errors, "conflicts": [], "checkpointId": None}

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
    if _active.get("engine") == "pipeline":
        _dispose_workspace()
    ctx._bridge.emit_event("fs.changed", {"kind": "modified", "paths": applied})
    return {
        "applied": applied,
        "errors": [],
        "conflicts": [],
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
    if _active.get("engine") == "pipeline":
        _dispose_workspace()
    return {}


def shutdown():
    """Uygulama kapanırken koşuyu iptal et (zombi thread önleme)."""
    w = _active.get("worker")
    if w is not None and w.isRunning():
        if _active.get("engine") == "pipeline":
            cancel_event = _active.get("cancel_event")
            if cancel_event is not None:
                cancel_event.set()
        else:
            w.cancel()
        w.wait(2000)
    _stop_activity_streamer()
    _dispose_workspace()
