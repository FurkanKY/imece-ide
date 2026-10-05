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
import re
import copy
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal

import ui_prefs
import engine_factory
from checkpoints import CheckpointStore
from project import IGNORE_EXT, Project
from project_runner import run_project_task
import providers as provider_registry
from agents import DEFAULT_ROUTING
from run_runtime.events import RunEventType
from run_runtime.legacy import LegacyRunCoordinator
from run_runtime.pipeline import CanonicalPipelineRecorder
from run_runtime.models import RunStatus
from agent_runtime.cancellation import CancellationToken, OperationCancelledError
from agent_execution_runtime import (
    AgentExecutionRequest, AgentExecutionStatus, AgentRunCoordinator, build_agent_ports, execute_task,
)
from context_runtime import load_project_rules
from webhost import state
from webhost.api.activity import ActivityStreamer
from webhost.bridge import handler, BridgeError
from webhost.run_registry import RunSlot, registry as _run_registry

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
    # Jev System One karar katmanı (bkz. decision_runtime/, engine_factory.
    # build_verification_failure_gate): run.start sırasında bir kez kurulan
    # VerificationFailureGate | None -- run.followUp AYNI kapıyı yeniden
    # kullanır (bkz. _follow_up).
    "decision_gate": None,
    "collab_session": None,
}


def _execute_with_collaboration(worker, operation):
    """Run one pipeline execution under a worker-thread-owned collaboration lease."""
    session = worker.collab_session
    if session is None:
        try:
            worker.finished_ok.emit(operation())
        except Exception as exc:
            worker.failed.emit(str(exc))
        return
    from pipeline_runtime.models import PipelineReport
    report = None
    error = None
    cleanup_failed = False
    try:
        try:
            session.activate(worker.workspace, cancel_token=CancellationToken.from_event(worker.cancel_event))
            report = operation()
        except OperationCancelledError:
            report = PipelineReport(worker.run_id, PipelineStatus.CANCELLED, "cancelled")
        except Exception as exc:
            error = "collab_activation_failed" if exc.__class__.__name__ == "HostCollaborationError" else "collab_run_failed"
    finally:
        try:
            session.deactivate()
        except Exception:
            cleanup_failed = True
    if cleanup_failed:
        worker.failed.emit("collab_cleanup_failed")
        return
    if report is not None:
        worker.finished_ok.emit(report)
    elif error is not None:
        worker.failed.emit(error)


def get_collaboration_status(run_id=None):
    """Return the in-memory, credential-free collaboration status for a run."""
    session = _active.get("collab_session")
    current_id = _active.get("run_id")
    if run_id is not None and run_id != current_id:
        return None
    cached = state.get_collaboration_status_cache(run_id)
    if cached and cached.get("state") == "cleanup_failed":
        return {**cached, "runId": current_id}
    if session is None:
        return {**cached, "runId": current_id} if cached else None
    try:
        live = dict(session.status())
        if cached and cached.get("state") in _COLLAB_TERMINAL_STATES and live.get("state") not in _COLLAB_TERMINAL_STATES:
            live = cached
        return {**live, "runId": current_id}
    except Exception:
        return {**_unavailable_collaboration_status(cached), "runId": current_id}

# F6 (@-mentions): backend cap independent of (and enforced regardless of)
# the Composer's own UI-level cap — a client is never trusted blindly.
_MAX_MENTIONS = 10
_draining_workers: list[tuple[object, object, object, str | None]] = []
_delivery_lock = threading.RLock()
_delivery_leases: dict[tuple[int, str], int] = {}
_delivery_closing: set[tuple[int, str]] = set()
_project_apply_locks_guard = threading.Lock()
_project_apply_locks: dict[str, threading.RLock] = {}


def _project_apply_lock(root):
    key = str(Path(root).resolve())
    with _project_apply_locks_guard:
        return _project_apply_locks.setdefault(key, threading.RLock())


def _resolve_agent_slot(run_id=None, project_root=None):
    eligible = [s for s in _run_registry.open_slots(project_root) if s.proposals or s.phase in ("running", "starting", "waiting_user")]
    if run_id is not None:
        slot = _run_registry.get(run_id)
        if slot is None:
            raise BridgeError("unknown_run", "Koşu bulunamadı.")
        if slot not in eligible:
            raise BridgeError("run_not_active", "Koşu artık işlem kabul etmiyor.")
        return slot
    if len(eligible) != 1:
        raise BridgeError("run_id_required", "Belirsiz koşu için runId gereklidir.")
    return eligible[0]


def _legacy_run_id_matches(params):
    # Registry ownership survives changes to the legacy compatibility mirror,
    # including a later agent's failed admission.
    if "runId" in params and _run_registry.get(params["runId"]) is not None:
        return False
    if _active.get("engine") == "agent":
        return False
    if "runId" not in params:
        return False
    if params.get("runId") != _active.get("run_id") or not _active.get("run_id"):
        raise BridgeError("unknown_run", "Koşu bulunamadı.")
    return True


def _validate_run_rpc_params(params, allowed, *, required=()):
    if not isinstance(params, dict) or set(params) - set(allowed) or any(key not in params for key in required):
        raise BridgeError("invalid_params", "Koşu isteğinin alanları geçersiz.")
    if "runId" in params and (type(params["runId"]) is not str or not 0 < len(params["runId"]) <= 256):
        raise BridgeError("invalid_run_id", "runId geçersiz.")


def _delivery_is_busy(session=None, run_id=None):
    with _delivery_lock:
        keys = set(_delivery_closing) | {key for key, count in _delivery_leases.items() if count}
        return any((session is None or key[0] == id(session)) and
                   (run_id is None or key[1] == run_id) for key in keys)


@contextmanager
def borrow_delivery_context(run_id):
    """Lease the immutable native-run inputs while delivery reads/captures them."""
    borrowed, session, key = _capture_delivery_context(run_id)
    with _delivery_lock:
        if key in _delivery_closing:
            raise RuntimeError("busy")
        _delivery_leases[key] = _delivery_leases.get(key, 0) + 1
    try:
        # Recheck after acquiring the lease: a shutdown drain may have won the
        # narrow interval between the first check and the lease registration.
        _validate_delivery_context(run_id, borrowed, session)
        yield borrowed
    finally:
        with _delivery_lock:
            remaining = _delivery_leases.get(key, 0) - 1
            if remaining > 0:
                _delivery_leases[key] = remaining
            else:
                _delivery_leases.pop(key, None)
        if any(entry[1] is session and entry[3] == run_id for entry in list(_draining_workers)):
            _drain_collaboration_resources()


@contextmanager
def borrow_participant_context(run_id):
    """Lease an accepted, idle pipeline run for a private participant command."""
    with borrow_delivery_context(run_id) as borrowed:
        project = state.get_project()
        if (project is None or state.project_generation() != borrowed["generation"] or
                Path(project.root).resolve(strict=True) != borrowed["project_root"]):
            raise RuntimeError("stale")
        yield borrowed


def _capture_delivery_context(run_id):
    from collab_runtime.context import SharedSnapshot

    project = state.get_project()
    session = _active.get("collab_session")
    worker = _active.get("worker")
    workspace = _active.get("workspace")
    binding = session.accepted_binding if session is not None else None
    if (project is None or type(run_id) is not str or not run_id or
            _active.get("run_id") != run_id or _active.get("engine") != "pipeline" or
            session is None or workspace is None or not isinstance(binding, SharedSnapshot)):
        raise RuntimeError("invalid")
    try:
        root = Path(project.root).resolve(strict=True)
        if root != Path(session.project_root).resolve(strict=True):
            raise RuntimeError("stale")
        if not Path(workspace.root).is_dir() or (worker is not None and not _worker_is_finished(worker)):
            raise RuntimeError("busy")
        current = _active["coordinator"].get_run()
        if current.status != RunStatus.WAITING_USER:
            raise RuntimeError("busy")
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("invalid") from None
    key = (id(session), run_id)
    return ({"session": session, "workspace": workspace, "binding": binding,
             "binding_data": binding.to_dict(),
             "project_root": root, "generation": state.project_generation(), "run_id": run_id,
             "available_paths": tuple(p.get("path") for p in (_active.get("proposals") or [])
                                       if isinstance(p, dict) and isinstance(p.get("path"), str))},
            session, key)


def _validate_delivery_context(run_id, borrowed, session):
    from collab_runtime.context import SharedSnapshot

    project = state.get_project()
    if (project is None or _active.get("run_id") != run_id or
            _active.get("collab_session") is not session or
            _active.get("workspace") is not borrowed["workspace"]):
        raise RuntimeError("stale")
    try:
        binding = borrowed["session"].accepted_binding
        if (Path(project.root).resolve(strict=True) != borrowed["project_root"] or
                not Path(borrowed["workspace"].root).is_dir() or
                not isinstance(binding, SharedSnapshot) or binding.to_dict() != borrowed["binding_data"]):
            raise RuntimeError("stale")
        worker = _active.get("worker")
        if worker is not None and not _worker_is_finished(worker):
            raise RuntimeError("busy")
        if _active["coordinator"].get_run().status != RunStatus.WAITING_USER:
            raise RuntimeError("busy")
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("invalid") from None
_collaboration_cleanup_lock = threading.RLock()
_collaboration_cleanup_codes: dict[tuple[int, str | None], str] = {}
_COLLAB_TERMINAL_STATES = {"access_denied", "resnapshot_required", "protocol_error", "server_error"}


def _retain_collaboration(session, workspace, run_id, worker=None, terminal_code="run_finished"):
    with _collaboration_cleanup_lock:
        key = (id(session), run_id)
        existing = next((item for item in _draining_workers
                         if item[1] is session and item[3] == run_id), None)
        if existing is None:
            _draining_workers.append((worker, session, workspace, run_id))
        elif existing[0] is None and worker is not None:
            _draining_workers[_draining_workers.index(existing)] = (worker, session, workspace, run_id)
        _collaboration_cleanup_codes.setdefault(key, terminal_code)


def _cache_collaboration_status(session, run_id, state_name, code):
    if _active.get("run_id") != run_id or _active.get("collab_session") is not session:
        return
    try:
        status = dict(session.status())
        previous = state.get_collaboration_status_cache(run_id)
        previous_terminal = None
        if previous:
            if previous.get("state") in _COLLAB_TERMINAL_STATES:
                previous_terminal = (previous["state"], previous.get("code"))
            elif previous.get("recoveryState") in _COLLAB_TERMINAL_STATES:
                previous_terminal = (previous["recoveryState"], previous.get("recoveryCode"))
        if previous_terminal:
            status["recoveryState"], status["recoveryCode"] = previous_terminal
        if state_name == "cleanup_failed":
            if status.get("state") in _COLLAB_TERMINAL_STATES:
                status["recoveryState"] = status["state"]
                status["recoveryCode"] = status.get("code")
            status["state"], status["code"] = "cleanup_failed", "cleanup_failed"
        elif previous_terminal and status.get("state") not in _COLLAB_TERMINAL_STATES:
            status["state"], status["code"] = previous_terminal
        elif status.get("state") not in _COLLAB_TERMINAL_STATES:
            status["state"], status["code"] = state_name, code
        state.set_collaboration_status_cache({
            "runId": run_id, "projectRoot": str(session.project_root), "status": status,
        })
    except Exception:
        state.set_collaboration_status_cache({
            "runId": run_id, "projectRoot": str(getattr(session, "project_root", "")),
            "status": _unavailable_collaboration_status(state.get_collaboration_status_cache(run_id)),
        })


def _unavailable_collaboration_status(cached=None):
    status = dict(cached or {})
    if status.get("state") in _COLLAB_TERMINAL_STATES:
        status["recoveryState"], status["recoveryCode"] = status["state"], status.get("code")
    status.update({
        "state": "unavailable", "code": "unavailable",
        "consumedRevision": status.get("consumedRevision"),
        "receivedRevision": status.get("receivedRevision"),
        "pendingCount": status.get("pendingCount", 0),
        "sessionId": status.get("sessionId", ""),
        "taskId": status.get("taskId", ""),
        "memberId": status.get("memberId", ""),
        "active": status.get("active"),
    })
    return status


def _worker_is_finished(worker):
    if worker is None:
        return True
    try:
        return bool(worker.isFinished())
    except Exception:
        return False


def _drain_collaboration_resources():
    """Retry only quiescent retained resources; active workers keep ownership."""
    with _collaboration_cleanup_lock:
        entries = list(_draining_workers)
        for entry in entries:
            worker, session, workspace, run_id = entry
            with _delivery_lock:
                if not _worker_is_finished(worker) or _delivery_is_busy(session, run_id):
                    continue
                key = (id(session), run_id)
                _delivery_closing.add(key)
            try:
                try:
                    session.close()
                except Exception:
                    _cache_collaboration_status(session, run_id, "cleanup_failed", "cleanup_failed")
                    continue
                if workspace is not None:
                    try:
                        workspace.dispose()
                    except Exception:
                        _cache_collaboration_status(session, run_id, "cleanup_failed", "cleanup_failed")
                        continue
                terminal_code = _collaboration_cleanup_codes.get(key, "run_finished")
                _cache_collaboration_status(session, run_id, "closed", terminal_code)
                _draining_workers.remove(entry)
                _collaboration_cleanup_codes.pop(key, None)
                if _active.get("run_id") == run_id and _active.get("collab_session") is session:
                    _active["collab_session"] = None
                    if _active.get("workspace") is workspace:
                        _active["workspace"] = None
            finally:
                with _delivery_lock:
                    _delivery_closing.discard(key)


def _cleanup_after_collaboration_decision(session, run_id, terminal_code):
    """Keep a committed apply/reject result successful while cleanup is retriable."""
    workspace = _active.get("workspace")
    if session is None:
        if _active.get("engine") == "agent":
            _dispose_agent_workspace(workspace)
        else:
            _dispose_workspace()
        return
    if _delivery_is_busy(session, run_id):
        _retain_collaboration(session, workspace, run_id, terminal_code=terminal_code)
        return
    try:
        session.close()
    except Exception:
        _retain_collaboration(session, workspace, run_id, terminal_code=terminal_code)
        _cache_collaboration_status(session, run_id, "cleanup_failed", "cleanup_failed")
        return
    if workspace is not None:
        try:
            workspace.dispose()
        except Exception:
            _retain_collaboration(session, workspace, run_id, terminal_code=terminal_code)
            _cache_collaboration_status(session, run_id, "cleanup_failed", "cleanup_failed")
            return
    _cache_collaboration_status(session, run_id, "closed", terminal_code)
    if _active.get("run_id") == run_id and _active.get("collab_session") is session:
        _active["collab_session"] = None
        if _active.get("workspace") is workspace:
            _active["workspace"] = None


def _retire_collaboration_after_worker(worker, session, workspace, run_id, terminal_code="run_finished"):
    """Keep worker-owned resources alive until QThread has actually exited."""
    if session is None:
        return
    with _collaboration_cleanup_lock:
        entry = next((item for item in _draining_workers
                      if item[1] is session and item[3] == run_id), None)
        if entry is None:
            entry = (worker, session, workspace, run_id)
            _draining_workers.append(entry)
        _collaboration_cleanup_codes.setdefault((id(session), run_id), terminal_code)

    def cleanup():
        if not _worker_is_finished(worker):
            return
        _drain_collaboration_resources()

    if _worker_is_finished(worker):
        cleanup()
    else:
        worker.finished.connect(cleanup)
        # Close the check/connect race where QThread exits between the two.
        if _worker_is_finished(worker):
            cleanup()

# kanonik pipeline rolü -> legacy routing anahtarı (bkz. agents.DEFAULT_ROUTING).
_ROLE_ROUTING_KEY = {"planner": "planner", "worker": "coder", "reviewer": "reviewer"}
_ROLE_LEGACY_STAGE = {"planner": "plan", "worker": "code", "reviewer": "review"}

# A5 (hata UX): terminal hata kartı için TEK Türkçe eşleme noktası. Bugün her
# alt katman hatası (agent_runtime/acp_runtime/process_runtime/workspace...)
# run.py'ye ulaşmadan ÖNCE bir metin dizesine ("str(exc)") düzleştirilir (bkz.
# _PipelineWorker.run()/_Worker.run()/_FollowUpWorker.run()'ın `except
# Exception as e: ... str(e)` blokları) -- bu yüzden burada tip bazlı değil,
# run_runtime.completion.RunCompletionGate'in ZATEN ürettiği kanonik
# `RunRecord.error_code` (execution_failed/verification_fail/
# verification_timeout/verification_error/fix_loop_exhausted/
# fix_loop_failed) ve run_runtime.legacy'nin kendi yerleşim kodları
# (legacy_worker_error/legacy_lifecycle_start_failed) TEMEL ALINIR;
# _classify_error bu kaba kodu, ham hata metnindeki tanınabilir anahtar
# sözcüklerle (best-effort) daha spesifik bir kutuya (auth/hız sınırı/zaman
# aşımı/...) İNCELTİR. Tanınamayan bir metin, kodun kendi genel mesajına
# düşer -- ASLA hatasız görünmez.
_ERROR_MESSAGES: dict[str, tuple[str, str]] = {
    "provider_not_configured": (
        "Sağlayıcı yapılandırılmamış",
        "Seçili rol için API anahtarı veya hesap girişi eksik. Ayarlar'dan sağlayıcıyı "
        "yapılandırıp görevi tekrar başlatın.",
    ),
    "auth_required": (
        "Kimlik doğrulama gerekiyor",
        "Sağlayıcı oturumu geçersiz veya süresi dolmuş. Hesapla yeniden giriş yapıp "
        "tekrar deneyin.",
    ),
    "acp_agent_unavailable": (
        "Ajan başlatılamadı",
        "Seçili CLI ajanı kurulu değil, bulunamadı ya da çalışırken çöktü. Kurulumu "
        "kontrol edip tekrar deneyin.",
    ),
    "rate_limited": (
        "Kullanım sınırına ulaşıldı",
        "Sağlayıcı isteği hız veya kota sınırı nedeniyle reddetti. Biraz bekleyip "
        "tekrar deneyin.",
    ),
    "timeout": (
        "Zaman aşımı",
        "İstek beklenen sürede tamamlanmadı. Ağ bağlantınızı kontrol edip tekrar deneyin.",
    ),
    "network_error": (
        "Ağ hatası",
        "Sağlayıcıya bağlanılamadı. İnternet bağlantınızı kontrol edip tekrar deneyin.",
    ),
    "verification_tool_missing": (
        "Doğrulama aracı bulunamadı",
        "Projenin doğrulama komutu (ör. test veya derleme aracı) sistemde bulunamadı. "
        "Aracı kurup tekrar deneyin.",
    ),
    "worktree_git_failure": (
        "Çalışma alanı hatası",
        "İzole git çalışma alanı oluşturulamadı veya bozuldu. Projenin git durumunu "
        "kontrol edip tekrar deneyin.",
    ),
    "fix_loop_exhausted": (
        "Otomatik düzeltme tükendi",
        "Doğrulama defalarca başarısız oldu ve otomatik düzeltme deneme hakkı bitti. "
        "Planı gözden geçirip tekrar deneyin.",
    ),
    "provider_unavailable": (
        "Sağlayıcıya ulaşılamadı",
        "Seçili model/sağlayıcı isteğe beklenmedik şekilde yanıt veremedi. Tekrar "
        "deneyin ya da farklı bir sağlayıcı seçin.",
    ),
    "generic": (
        "Koşu tamamlanamadı",
        "Beklenmeyen bir hata oluştu. Ayrıntılara bakıp tekrar deneyin.",
    ),
}


def _classify_error(code: str | None, message: str | None) -> str:
    """Kaba kanonik `error_code`'u (bkz. yukarıdaki modül notu) ham hata
    metnindeki anahtar sözcüklerle daha spesifik bir _ERROR_MESSAGES
    anahtarına inceltir. Asla KeyError vermez -- tanınmayan her şey
    "generic"e düşer."""
    lower = (message or "").lower()
    if code == "verification_tool_missing":
        return code
    if code == "fix_loop_exhausted":
        return "fix_loop_exhausted"
    if "executable not found" in lower or ("bulunamadı" in lower and ("araç" in lower or "komut" in lower)):
        return "verification_tool_missing"
    if re.search(r"\b(unauthori[sz]ed|authentication|auth(entication)? required|401)\b", lower) or "kimlik doğrula" in lower:
        return "auth_required"
    if re.search(r"\b(rate[ _-]?limit\w*|429)\b", lower) or "kota" in lower:
        return "rate_limited"
    if "zaman aşım" in lower or "timeout" in lower:
        return "timeout"
    if "acp" in lower and ("başlat" in lower or "spawn" in lower or "bulunamadı" in lower):
        return "acp_agent_unavailable"
    if re.search(r"\b(git|worktree)\b", lower):
        return "worktree_git_failure"
    if "api anahtarı" in lower or "yapılandırılmadı" in lower or "eksik alan" in lower:
        return "provider_not_configured"
    if "bağlan" in lower or "network" in lower or "connection" in lower:
        return "network_error"
    if code in ("execution_failed", "legacy_worker_error", "verification_error", "fix_loop_failed"):
        return "provider_unavailable"
    return "generic"


def _error_details(coordinator, message: str | None) -> dict:
    """`finish("failed", message)` çağıranların ortak ekidir: kanonik
    Run'ın (varsa) `error_code`'unu okuyup _classify_error ile inceltir ve
    run.finished'a eklenecek {errorCode, errorTitle, errorDescription}
    alanlarını üretir. Kanonik durum okunamazsa (best-effort) code=None ile
    devam edilir -- bu yüzden ASLA fırlatmaz."""
    code = None
    try:
        if coordinator is not None:
            code = coordinator.get_run().error_code
    except Exception:
        code = None
    key = _classify_error(code, message)
    title, description = _ERROR_MESSAGES[key]
    return {"errorCode": key, "errorTitle": title, "errorDescription": description}


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

    def __init__(self, runtime, run_id, workspace, ports, task, cancel_event, pinned_paths=None,
                 decision_gate=None, collab_session=None):
        super().__init__()
        self.runtime = runtime
        self.run_id = run_id
        self.workspace = workspace
        self.ports = ports
        self.task = task
        self.cancel_event = cancel_event
        self.pinned_paths = pinned_paths or []
        self.decision_gate = decision_gate
        self.collab_session = collab_session

    def run(self):
        def execute():
            pipeline = PipelineRunner(
                self.runtime,
                planner=self.ports.planner, worker=self.ports.worker,
                verification=self.ports.verification, reviewer=self.ports.reviewer,
                change_provider=self.ports.change_provider,
                decision_gate=self.decision_gate,
            )
            return pipeline.run(
                self.run_id, self.workspace, self.task,
                cancel_event=self.cancel_event,
                on_stage=lambda s, info: self.stage.emit(s, dict(info)),
                pinned_paths=self.pinned_paths,
            )
        _execute_with_collaboration(self, execute)


class _FollowUpWorker(QThread):
    """F2 (takip isteği): _PipelineWorker'ın AYNI QThread desenini izler, ama
    PipelineRunner.run() yerine continue_with_feedback() çağırır -- AYNI
    worktree'den, YENİ bir Planner denemesi OLMADAN devam eder."""

    stage = Signal(str, dict)
    finished_ok = Signal(object)  # PipelineReport
    failed = Signal(str)

    def __init__(
        self, runtime, run_id, workspace, ports, task, feedback, plan_text, cancel_event,
        pinned_paths=None, max_fix_attempts=None, decision_gate=None, collab_session=None,
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
        self.decision_gate = decision_gate
        self.collab_session = collab_session

    def run(self):
        def execute():
            pipeline = PipelineRunner(
                self.runtime,
                planner=self.ports.planner, worker=self.ports.worker,
                verification=self.ports.verification, reviewer=self.ports.reviewer,
                change_provider=self.ports.change_provider,
                decision_gate=self.decision_gate,
            )
            kwargs = {}
            if self.max_fix_attempts is not None:
                kwargs["max_fix_attempts"] = self.max_fix_attempts
            return pipeline.continue_with_feedback(
                self.run_id, self.workspace, self.feedback,
                task=self.task, plan_report_or_text=self.plan_text, pinned_paths=self.pinned_paths,
                cancel_event=self.cancel_event,
                on_stage=lambda s, info: self.stage.emit(s, dict(info)),
                **kwargs,
            )
        _execute_with_collaboration(self, execute)


class _AgentWorker(QThread):
    """One bounded single-agent execution; no planner/reviewer/pipeline report."""
    stage = Signal(str, dict)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, runtime, run_id, workspace, ports, task, provider_id, cancel_event, mentions):
        super().__init__()
        self.runtime, self.run_id, self.workspace, self.ports = runtime, run_id, workspace, ports
        self.task, self.provider_id, self.cancel_event = task, provider_id, cancel_event
        self.mentions = tuple(mentions)
        self.feedback = None

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            result = execute_task(
                self.runtime, self.run_id,
                AgentExecutionRequest(self.task if self.feedback is None else
                                      f"Original task:\n{self.task}\n\nUser follow-up:\n{self.feedback}",
                                      self.provider_id, self.workspace,
                                      rules=load_project_rules(self.workspace.root),
                                      pinned_paths=self.mentions),
                ports=self.ports, cancel_token=CancellationToken.from_event(self.cancel_event),
                on_stage=lambda stage: self.stage.emit(stage, {}),
            )
            self.finished_ok.emit(result)
        except Exception:
            self.failed.emit("agent_execution_failed")


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


def _require_agent_project(proj: Project) -> None:
    """Prevent an agent's pending proposal/worktree being retargeted to another project."""
    if _active.get("engine") != "agent":
        return
    expected = _active.get("agent_project_root")
    workspace = _active.get("workspace")
    if expected is None and workspace is not None:
        expected = getattr(getattr(workspace, "snapshot", None), "source_root", None)
    try:
        matches = expected is not None and Path(proj.root).resolve(strict=True) == Path(expected).resolve(strict=True)
    except (OSError, RuntimeError, TypeError):
        matches = False
    if not matches:
        raise BridgeError("agent_project_stale", "Tek ajan koşusunun özgün projesi artık seçili değil.")


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


@handler("run.list")
def _list_runs(params, ctx):
    _validate_run_rpc_params(params, set())
    project = _require_project()
    root = str(Path(project.root).resolve())
    items = []
    for slot in _run_registry.slots():
        if slot.project_root != root:
            continue
        try:
            run = slot.coordinator.get_run()
            status = run.status.value
        except Exception:
            status = "unavailable"
        items.append({"runId": slot.run_id, "taskId": slot.task_id, "task": slot.task,
                      "status": status, "phase": slot.phase, "providerId": slot.provider_id,
                      "engine": "agent", "changedPathCount": len(slot.proposals),
                      "errorCode": slot.error_code})
    return {"runs": items}


@handler("run.get")
def _get_run(params, ctx):
    _validate_run_rpc_params(params, {"runId"}, required={"runId"})
    project = _require_project()
    run_id = params.get("runId")
    slot = _run_registry.get(run_id) if isinstance(run_id, str) else None
    if slot is None:
        raise BridgeError("unknown_run", "Koşu bulunamadı.")
    if str(Path(project.root).resolve()) != slot.project_root:
        raise BridgeError("run_project_mismatch", "Koşu başka bir projeye ait.")
    try:
        status = slot.coordinator.get_run().status.value
    except Exception:
        status = "unavailable"
    return {"runId": slot.run_id, "task": slot.task, "providerId": slot.provider_id,
            "status": status, "phase": slot.phase, "engine": "agent",
            "evidence": copy.deepcopy(slot.evidence), "proposals": copy.deepcopy(slot.proposals),
            "totals": dict(slot.totals), "errorCode": slot.error_code, "checkpointId": slot.checkpoint_id}


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


def _decision_gate_needs_user_message(runtime, run_id: str) -> str | None:
    """Jev System One karar katmanı (bkz. decision_runtime.gate.
    VerificationFailureGate) bir NEEDS_USER kararı verdiğinde, pipeline_
    runtime.runner._settle_decision_needs_user bunu CanonicalPipelineRecorder.
    needs_user() ile proposal.ready olayının payload'ına "message" anahtarıyla
    yazar (bkz. decision_runtime.triage._NEEDS_USER_MESSAGES -- Türkçe,
    kullanıcıya gösterilecek metin). Bu fonksiyon SON proposal.ready olayının
    bu anahtarını okur; ne karar katmanı devrede ne de bu koşuda bir
    proposal.ready varsa None döner ("no verification plan" gibi karar
    katmanıyla İLGİSİZ NEEDS_USER yolları hiç "message" yazmaz)."""
    try:
        events = []
        page = runtime.events(run_id, limit=500)
        events.extend(page.events)
        while page.has_more:
            page = runtime.events(run_id, after_seq=events[-1].seq, limit=500)
            events.extend(page.events)
    except Exception:
        return None

    last_message: str | None = None
    for event in events:
        if event.type == RunEventType.PROPOSAL_READY:
            message = event.payload.get("message")
            last_message = message if isinstance(message, str) and message.strip() else None
    return last_message


def _full_plan_text(plan_report) -> str:
    """F2 (takip isteği) düzeltmesi: PipelineRunner.continue_with_feedback'e
    yalnızca `plan_report.summary` (tek cümlelik özet) DEĞİL, Planner'ın
    ürettiği TÜM plan -- adımlar/kabul kriterleri/riskler dahil -- metin
    olarak geçirilsin diye. `continue_with_feedback` bir PlanReport
    aldığında yine de yalnızca `.summary`'sini kullanır (bkz.
    pipeline_runtime.runner.PipelineRunner.continue_with_feedback
    docstring'i); bu yüzden zenginleştirme BURADA, çağrı yerinde, düz bir
    string olarak yapılır -- runner'a DOKUNULMAZ."""
    lines = [plan_report.summary]
    if plan_report.steps:
        lines.append("Adımlar:")
        for i, step in enumerate(plan_report.steps, start=1):
            lines.append(f"{i}. {step.title}: {step.objective}")
    if plan_report.acceptance_criteria:
        lines.append("Kabul kriterleri:")
        for c in plan_report.acceptance_criteria:
            lines.append(f"- {c}")
    if plan_report.risks:
        lines.append("Riskler:")
        for r in plan_report.risks:
            lines.append(f"- {r}")
    return "\n".join(lines)


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
        # Jev System One karar katmanı NEEDS_USER kararı verdiyse (bkz.
        # decision_runtime.gate.VerificationFailureGate), kullanıcıya
        # gösterilecek Türkçe gerekçe (ör. "eksik bağımlılık" / "ortam
        # sorunu") proposal.ready olayının kanonik payload'ında "message"
        # anahtarıyla saklanır -- burada okunup önerinin HEMEN ÜSTÜNDE bir
        # bilgi satırı olarak gösterilir, aksi halde kullanıcı yalnızca bir
        # öneri kartı görür ve NEDEN durdurulduğunu asla öğrenemez.
        if runtime is not None and run_id is not None:
            decision_message = _decision_gate_needs_user_message(runtime, run_id)
            if decision_message:
                emit_ui({"type": "info", "text": decision_message})
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
    agent_mode = "providerId" in params
    collab_requested = "collabApprovalHandle" in params
    collab_handle = params.get("collabApprovalHandle") if collab_requested else None
    if collab_requested and (type(collab_handle) is not str or not collab_handle):
        raise BridgeError("collab_invalid", "Yerel işbirliği onayı geçersiz.")
    task = (params.get("task") or "").strip()
    if not task:
        raise BridgeError("empty_task", "Görev boş.")
    _drain_collaboration_resources()
    if any(item[1] is not None and
           Path(proj.root).resolve() == Path(item[1].project_root).resolve()
           for item in list(_draining_workers)):
        raise BridgeError("collab_cleanup_failed", "Önceki yerel işbirliği kaynağı güvenle kapatılamadı.")
    if _active["worker"] is not None and _active["worker"].isRunning() and not agent_mode:
        raise BridgeError("busy", "Zaten bir koşu sürüyor.")
    if _delivery_is_busy():
        raise BridgeError("busy", "Teslimat işlemi sürüyor.")
    if not agent_mode and _run_registry.open_slots():
        raise BridgeError("busy", "Tek ajan koşuları sürüyor.")
    if not agent_mode and _active_canonical_run_blocks_start():
        # Kanonik Run hâlâ terminal-olmayan bir durumda (CREATED/RUNNING/
        # WAITING_USER) olabilir; yeni bir koşu başlatmak bu durumu sessizce
        # terk ederdi.
        raise BridgeError("pending_proposals", "Bekleyen öneriler var; önce uygula veya reddet.")
    if agent_mode and _active.get("engine") != "agent" and _active_canonical_run_blocks_start():
        raise BridgeError("busy", "Başka bir koşu sürüyor.")
    if not agent_mode and _active.get("engine") == "agent" and _active.get("workspace") is not None:
        if not _dispose_agent_workspace(_active["workspace"]):
            raise BridgeError("workspace_cleanup_failed", "Tek ajan çalışma alanı güvenle kapatılamadı.")

    # Explicit provider mode bypasses all legacy routing and three-role setup.
    if "providerId" in params:
        provider_id = params.get("providerId")
        if type(provider_id) is not str or not provider_id or len(provider_id) > 128:
            raise BridgeError("invalid_provider", "Sağlayıcı geçersiz.")
        if "routing" in params or any(k in params for k in ("planner", "coder", "reviewer")):
            raise BridgeError("conflicting_routing", "Sağlayıcı modu diğer yönlendirmelerle kullanılamaz.")
        if collab_requested:
            raise BridgeError("collab_unsupported", "Tek ajan koşuları yerel işbirliğini desteklemiyor.")
        supported, why = engine_factory.role_supported(provider_id)
        if not supported:
            raise BridgeError("invalid_provider", why)
        if engine_factory._repo_root_for(proj.root) is None:
            raise BridgeError("agent_requires_git", "Tek ajan koşusu için Git deposu gerekir.")
        if len(task) > 20_000:
            raise BridgeError("task_too_long", "Görev çok uzun.")
        mentions, invalid_mentions = _validate_mentions(proj, params.get("mentions") or [])
        project_root = str(Path(proj.root).resolve())
        if not _run_registry.reserve(project_root):
            raise BridgeError("run_capacity", "Bu proje için en fazla iki ajan koşusu tutulabilir.")
        coordinator = None
        try:
            runtime = state.get_run_runtime()
            coordinator = AgentRunCoordinator.start(
                runtime, project_root=proj.root, task=task, provider_id=provider_id,
            )
        except Exception:
            _run_registry.release(project_root)
            raise
        run_id = coordinator.run_id
        workspace = None
        try:
            workspace = engine_factory.create_pipeline_workspace(proj.root, run_id)
            ports = build_agent_ports(runtime, run_id, provider_id)
        except Exception:
            _run_registry.release(project_root)
            if coordinator is not None:
                try:
                    coordinator.finish_failed("agent_worker_unavailable")
                except Exception:
                    pass
            if workspace is not None:
                try:
                    workspace.dispose()
                except Exception:
                    if coordinator is not None:
                        record = coordinator.get_run()
                        retained = RunSlot(run_id, record.task_id, project_root, provider_id, coordinator,
                                           workspace=workspace, task=task, phase="cleanup_failed",
                                           error_code="workspace_cleanup_failed")
                        _run_registry.add(retained)
            raise BridgeError("worker_start_failed", "Tek ajan worker'ı başlatılamadı.") from None
        cancel_event = threading.Event()
        record = coordinator.get_run()
        slot = RunSlot(run_id, record.task_id, project_root, provider_id, coordinator,
                       workspace=workspace, ports=ports, task=task,
                       pinned_paths=list(mentions), cancel_event=cancel_event)
        _run_registry.add(slot)
        _active.update({"worker": None, "coordinator": coordinator, "run_id": run_id, "proposals": [],
                        "engine": "agent", "workspace": workspace, "cancel_event": cancel_event,
                        "activity_streamer": None, "pipeline_ports": ports, "task": task,
                        "plan_text": None, "pinned_paths": list(mentions), "decision_gate": None,
                        "collab_session": None, "agent_provider_id": provider_id,
                        "agent_project_root": Path(proj.root).resolve()})
        bridge = ctx._bridge
        ended = {"flag": False}
        emit_ui = lambda ev: bridge.emit_event("run.event", {"runId": run_id, "ev": ev})
        worker = None
        try:
            worker = _AgentWorker(runtime, run_id, workspace, ports, task, provider_id, cancel_event, mentions)
            if _active.get("run_id") == run_id:
                _active["worker"] = worker
            _wire_agent_worker(worker, runtime=runtime, run_id=run_id, coordinator=coordinator,
                               workspace=workspace, proj=proj, emit_ui=emit_ui, bridge=bridge, ended=ended,
                               slot=slot)
        except Exception as exc:
            if worker is not None and (worker.isRunning() or getattr(worker, "agent_started", False)):
                # Started workers must remain reachable by cancel/shutdown.
                if isinstance(exc, BridgeError):
                    raise
                return {"runId": run_id}
            try:
                coordinator.finish_failed("agent_worker_unavailable")
            except Exception:
                pass
            disposed = _dispose_agent_workspace(workspace)
            slot.worker = None  # this worker never started; no drain is owed
            slot.error_code = "agent_worker_unavailable"
            slot.phase = "failed"
            if disposed:
                slot.workspace = None
            if _active.get("run_id") == run_id and _active.get("worker") is worker and disposed:
                _active.update({"worker": None, "coordinator": None, "run_id": None,
                                "workspace": None, "cancel_event": None, "engine": "legacy",
                                "pipeline_ports": None, "activity_streamer": None})
            raise BridgeError("worker_start_failed", "Tek ajan worker'ı başlatılamadı.") from None
        for bad in invalid_mentions:
            emit_ui({"type": "info", "text": f"Bahsedilen dosya bulunamadı: {bad}"})
        return {"runId": run_id}

    routing = _effective_routing(params)
    mentions, invalid_mentions = _validate_mentions(proj, params.get("mentions") or [])
    runtime = state.get_run_runtime()
    prefs = ui_prefs.load()
    ai_engine_pref = prefs.get("ai_engine", "auto")
    selection = engine_factory.select_engine(proj.root, routing, ai_engine_pref=ai_engine_pref)
    if collab_requested:
        coder = provider_registry.get(routing.get("coder")) if routing.get("coder") else None
        if (selection.engine != "pipeline" or PipelineRunner is None or coder is None or
                coder.get("kind") not in ("openai", "anthropic")):
            raise BridgeError("collab_unsupported", "İşbirliği yalnızca yerel native pipeline kodlayıcısıyla kullanılabilir.")
    # run.created/run.started BURADA, QThread BAŞLAMADAN ÖNCE kalıcı olur.
    # Başarısız olursa (Task/Run kalıcılığı) BridgeError doğal olarak
    # yukarı taşınır — hiçbir yerel worker/host durumu KURULMAMIŞ olur.
    coordinator = LegacyRunCoordinator.start(
        runtime, project_root=proj.root, task=task, routing=routing,
    )
    run_id = coordinator.run_id
    state.set_collaboration_status_cache(None)
    collab_session = None
    if collab_requested:
        try:
            collab_session = state.get_collaboration_host().bind_run(collab_handle, proj.root, run_id)
        except Exception as exc:
            try:
                coordinator.finish_failed("collab_invalid")
            except Exception:
                pass
            code = getattr(exc, "code", None)
            if code == "stale":
                raise BridgeError("collab_stale", "Yerel işbirliği onayı proje veya kaynak sürümüyle eşleşmiyor.") from None
            if code == "busy":
                raise BridgeError("collab_busy", "İşbirliği checkpoint'i başka bir koşu tarafından kullanılıyor.") from None
            raise BridgeError("collab_invalid", "Yerel işbirliği onayı geçersiz veya kullanılamıyor.") from None

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
    _active["decision_gate"] = None
    _active["collab_session"] = collab_session
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
            if status == "failed":
                # A5 (hata UX): tek Türkçe eşleme noktası (bkz. _error_details).
                payload.update(_error_details(coordinator, error))
        bridge.emit_event("run.finished", payload)

    # ---------------- motor seçimi (T1.2) ----------------
    # Use canonical routing for ports. Collaboration additionally pins its
    # safe-point to the native worker boundary; opt-out keeps the legacy call shape.
    selection = engine_factory.select_engine(proj.root, coordinator.routing, ai_engine_pref=ai_engine_pref)
    fallback_reason = selection.reason
    canonical_coder = provider_registry.get(coordinator.routing.get("coder")) if coordinator.routing.get("coder") else None
    if collab_session is not None and (
            selection.engine != "pipeline" or PipelineRunner is None or canonical_coder is None or
            canonical_coder.get("kind") not in ("openai", "anthropic")):
        closed = True
        try:
            collab_session.close()
        except Exception:
            closed = False
            _retain_collaboration(collab_session, None, run_id, terminal_code="run_failed")
        try:
            coordinator.finish_failed("collab_unsupported")
        except Exception:
            pass
        if closed:
            _active["collab_session"] = None
        _active["coordinator"] = None
        _active["run_id"] = None
        raise BridgeError("collab_unsupported", "İşbirliği yalnızca yerel native pipeline kodlayıcısıyla kullanılabilir.")
    pipeline_ports = None
    workspace = None
    # Jev System One karar katmanı (bkz. decision_runtime/, engine_factory.py
    # build_verification_failure_gate): "off" -> None, pipeline/fix-loop
    # davranışı bugünküyle bayt-bayt aynı kalır. Yalnızca yeni motor
    # kullanılıyorsa anlamlıdır -- klasik motorun kendi fix akışı YOKTUR.
    decision_gate = None
    if selection.engine == "pipeline" and PipelineRunner is not None:
        try:
            workspace = engine_factory.create_pipeline_workspace(proj.root, run_id)
            if collab_session is not None:
                pipeline_ports = engine_factory.build_pipeline_ports(
                    runtime, run_id, coordinator.routing, worker_safe_point=collab_session.safe_point)
            else:
                pipeline_ports = engine_factory.build_pipeline_ports(runtime, run_id, coordinator.routing)
            decision_gate = engine_factory.build_verification_failure_gate(
                engine_factory.decision_layer_preference(prefs), runtime=runtime, run_id=run_id,
            )
        except Exception as exc:
            if workspace is not None:
                try:
                    workspace.dispose()
                except Exception:
                    pass
                workspace = None
            pipeline_ports = None
            decision_gate = None
            if collab_session is not None:
                closed = True
                try:
                    collab_session.close()
                except Exception:
                    closed = False
                    _retain_collaboration(collab_session, workspace, run_id, terminal_code="run_failed")
                try:
                    coordinator.finish_failed("collab_unavailable")
                except Exception:
                    pass
                if closed:
                    _active["collab_session"] = None
                _active["coordinator"] = None
                _active["run_id"] = None
                raise BridgeError("collab_unavailable", "Yerel işbirliği pipeline'ı başlatılamadı.") from None
            fallback_reason = f"Yeni motor kullanılamadı ({exc}); klasik motor kullanılıyor."
    _active["pipeline_ports"] = pipeline_ports
    _active["decision_gate"] = decision_gate
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
                ended=ended, bridge=bridge, pinned_paths=mentions, decision_gate=decision_gate,
                collab_session=collab_session,
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
            coordinator.finish_failed(
                "collab_unavailable" if collab_requested else f"Yerel worker başlatılamadı: {exc}")
        except Exception:
            pass
        if workspace is not None:
            try:
                workspace.dispose()
            except Exception:
                pass
        if collab_session is not None:
            closed = True
            try:
                collab_session.close()
            except Exception:
                closed = False
                _retain_collaboration(collab_session, workspace, run_id, terminal_code="run_failed")
        _stop_activity_streamer()
        _active["worker"] = None
        _active["coordinator"] = None
        _active["run_id"] = None
        _active["proposals"] = []
        _active["workspace"] = None
        _active["cancel_event"] = None
        _active["decision_gate"] = None
        if collab_session is None or closed:
            _active["collab_session"] = None
        if collab_requested:
            raise BridgeError("collab_unavailable", "Yerel işbirliği worker'ı başlatılamadı.") from None
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
                         ended, bridge, pinned_paths=None, decision_gate=None, collab_session=None):
    run_id = coordinator.run_id
    cancel_event = threading.Event()
    # F7: this MUST be the same Event instance run.cancel()/shutdown() act
    # on -- previously it was created here but never published to _active,
    # so run.cancel() found _active["cancel_event"] still None and silently
    # cancelled nothing. Stored here (before the QThread starts) so a
    # cancel requested the instant after run.start() returns is never lost.
    _active["cancel_event"] = cancel_event
    proj = _require_project()
    worker = _PipelineWorker(
        runtime, run_id, workspace, ports, task, cancel_event, pinned_paths=pinned_paths,
        decision_gate=decision_gate, collab_session=collab_session,
    )
    _wire_pipeline_worker(
        worker, runtime=runtime, run_id=run_id, coordinator=coordinator, workspace=workspace, proj=proj,
        emit_ui=emit_ui, finish=finish, settle_canonical_failure=settle_canonical_failure,
        ended=ended, bridge=bridge, activity_after_seq=0,
    )
    return worker


def _dispose_agent_workspace(workspace, run_id=None) -> bool:
    """Dispose a quiescent agent workspace; retain host ownership on failure."""
    if workspace is None:
        return True
    try:
        workspace.dispose()
    except Exception:
        return False
    if _active.get("workspace") is workspace and (run_id is None or _active.get("run_id") == run_id):
        _active["workspace"] = None
    return True


def _agent_event_for_execution(runtime, run_id, event_type, execution_id):
    """Find an exact attempt's latest canonical event through a fixed seq bound."""
    through_seq = runtime.get_run(run_id).last_event_seq
    after_seq = 0
    found = None
    while after_seq < through_seq:
        page = runtime.events(run_id, after_seq=after_seq, limit=200)
        if not page.events:
            break
        for event in page.events:
            if event.seq > through_seq:
                break
            if event.type == event_type and event.payload.get("execution_id") == execution_id:
                found = event
        last_seq = page.events[-1].seq
        if last_seq <= after_seq:
            break
        after_seq = last_seq
        if not page.has_more:
            break
    return found


def _wire_agent_worker(worker, *, runtime, run_id, coordinator, workspace, proj, emit_ui, bridge, ended,
                       activity_after_seq=0, slot=None, on_started=None):
    """Bridge the agent core's canonical result without synthesizing pipeline events."""
    def dispose_after_quiescent():
        def dispose():
            if slot is not None:
                if _dispose_agent_workspace(workspace, run_id):
                    slot.workspace = None
                else:
                    slot.phase = "cleanup_failed"
                    slot.error_code = "workspace_cleanup_failed"
            else:
                _dispose_agent_workspace(workspace)
        if worker.isFinished():
            dispose()
        else:
            worker.finished.connect(dispose)
            if worker.isFinished():
                dispose()

    def stage(name, _info):
        emit_ui({"type": "stage", "stage": "code" if name == "working" else "verifying"})

    def finish(status, error=None):
        if ended["flag"]:
            return
        ended["flag"] = True
        if slot is not None:
            streamer = slot.activity_streamer
            if streamer is not None:
                try:
                    streamer.request_stop()
                    streamer.wait(2000)
                except Exception:
                    pass
                if _worker_is_finished(streamer):
                    if slot.activity_streamer is streamer:
                        slot.activity_streamer = None
                else:
                    slot.phase = "cleanup_failed"
                    slot.error_code = "activity_streamer_cleanup_pending"
            if slot.phase != "cleanup_failed":
                try:
                    slot.phase = ("waiting_user" if coordinator.get_run().status is RunStatus.WAITING_USER
                                  else status)
                except Exception:
                    slot.phase = "cleanup_failed"
            if error and slot.phase != "cleanup_failed":
                slot.error_code = error
        else:
            _stop_activity_streamer()
        payload = {"runId": run_id, "status": status, "engine": "agent"}
        if error:
            payload["error"] = error
        bridge.emit_event("run.finished", payload)

    def failed(_message):
        if coordinator.get_run().status == RunStatus.RUNNING:
            coordinator.finish_failed("agent_execution_failed")
        dispose_after_quiescent()
        finish("failed", "agent_execution_failed")

    def complete(result):
        if result.status is AgentExecutionStatus.NEEDS_USER:
            try:
                evidence_event = _agent_event_for_execution(
                    runtime, run_id, RunEventType.PROPOSAL_READY, result.execution_id)
                if evidence_event is None:
                    raise RuntimeError("proposal receipt missing")
                evidence = dict(evidence_event.payload)
                change_set = worker.ports.change_provider.capture(workspace)
                if evidence.get("diff_sha256") != change_set.diff_sha256:
                    evidence["verification"] = dict(evidence.get("verification") or {})
                    if evidence["verification"].get("outcome") == "pass":
                        evidence["verification"]["outcome"] = "invalidated"
                if slot is not None:
                    slot.evidence = evidence
                proposals, skipped = _build_pipeline_proposals(proj, workspace, change_set)
                if slot is not None:
                    slot.proposals = proposals
                    slot.phase = "waiting_user"
                if slot is None or _active.get("run_id") == run_id:
                    _active["proposals"] = proposals
                emit_ui({"type": "diff", "files": list(result.changed_paths)})
                emit_ui({"type": "proposal", "proposals": proposals,
                         "totals": {"latency_s": None, "tokens": None, "cost_usd": None}})
                emit_ui({"type": "evidence", **evidence})
                if evidence.get("agent_message"):
                    emit_ui({"type": "summary", "text": evidence["agent_message"]})
                for path in skipped:
                    emit_ui({"type": "info", "text": f"İkili dosya önerisi gösterilemiyor: {path}"})
            except Exception:
                coordinator.finish_failed("agent_result_processing_failed")
                dispose_after_quiescent()
                finish("failed", "agent_result_processing_failed")
                return
            finish("done")
        elif result.status is AgentExecutionStatus.NO_CHANGES:
            dispose_after_quiescent()
            finish("done")
        elif result.status is AgentExecutionStatus.CANCELLED:
            dispose_after_quiescent()
            finish("cancelled")
        else:
            dispose_after_quiescent()
            finish("failed", "agent_execution_failed")

    worker.stage.connect(stage)
    worker.failed.connect(failed)
    worker.finished_ok.connect(complete)
    if slot is not None:
        slot.worker = worker
        slot.phase = "running"
        def release_worker_reference():
            if slot.worker is worker and _worker_is_finished(worker):
                slot.worker = None
        worker.finished.connect(release_worker_reference)
    worker.start()
    worker.agent_started = True
    if on_started is not None:
        on_started()
    # Activity streaming is optional: it must not invalidate an already-started
    # native worker or sever the host's ownership/cancellation handle.
    try:
        if slot is None:
            _stop_activity_streamer()
        streamer = ActivityStreamer(runtime, run_id, after_seq=activity_after_seq)
        streamer.activity.connect(lambda item: bridge.emit_event("run.activity", {**item, "runId": run_id}))
        if slot is not None:
            slot.activity_streamer = streamer
            def release_streamer_reference():
                if slot.activity_streamer is streamer and _worker_is_finished(streamer):
                    slot.activity_streamer = None
                    if slot.error_code == "activity_streamer_cleanup_pending":
                        slot.error_code = None
                        try:
                            slot.phase = slot.coordinator.get_run().status.value
                        except Exception:
                            slot.phase = "cleanup_failed"
            streamer.finished.connect(release_streamer_reference)
        else:
            _active["activity_streamer"] = streamer
        streamer.start()
        if worker.isFinished():
            streamer.request_stop()
            streamer.wait(2000)
            if _worker_is_finished(streamer):
                if slot is not None and slot.activity_streamer is streamer:
                    slot.activity_streamer = None
                    if slot.error_code == "activity_streamer_cleanup_pending":
                        slot.error_code = None
                elif _active.get("activity_streamer") is streamer:
                    _active["activity_streamer"] = None
            elif slot is not None and slot.activity_streamer is streamer:
                slot.phase = "cleanup_failed"
                slot.error_code = "activity_streamer_cleanup_pending"
    except Exception:
        try:
            if "streamer" in locals():
                streamer.request_stop()
                drained = streamer.wait(1000)
                is_finished = getattr(streamer, "isFinished", None)
                finished = bool(is_finished()) if callable(is_finished) else bool(drained)
                if finished and slot is not None and slot.activity_streamer is streamer:
                    slot.activity_streamer = None
                    if slot.error_code == "activity_streamer_cleanup_pending":
                        slot.error_code = None
                        try:
                            slot.phase = ("waiting_user" if coordinator.get_run().status is RunStatus.WAITING_USER
                                          else "running" if worker.isRunning() else "done")
                        except Exception:
                            slot.phase = "cleanup_failed"
        except Exception:
            pass
        emit_ui({"type": "info", "text": "Canlı etkinlik akışı kullanılamıyor; ajan koşusu devam ediyor."})
        # Admission already succeeded. Return its run ID so the caller can
        # still observe/cancel it; optional telemetry cannot turn it into a
        # failed start with an unreachable UI handle.
        return


def _wire_pipeline_worker(worker, *, runtime, run_id, coordinator, workspace, proj, emit_ui, finish,
                           settle_canonical_failure, ended, bridge, activity_after_seq=0):
    """Shared stage/finished/failed wiring + F1 activity streaming for BOTH
    _PipelineWorker (run.start) and _FollowUpWorker (run.followUp, F2) --
    they share the exact same stage/finished_ok/failed Signal shapes."""
    # A4-A2 (rol başına canlı sayaç): `role_tokens`/`role_cost`/`role_has_usage`
    # o rolün BAŞLADIĞI andan beri BİRİKTİRİLİR (yalnızca yeni bir role_stage'e
    # girerken sıfırlanır) -- `_collect_usage_since_last` her çağrıda YALNIZCA
    # `last_seq`'ten SONRAKİ olayları okur, bu yüzden birikimi BURADA tutmak
    # gerekir (aksi halde her ara "running" tikinde yalnızca son dilimin
    # tokenları görünür, rolün TOPLAMI değil). `role_has_usage`, hiç
    # usage.recorded üretmeyen (ör. ACP/hesap) bir rol için arayüzün "—"
    # gösterebilmesi içindir -- 0 token ile "hiç veri yok" birbirine
    # KARIŞTIRILMAZ.
    worker_session = getattr(worker, "collab_session", None)
    stage_state = {
        "role": None, "start_ts": None, "last_seq": 0,
        "role_tokens": 0, "role_cost": 0.0, "role_has_usage": False,
    }
    metrics_timer = QTimer()
    metrics_timer.setInterval(1000)

    def _collect_usage_since_last() -> tuple[int, float]:
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
                stage_state["role_has_usage"] = True
                total_tokens += int(e.payload.get("total_tokens") or 0)
                cost_usd += float(e.payload.get("cost_usd") or 0.0)
        return total_tokens, cost_usd

    def _emit_role_metric(*, partial: bool) -> None:
        role = stage_state["role"]
        if role is None:
            return
        tokens, cost = _collect_usage_since_last()
        stage_state["role_tokens"] += tokens
        stage_state["role_cost"] += cost
        started = stage_state["start_ts"]
        latency_s = max(0.0, time.monotonic() - started) if started else 0.0
        provider_id = coordinator.routing.get(_ROLE_ROUTING_KEY[role])
        payload = {
            "type": "metric", "stage": _ROLE_LEGACY_STAGE[role], "provider": provider_id,
            "model": provider_id, "latency_s": latency_s,
            # ACP/hesap rotasında hiç usage.recorded olmayabilir -- bu
            # durumda arayüz "—" göstersin diye None (0 DEĞİL) gönderilir.
            "tokens": stage_state["role_tokens"] if stage_state["role_has_usage"] else None,
            "cost_usd": stage_state["role_cost"] if stage_state["role_has_usage"] else None,
        }
        if partial:
            payload["partial"] = True
        emit_ui(payload)

    def flush_role_metrics():
        _emit_role_metric(partial=False)

    def on_metrics_tick():
        if ended["flag"]:
            metrics_timer.stop()
            return
        _emit_role_metric(partial=True)

    metrics_timer.timeout.connect(on_metrics_tick)

    def enter_role_stage(role: str, legacy_stage: str):
        flush_role_metrics()
        stage_state["role"] = role
        stage_state["start_ts"] = time.monotonic()
        stage_state["role_tokens"] = 0
        stage_state["role_cost"] = 0.0
        stage_state["role_has_usage"] = False
        provider_id = coordinator.routing.get(_ROLE_ROUTING_KEY[role])
        emit_ui({"type": "stage", "stage": legacy_stage, "provider": provider_id})
        entry = provider_registry.get(provider_id) if provider_id else None
        if entry and entry.get("kind") == "cli":
            # ACP (hesap) yolunda ara adım/olay akmaz — bkz. run_runtime.acp.
            emit_ui({"type": "info", "text": f"{entry.get('label', provider_id)} hesap oturumu çalışıyor…"})
        if not metrics_timer.isActive():
            metrics_timer.start()

    def on_stage(stage: str, info: dict):
        if ended["flag"]:
            return
        if worker_session is not None and _active.get("run_id") != run_id:
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
            metrics_timer.stop()

    def on_pipeline_failed(msg: str):
        if ended["flag"]:
            return
        if worker_session is not None and _active.get("run_id") != run_id:
            _retire_collaboration_after_worker(worker, worker_session, workspace, run_id, "run_failed")
            return
        metrics_timer.stop()
        settle_canonical_failure(msg)
        if worker_session is not None:
            _retire_collaboration_after_worker(worker, worker_session, workspace, run_id, "run_failed")
        else:
            _dispose_workspace()
        finish("failed", msg)

    def on_pipeline_finished(report):
        if ended["flag"]:
            return
        if worker_session is not None and _active.get("run_id") != run_id:
            code = "run_cancelled" if report.status is PipelineStatus.CANCELLED else "run_finished"
            _retire_collaboration_after_worker(worker, worker_session, workspace, run_id, code)
            return
        # A4-A2: iptal (CANCELLED) "done" aşamasından GEÇMEDEN buraya
        # ulaşabilir -- zamanlayıcı orada durdurulmamış olabilir.
        metrics_timer.stop()
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
            message = "collab_run_failed" if worker_session is not None else f"Sonuç işlenemedi: {exc}"
            settle_canonical_failure(message)
            if worker_session is not None:
                _retire_collaboration_after_worker(worker, worker_session, workspace, run_id, "run_failed")
            else:
                _dispose_workspace()
            finish("failed", message)
            return

        # F2: a fresh Planner attempt's plan becomes the context for any
        # SUBSEQUENT follow-up. A follow-up continuation's own report never
        # carries a plan_report (see PipelineRunner.continue_with_feedback),
        # so this deliberately does NOT clear plan_text then -- the ORIGINAL
        # plan stays available across multiple follow-ups.
        #
        # A4-A3: the FULL plan (steps/acceptance criteria/risks), not just
        # the one-line summary, is stored here -- see _full_plan_text's
        # docstring for why this has to happen at the call site.
        if report.plan_report is not None:
            _active["plan_text"] = _full_plan_text(report.plan_report)

        try:
            still_waiting = coordinator.get_run().status == RunStatus.WAITING_USER
        except Exception:
            still_waiting = False
        if not still_waiting:
            if worker_session is not None:
                code = ("run_cancelled" if report.status is PipelineStatus.CANCELLED else
                        "run_failed" if report.status in (PipelineStatus.FAILED, PipelineStatus.EXHAUSTED) else
                        "run_finished")
                _retire_collaboration_after_worker(worker, worker_session, workspace, run_id, code)
            else:
                _dispose_workspace()

        status = report.status
        if PipelineStatus is not None and status is PipelineStatus.CANCELLED:
            try:
                if coordinator.get_run().status != RunStatus.CANCELLED:
                    coordinator.finish_cancelled()
            except Exception as exc:
                message = "Kanonik iptal sonlandırması başarısız oldu."
                settle_canonical_failure(message)
                finish("failed", message)
                return
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
    _validate_run_rpc_params(params, {"runId", "feedback", "collabApprovalHandle", "mentions"})
    if _delivery_is_busy():
        raise BridgeError("busy", "Teslimat işlemi sürüyor.")
    collab_handle_present = "collabApprovalHandle" in params
    collab_handle = params.get("collabApprovalHandle") if collab_handle_present else None
    if collab_handle_present and (type(collab_handle) is not str or not collab_handle):
        raise BridgeError("collab_invalid", "Yerel işbirliği onayı geçersiz.")
    feedback = (params.get("feedback") or "").strip()
    if not feedback:
        raise BridgeError("empty_feedback", "Takip isteği boş.")

    if _active.get("engine") == "agent" or ("runId" in params and not _legacy_run_id_matches(params)):
        slot = _resolve_agent_slot(params.get("runId"), str(Path(proj.root).resolve()))
        _validate_slot_project(proj, slot)
        if collab_handle_present:
            raise BridgeError("collab_unsupported", "Tek ajan koşuları yerel işbirliğini desteklemiyor.")
        with slot.lock:
            if slot.worker is not None and slot.worker.isRunning():
                raise BridgeError("busy", "Zaten bir koşu sürüyor.")
            try:
                waiting = slot.coordinator.get_run().status is RunStatus.WAITING_USER
            except Exception:
                raise BridgeError("canonical_state_unavailable", "Koşu durumu doğrulanamadı.") from None
            if (slot.phase != "waiting_user" or not slot.proposals or slot.workspace is None or
                    slot.ports is None or not waiting):
                raise BridgeError("no_active_run", "Takip isteği için bekleyen bir öneri yok.")
            mentions, _invalid = _validate_mentions(proj, params.get("mentions") or [])
            slot.pinned_paths = list(dict.fromkeys(slot.pinned_paths + mentions))
            CanonicalPipelineRecorder(state.get_run_runtime(), slot.run_id).resumed(reason="user_feedback")
            slot.proposals = []
            slot.evidence = None
            slot.cancel_event = threading.Event()
            bridge = ctx._bridge
            emit_ui = lambda ev: bridge.emit_event("run.event", {"runId": slot.run_id, "ev": ev})
            worker = None
            try:
                worker = _AgentWorker(state.get_run_runtime(), slot.run_id, slot.workspace, slot.ports,
                                      slot.task, slot.provider_id, slot.cancel_event, slot.pinned_paths)
                worker.feedback = feedback
                _wire_agent_worker(worker, runtime=state.get_run_runtime(), run_id=slot.run_id,
                                   coordinator=slot.coordinator, workspace=slot.workspace, proj=proj,
                                   emit_ui=emit_ui, bridge=bridge, ended={"flag": False},
                                   activity_after_seq=slot.coordinator.get_run().last_event_seq, slot=slot,
                                   on_started=lambda: emit_ui({"type": "followUpStarted", "feedback": feedback}))
            except Exception:
                if worker is not None and (worker.isRunning() or getattr(worker, "agent_started", False)):
                    # A launched continuation remains slot-owned and cancellable.
                    return {"runId": slot.run_id}
                try:
                    slot.coordinator.finish_failed("agent_worker_unavailable")
                except Exception:
                    pass
                slot.worker = None
                slot.phase = "failed"
                slot.error_code = "agent_worker_unavailable"
                if _dispose_agent_workspace(slot.workspace, slot.run_id):
                    slot.workspace = None
                else:
                    slot.phase = "cleanup_failed"
                    slot.error_code = "workspace_cleanup_failed"
                raise BridgeError("worker_start_failed", "Takip worker'ı başlatılamadı.") from None
            return {"runId": slot.run_id}

    if _active.get("engine") != "pipeline":
        if _active.get("engine") == "agent":
            _require_agent_project(proj)
            if collab_handle_present:
                raise BridgeError("collab_unsupported", "Tek ajan koşuları yerel işbirliğini desteklemiyor.")
            worker = _active.get("worker")
            if worker is not None and worker.isRunning():
                raise BridgeError("busy", "Zaten bir koşu sürüyor.")
            coordinator, workspace = _active.get("coordinator"), _active.get("workspace")
            ports = _active.get("pipeline_ports")
            if coordinator is None or workspace is None or ports is None:
                raise BridgeError("no_active_run", "Takip isteği için bekleyen bir öneri yok.")
            if coordinator.get_run().status is not RunStatus.WAITING_USER:
                raise BridgeError("not_waiting_user", "Takip isteği için bekleyen bir öneri yok.")
            mentions, _invalid = _validate_mentions(proj, params.get("mentions") or [])
            pinned = list(dict.fromkeys(list(_active.get("pinned_paths") or []) + mentions))
            runtime = state.get_run_runtime()
            CanonicalPipelineRecorder(runtime, coordinator.run_id).resumed(reason="user_feedback")
            _active["proposals"] = []
            _active["pinned_paths"] = pinned
            cancel_event = threading.Event()
            _active["cancel_event"] = cancel_event
            next_worker = _AgentWorker(runtime, coordinator.run_id, workspace, ports,
                                        _active.get("task") or "", _active["agent_provider_id"],
                                        cancel_event, pinned)
            next_worker.feedback = feedback
            bridge = ctx._bridge
            ended = {"flag": False}
            emit_ui = lambda ev: bridge.emit_event("run.event", {"runId": coordinator.run_id, "ev": ev})
            _active["worker"] = next_worker
            try:
                _wire_agent_worker(next_worker, runtime=runtime, run_id=coordinator.run_id,
                                   coordinator=coordinator, workspace=workspace, proj=proj,
                                   emit_ui=emit_ui, bridge=bridge, ended=ended,
                                   activity_after_seq=coordinator.get_run().last_event_seq)
            except Exception as exc:
                if next_worker.isRunning() or getattr(next_worker, "agent_started", False):
                    if isinstance(exc, BridgeError):
                        raise
                    return {"runId": coordinator.run_id}
                try:
                    coordinator.finish_failed("agent_worker_unavailable")
                except Exception:
                    pass
                _dispose_agent_workspace(workspace)
                if _active.get("worker") is next_worker:
                    _active["worker"] = None
                raise BridgeError("worker_start_failed", "Takip worker'ı başlatılamadı.") from None
            emit_ui({"type": "followUpStarted", "feedback": feedback})
            return {"runId": coordinator.run_id}
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
    collab_session = _active.get("collab_session")
    if collab_session is not None and Path(proj.root).resolve() != collab_session.project_root:
        raise BridgeError("collab_stale", "İşbirliği koşusunun özgün projesi artık seçili değil.")
    if collab_handle_present:
        if collab_session is None:
            raise BridgeError("collab_unsupported", "Bu koşu yerel işbirliği onayıyla başlatılmadı.")
        try:
            collab_session.reapprove(collab_handle)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == "stale":
                raise BridgeError("collab_stale", "Yeni onay özgün işbirliği göreviyle eşleşmiyor.") from None
            if code == "busy":
                raise BridgeError("collab_busy", "İşbirliği koşusu hâlâ etkin.") from None
            raise BridgeError("collab_invalid", "Yeni yerel işbirliği onayı geçersiz.") from None

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
    prior_proposals = list(_active.get("proposals") or [])

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
            if status == "failed":
                payload.update(_error_details(coordinator, error))
        bridge.emit_event("run.finished", payload)

    activity_after_seq = current.last_event_seq
    # F2: AYNI karar kapısı (varsa) devam ettirilir -- decision_layer tercihi
    # bir koşu ORTASINDA değişemez, run.start sırasında bir kez kurulur.
    worker = _FollowUpWorker(
        runtime, run_id, workspace, ports, task, feedback, plan_text, cancel_event,
        pinned_paths=pinned_paths, decision_gate=_active.get("decision_gate"),
        collab_session=collab_session,
    )
    _active["proposals"] = []
    try:
        _wire_pipeline_worker(
            worker, runtime=runtime, run_id=run_id, coordinator=coordinator, workspace=workspace, proj=proj,
            emit_ui=emit_ui, finish=finish, settle_canonical_failure=settle_canonical_failure,
            ended=ended, bridge=bridge, activity_after_seq=activity_after_seq,
        )
    except Exception:
        if not worker.isRunning():
            _active["proposals"] = prior_proposals
        raise BridgeError("collab_unavailable", "Takip worker'ı başlatılamadı.") from None
    _active["worker"] = worker

    emit_ui({"type": "followUpStarted", "feedback": feedback})
    for bad in invalid_mentions:
        emit_ui({"type": "info", "text": f"Bahsedilen dosya bulunamadı: {bad}"})
    return {"runId": run_id}


@handler("run.cancel")
def _cancel(params, ctx):
    _validate_run_rpc_params(params, {"runId"})
    if ("runId" in params and not _legacy_run_id_matches(params)) or _active.get("engine") == "agent":
        root = str(Path(_require_project().root).resolve())
        if "runId" in params:
            slot = _run_registry.get(params["runId"])
            if slot is None:
                raise BridgeError("unknown_run", "Koşu bulunamadı.")
            if slot.project_root != root:
                raise BridgeError("run_project_mismatch", "Koşu başka bir projeye ait.")
        else:
            slot = _resolve_agent_slot(None, root)
        with slot.lock:
            worker = slot.worker
            if worker is not None and worker.isRunning():
                slot.cancel_event.set()
                ctx._bridge.emit_event("run.event", {"runId": slot.run_id,
                    "ev": {"type": "info", "text": "Durduruluyor…"}})
        return {"runId": slot.run_id}
    w = _active.get("worker")
    if w is None or not w.isRunning():
        return {}
    if _active.get("engine") in ("pipeline", "agent"):
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


def _validate_slot_project(proj, slot):
    try:
        valid = Path(proj.root).resolve(strict=True) == Path(slot.project_root).resolve(strict=True)
    except (OSError, RuntimeError):
        valid = False
    if not valid:
        raise BridgeError("run_project_mismatch", "Koşu başka bir projeye ait.")


def _apply_agent(params, ctx, proj, slot):
    _validate_slot_project(proj, slot)
    wanted = params.get("paths") or []
    if not isinstance(wanted, list) or any(not isinstance(p, str) for p in wanted):
        raise BridgeError("invalid_paths", "Öneri yolları geçersiz.")
    with slot.lock, _project_apply_lock(slot.project_root):
        if slot.worker is not None and not _worker_is_finished(slot.worker):
            raise BridgeError("busy", "Ajan worker'ı henüz durmadı.")
        if slot.phase != "waiting_user":
            raise BridgeError("not_waiting_user", "Bu koşuda bekleyen öneri yok.")
        proposals_by_path = {p.get("path"): p for p in slot.proposals if isinstance(p, dict)}
        if any(path not in proposals_by_path for path in wanted):
            raise BridgeError("invalid_paths", "Seçilen yol bu koşunun önerileri arasında değil.")
        proposals = [proposals_by_path[p] for p in dict.fromkeys(wanted)]
        if not proposals:
            return {"applied": [], "errors": [], "conflicts": [], "checkpointId": None}
        try:
            if slot.coordinator.get_run().status != RunStatus.WAITING_USER:
                raise BridgeError("not_waiting_user", "Bu koşuda bekleyen öneri yok.")
        except BridgeError:
            raise
        except Exception:
            raise BridgeError("canonical_state_unavailable", "Koşu durumu doğrulanamadı.") from None
        conflicts = _stale_apply_conflicts(proj, proposals)
        if conflicts:
            return {"applied": [], "errors": [], "conflicts": conflicts, "checkpointId": None}
        try:
            checkpoint = CheckpointStore(proj.root).create(proj, [p["path"] for p in proposals], slot.run_id)
        except Exception as exc:
            raise BridgeError("checkpoint", f"Checkpoint oluşturulamadı: {exc}") from None
        applied, errors = [], []
        for proposal in proposals:
            try:
                if proposal.get("is_deleted"):
                    if proj.exists(proposal["path"]):
                        proj.delete(proposal["path"])
                else:
                    proj.apply(proposal["path"], proposal.get("new", ""), backup=False)
                applied.append(proposal["path"])
            except Exception as exc:
                errors.append({"path": proposal["path"], "message": str(exc)})
        if errors:
            restored, restore_error = _safe_restore_checkpoint(proj, checkpoint["id"])
            if not restored:
                raise BridgeError("apply_rollback_failed", f"Apply geri alınamadı: {restore_error}")
            return {"applied": [], "errors": errors, "conflicts": [], "checkpointId": None}
        try:
            slot.coordinator.record_proposal_applied(applied_paths=applied, checkpoint_id=checkpoint["id"])
        except Exception as exc:
            restored, restore_error = _safe_restore_checkpoint(proj, checkpoint["id"])
            if not restored:
                raise BridgeError("canonical_apply_rollback_failed", f"Geri alma başarısız: {restore_error}")
            raise BridgeError("canonical_apply_failed", f"Dosyalar geri alındı: {exc}") from None
        slot.proposals = []
        slot.phase = "applied"
        slot.checkpoint_id = checkpoint["id"]
        if _active.get("run_id") == slot.run_id:
            _active["proposals"] = []
        if slot.workspace is not None and _worker_is_finished(slot.worker):
            if _dispose_agent_workspace(slot.workspace, slot.run_id):
                slot.workspace = None
            else:
                slot.phase = "cleanup_failed"
                slot.error_code = "workspace_cleanup_failed"
        ctx._bridge.emit_event("fs.changed", {"kind": "modified", "paths": applied})
        return {"applied": applied, "errors": [], "conflicts": [], "checkpointId": checkpoint["id"]}


def _reject_agent(ctx, proj, slot):
    _validate_slot_project(proj, slot)
    with slot.lock:
        if slot.worker is not None and not _worker_is_finished(slot.worker):
            raise BridgeError("busy", "Ajan worker'ı henüz durmadı.")
        if slot.phase != "waiting_user":
            raise BridgeError("not_waiting_user", "Bu koşuda bekleyen öneri yok.")
        if not slot.proposals:
            return {}
        try:
            slot.coordinator.record_proposal_rejected(
                rejected_paths=[p.get("path", "") for p in slot.proposals])
        except Exception as exc:
            raise BridgeError("canonical_reject_failed", f"Kanonik ret kalıcılığı başarısız oldu: {exc}") from None
        slot.proposals = []
        slot.phase = "rejected"
        slot.evidence = None
        if _active.get("run_id") == slot.run_id:
            _active["proposals"] = []
        if slot.workspace is not None and _worker_is_finished(slot.worker):
            if _dispose_agent_workspace(slot.workspace, slot.run_id):
                slot.workspace = None
            else:
                slot.phase = "cleanup_failed"
                slot.error_code = "workspace_cleanup_failed"
        return {}


@handler("run.applyProposals")
def _apply(params, ctx):
    _validate_run_rpc_params(params, {"runId", "paths"})
    proj = _require_project()
    if ("runId" in params and not _legacy_run_id_matches(params)) or _active.get("engine") == "agent":
        return _apply_agent(params, ctx, proj, _resolve_agent_slot(params.get("runId"), str(Path(proj.root).resolve())))
    _require_agent_project(proj)
    session = _active.get("collab_session")
    if _delivery_is_busy(session, _active.get("run_id")):
        raise BridgeError("busy", "Teslimat işlemi sürüyor.")
    worker = _active.get("worker")
    if session is not None:
        if Path(proj.root).resolve() != session.project_root:
            raise BridgeError("collab_stale", "İşbirliği koşusunun özgün projesi artık seçili değil.")
        if worker is not None and worker.isRunning():
            raise BridgeError("busy", "İşbirliği worker'ı henüz durmadı.")
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
    if _active.get("engine") in ("pipeline", "agent"):
        _cleanup_after_collaboration_decision(session, _active.get("run_id"), "run_applied")
    ctx._bridge.emit_event("fs.changed", {"kind": "modified", "paths": applied})
    return {
        "applied": applied,
        "errors": [],
        "conflicts": [],
        "checkpointId": checkpoint["id"],
    }


@handler("run.rejectProposals")
def _reject(params, ctx):
    _validate_run_rpc_params(params, {"runId"})
    proj = _require_project()
    if ("runId" in params and not _legacy_run_id_matches(params)) or _active.get("engine") == "agent":
        return _reject_agent(ctx, proj, _resolve_agent_slot(params.get("runId"), str(Path(proj.root).resolve())))
    _require_agent_project(proj)
    session = _active.get("collab_session")
    if _delivery_is_busy(session, _active.get("run_id")):
        raise BridgeError("busy", "Teslimat işlemi sürüyor.")
    worker = _active.get("worker")
    if session is not None:
        if Path(proj.root).resolve() != session.project_root:
            raise BridgeError("collab_stale", "İşbirliği koşusunun özgün projesi artık seçili değil.")
        if worker is not None and worker.isRunning():
            raise BridgeError("busy", "İşbirliği worker'ı henüz durmadı.")
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
    if _active.get("engine") in ("pipeline", "agent"):
        _cleanup_after_collaboration_decision(session, _active.get("run_id"), "run_rejected")
    return {}


def shutdown():
    """Uygulama kapanırken koşuyu iptal et (zombi thread önleme)."""
    for slot in _run_registry.slots():
        worker = slot.worker
        if worker is not None and worker.isRunning():
            if slot.cancel_event is not None:
                slot.cancel_event.set()
            worker.wait(2000)
        streamer = slot.activity_streamer
        if streamer is not None:
            try:
                streamer.request_stop()
                streamer.wait(2000)
            except Exception:
                pass
            if _worker_is_finished(streamer):
                if slot.activity_streamer is streamer:
                    slot.activity_streamer = None
            else:
                slot.phase = "cleanup_failed"
                slot.error_code = "activity_streamer_cleanup_pending"
        if slot.workspace is not None and _worker_is_finished(worker):
            if _dispose_agent_workspace(slot.workspace, slot.run_id):
                slot.workspace = None
            else:
                slot.phase = "cleanup_failed"
                slot.error_code = "workspace_cleanup_failed"
    w = _active.get("worker")
    session = _active.get("collab_session")
    workspace = _active.get("workspace")
    run_id = _active.get("run_id")
    if w is not None and w.isRunning():
        if _active.get("engine") in ("pipeline", "agent"):
            cancel_event = _active.get("cancel_event")
            if cancel_event is not None:
                cancel_event.set()
        else:
            w.cancel()
        w.wait(2000)
    _stop_activity_streamer()
    if session is not None:
        if not _worker_is_finished(w):
            _retire_collaboration_after_worker(w, session, workspace, run_id, "run_shutdown")
        else:
            _retain_collaboration(session, workspace, run_id, w, terminal_code="run_shutdown")
            _drain_collaboration_resources()
    else:
        if _active.get("engine") == "agent" and not _worker_is_finished(w):
            # Do not remove the worktree while a native agent/process is still
            # using it; cancellation may take longer than the bounded wait.
            w.finished.connect(lambda: _dispose_agent_workspace(workspace))
        elif _active.get("engine") == "agent":
            _dispose_agent_workspace(workspace)
        else:
            _dispose_workspace()
    try:
        state.get_collaboration_host().clear()
    except Exception:
        pass
    # Owner listeners are independently owned; stop only an already-created
    # manager and retain it if draining fails so explicit retry remains possible.
    try:
        from webhost.api.owner import shutdown as shutdown_owner
        shutdown_owner()
    except Exception:
        pass
