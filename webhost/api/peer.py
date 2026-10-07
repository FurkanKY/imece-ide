"""Participant metadata bridge. No bearer credential crosses this boundary."""
from __future__ import annotations

import threading
from pathlib import Path

from webhost import state
from webhost.bridge import BridgeError, handler

_SLOTS = threading.BoundedSemaphore(2)
_GENERIC = ("peer_unavailable", "Participant oturumu işlemi tamamlanamadı.")


def _project():
    project = state.get_project()
    if project is None:
        raise BridgeError("no_project", "Önce bir proje açın.")
    return project


def _error(exc):
    text = str(exc)
    if "peer_publish_uncertain" in text:
        return ("peer_publish_uncertain", "Öneri paylaşılmış olabilir. Aynı öneri kimliğini uzlaştırın; yeni kimlikle kör yeniden paylaşmayın.")
    if "peer_pairing_uncertain" in text:
        return ("peer_pairing_uncertain", "Eşleştirme sahibine ulaşmış olabilir; sahibinden daveti iptal edip yenisini oluşturmasını isteyin.")
    if "peer_cleanup_uncertain" in text:
        return ("peer_cleanup_uncertain", "Katılım yetkisinin temizlendiği doğrulanamadı; sahibinden erişimi iptal etmesini isteyin.")
    if "fingerprint" in text or "confirm" in text:
        return ("peer_pin_confirmation", "Eşleştirmeden önce parmak izini ayrı güvenlik kanalından doğrulayın.")
    return ("peer_operation_failed", "Participant işlemi tamamlanamadı; sahibinden erişimi iptal etmesini isteyin.")


def _submit(ctx, root, generation, operation, *, handle=None):
    if not _SLOTS.acquire(blocking=False):
        raise BridgeError("peer_busy", "Participant oturumu meşgul; yeniden deneyin.")
    try:
        manager = state.get_peer_manager()
    except Exception:
        _SLOTS.release()
        raise BridgeError(*_GENERIC) from None

    def work():
        try:
            current = state.get_project()
            if (current is None or str(Path(current.root).resolve()) != root or
                    state.project_generation() != generation):
                ctx.fail("peer_stale", "Proje değişti; işlem başlatılmadı.")
                return
            result = operation(manager)
            current = state.get_project()
            stale = (current is None or str(Path(current.root).resolve()) != root or
                     state.project_generation() != generation)
            if stale:
                # A stale join can only discard the receipt it just created, never a replacement.
                cleanup_warning = None
                if isinstance(result, dict) and result.get("peerHandle"):
                    try:
                        cleanup_warning = manager.disconnect(handle=result["peerHandle"]).get("warning")
                    except Exception:
                        cleanup_warning = "Katılım yetkisini yerel olarak unutun ve sahibinden erişimi iptal etmesini isteyin."
                message = ("Proje değişti; katılım sonucu atıldı. Sahibinden erişimi iptal etmesini isteyin."
                           if cleanup_warning else "Proje değişti; katılım sonucu güvenli biçimde atıldı.")
                ctx.fail("peer_stale", message)
            else:
                ctx.resolve(result)
        except Exception as exc:
            code, message = _error(exc)
            ctx.fail(code, message)
        finally:
            _SLOTS.release()

    try:
        threading.Thread(target=work, name="peer-request", daemon=True).start()
    except Exception:
        _SLOTS.release()
        raise BridgeError(*_GENERIC) from None


def _validate(params, allowed):
    if not isinstance(params, dict) or set(params) - allowed:
        raise BridgeError("peer_invalid", "Participant isteği geçersiz.")


def _handle(params):
    value = params.get("peerHandle")
    if type(value) is not str or len(value) != 32:
        raise BridgeError("peer_invalid", "Participant oturum tanıtıcısı geçersiz.")
    return value


@handler("collab.peer.join")
def _join(params, ctx):
    _validate(params, {"bundle", "confirmPin", "pin"})
    if params.get("confirmPin") is not True or not isinstance(params.get("bundle"), dict) or type(params.get("pin")) is not str:
        raise BridgeError("peer_invalid", "Davet ve parmak izi onayı gereklidir.")
    project = _project(); root = str(Path(project.root).resolve()); generation = state.project_generation()
    _submit(ctx, root, generation, lambda manager: manager.join(root, generation, params["bundle"],
                                                                confirm_pin=params["confirmPin"], pin=params["pin"]))
    return None


@handler("collab.peer.refresh")
def _refresh(params, ctx):
    _validate(params, {"peerHandle"}); handle = _handle(params)
    project = _project(); root = str(Path(project.root).resolve()); generation = state.project_generation()
    _submit(ctx, root, generation, lambda manager: manager.refresh(root, generation, handle))
    return None


@handler("collab.peer.status")
def _status(params, ctx):
    _validate(params, {"peerHandle"}); handle = _handle(params)
    project = _project(); root = str(Path(project.root).resolve()); generation = state.project_generation()
    _submit(ctx, root, generation, lambda manager: manager.status(root, generation, handle))
    return None


@handler("collab.peer.disconnect")
def _disconnect(params, ctx):
    _validate(params, {"peerHandle"}); handle = _handle(params)
    project = _project(); root = str(Path(project.root).resolve()); generation = state.project_generation()
    _submit(ctx, root, generation, lambda manager: manager.disconnect(root, generation, handle))
    return None


def _delivery_identity(params, allowed):
    _validate(params, allowed | {"peerHandle"})
    handle = _handle(params)
    project = _project()
    return str(Path(project.root).resolve()), state.project_generation(), handle


def _native_source(root, source_run_id):
    from webhost.api import run
    from change_runtime.git import GitWorktreeChangeProvider
    from collab_runtime.store import GitStore
    slot = run._run_registry.get(source_run_id)
    if slot is None or slot.project_root != root or slot.workspace is None:
        raise BridgeError("peer_invalid", "Yerel sonuç bu projede sahiplenilmemiş.")
    sequence = slot.coordinator.get_run().last_event_seq
    expected_diff = (slot.evidence or {}).get("diff_sha256")
    def guard():
        with slot.lock:
            snapshot = slot.workspace.snapshot
            if (slot.phase != "waiting_user" or not slot.proposals or
                    slot.worker is not None and slot.worker.isRunning() or
                    slot.coordinator.get_run().last_event_seq != sequence or
                    snapshot.project_relative_root != Path(".") or
                    snapshot.snapshot_commit != snapshot.source_head or
                    snapshot.source_head != GitStore.project_head(Path(root)) or
                    not expected_diff or GitWorktreeChangeProvider().capture(slot.workspace).diff_sha256 != expected_diff):
                raise ValueError("Native result changed or is not quiescent")
    guard()
    return slot.workspace.root, guard


@handler("collab.peer.previewProposal")
def _preview_proposal(params, ctx):
    root, generation, handle = _delivery_identity(params, {"taskId", "paths", "sourceRunId"})
    task, paths, run_id = params.get("taskId"), params.get("paths"), params.get("sourceRunId")
    if (type(task) is not str or not task or len(task) > 128 or
            not isinstance(paths, list) or not 1 <= len(paths) <= 64 or
            any(type(path) is not str or not path or len(path) > 512 for path in paths) or
            run_id is not None and (type(run_id) is not str or not run_id or len(run_id) > 128)):
        raise BridgeError("peer_invalid", "Görev, açık dosya seçimi ve yerel sonuç geçersiz.")
    def operation(manager):
        source, guard = _native_source(root, run_id) if run_id else (Path(root), None)
        return manager.delivery.preview(root, generation, handle, task_id=task, paths=paths,
            source_root=source, source_guard=guard, source_run_id=run_id)
    _submit(ctx, root, generation, operation)
    return None


@handler("collab.peer.publishProposal")
def _publish_proposal(params, ctx):
    root, generation, handle = _delivery_identity(params, {"ticketId", "confirm", "allowOutOfScope"})
    ticket = params.get("ticketId")
    if (type(ticket) is not str or len(ticket) != 32 or params.get("confirm") is not True or
            type(params.get("allowOutOfScope", False)) is not bool):
        raise BridgeError("peer_invalid", "Öneri önizlemesi ve açık paylaşım onayı gereklidir.")
    _submit(ctx, root, generation, lambda manager: manager.delivery.publish(root, generation, handle,
        ticket, confirm=True, allow_out_of_scope=params.get("allowOutOfScope", False)))
    return None


@handler("collab.peer.reconcileProposal")
def _reconcile_proposal(params, ctx):
    root, generation, handle = _delivery_identity(params, {"ticketId"})
    ticket = params.get("ticketId")
    if type(ticket) is not str or len(ticket) != 32:
        raise BridgeError("peer_invalid", "Öneri önizlemesi geçersiz.")
    _submit(ctx, root, generation, lambda manager: manager.delivery.reconcile(root, generation, handle, ticket))
    return None


@handler("collab.peer.discardProposal")
def _discard_proposal(params, ctx):
    root, generation, handle = _delivery_identity(params, {"ticketId"})
    ticket = params.get("ticketId")
    if type(ticket) is not str or len(ticket) != 32:
        raise BridgeError("peer_invalid", "Öneri önizlemesi geçersiz.")
    _submit(ctx, root, generation, lambda manager: manager.delivery.discard(root, generation, handle, ticket))
    return None


@handler("collab.peer.fetchProposal")
def _fetch_proposal(params, ctx):
    root, generation, handle = _delivery_identity(params, {"proposalId"})
    proposal_id = params.get("proposalId")
    if type(proposal_id) is not str or not proposal_id or len(proposal_id) > 128:
        raise BridgeError("peer_invalid", "Öneri kimliği geçersiz.")
    _submit(ctx, root, generation, lambda manager: manager.delivery.fetch(root, generation, handle, proposal_id))
    return None


def shutdown():
    """Synchronously close admission/detach, with leave delegated to bounded cleanup."""
    manager = state.peek_peer_manager()
    if manager is not None:
        try:
            manager.shutdown()
        except Exception:
            pass
