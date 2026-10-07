"""Owner-side collaboration RPCs. Credentials cross the bridge only by explicit share."""
from __future__ import annotations

import threading
from pathlib import Path

from webhost import state
from webhost.bridge import BridgeError, handler
from collab_runtime.models import build_context, build_task, safe_id
from collab_runtime.models import MAX_JSON_BYTES, canonical_json_bytes


_SLOTS = threading.BoundedSemaphore(2)
_SHUTDOWN_LOCK = threading.Lock()
_SHUTDOWN_THREAD = None
_ERROR_MESSAGES = {
    "invalid_project": "Açık proje geçerli değil.", "invalid_project_root": "Depo kök dizini geçersiz.",
    "invalid_project_head": "Proje HEAD bilgisi geçersiz.", "invalid_metadata": "Oturum bilgileri geçersiz.",
    "preview_capacity": "Çok fazla bekleyen önizleme var.", "preview_stale": "Önizleme süresi doldu; yeniden deneyin.",
    "unsafe_private_root": "Özel metadata konumu güvenli değil.", "unsafe_private_parent": "Özel metadata üst dizini güvenli değil.",
    "unsafe_private_owner": "Özel metadata sahibi geçersiz.", "unsafe_private_permissions": "Özel metadata izinleri geçersiz.",
    "creation_failed": "Özel metadata oluşturulamadı.", "stop_required": "Önce çalışan oturumu durdurun.",
    "invalid_existing_session": "Mevcut oturum okunamadı.", "invalid_metadata_path": "Metadata yolu geçersiz.",
    "invalid_metadata_paths": "Store ve hub yolları ayrı olmalıdır.", "unsafe_metadata_path": "Metadata yolu kaynak projeyle çakışıyor.",
    "symlink_metadata_path": "Sembolik bağlantılı metadata yolu kabul edilmiyor.",
    "source_head_mismatch": "Kaynak proje HEAD değeri oturumla eşleşmiyor.",
    "invalid_members": "Üye listesi oturumla eşleşmiyor.", "invalid_port": "Port geçersiz.",
    "not_configured": "Önce oturumu yapılandırın.", "start_failed": "Sunucu başlatılamadı.",
    "invalid_certificate": "TLS sertifika dosyası geçersiz.", "invite_failed": "Davet oluşturulamadı.",
    "session_identity_mismatch": "Oturum kimliği metadata ile eşleşmiyor.",
    "cleanup_failed": "Sunucu kapatılamadı; yeniden deneyin.", "not_running_or_member": "Oturum çalışmıyor veya üye geçersiz.",
    "already_shared": "Bu üyeye bu çalışma döneminde zaten erişim verildi.",
    "wrong_project": "Oturum farklı bir kaynak projeye bağlı.",
    "invalid_task_assignment": "Görev bu üyeye atanmadı veya etkin değil.",
    "local_preview_failed": "Yerel önizleme oluşturulamadı.", "stale_local_preview": "Oturum değişti; önizleme atıldı.",
    "epoch_mismatch": "Sahip oturum dönemi değişti.",
    "not_running": "Sahip oturumu çalışmıyor.", "product_stale": "Oturum okuma sırasında değişti; yenileyin.",
    "product_read_failed": "Ortak ürün panosu okunamadı.", "product_write_rejected": "Güncelleme reddedildi; panoyu yenileyip yeniden inceleyin.",
    "product_history_unavailable": "Bu geçmiş başlangıcı kullanılamıyor; güncel panodan yeni bir başlangıç alın.",
    "proposals_read_failed": "Teklif metadata'sı okunamadı.",
    "product_stale_revision": "Ortak ürün revizyonu değişti; panoyu yenileyin.",
    "product_access_denied": "Bu güncelleme için sahip yetkisi gerekli.",
    "product_invalid": "Ortak ürün güncellemesi geçersiz.",
    "task_exists": "Bu görev kimliği zaten kullanılıyor.",
    "task_capacity": "Ortak görev panosu kapasitesine ulaştı.",
}
_GENERIC = ("owner_unavailable", "Yerel sahip oturumu işlemi tamamlanamadı.")


def _params(params, allowed):
    if not isinstance(params, dict) or set(params) - allowed:
        raise BridgeError("owner_invalid", "Sahip oturumu isteği geçersiz.")


def _project():
    project = state.get_project()
    if project is None:
        raise BridgeError("no_project", "Önce bir proje açın.")
    return project


def _owner_error(exc):
    code = getattr(exc, "code", None)
    message = _ERROR_MESSAGES.get(code)
    if message is None:
        raise BridgeError(*_GENERIC) from None
    raise BridgeError("owner_" + code, message) from None


def _submit(ctx, root, generation, operation, *, stale_cleanup=None, stale_read=False):
    if not _SLOTS.acquire(blocking=False):
        raise BridgeError("owner_busy", "Sahip oturumu meşgul; yeniden deneyin.")
    try:
        manager = state.get_owner_manager()
    except Exception:
        _SLOTS.release()
        raise BridgeError(*_GENERIC) from None

    def work():
        try:
            result = operation(manager, root)
            current = state.get_project()
            is_stale = (current is None or current.root != root or
                        state.project_generation() != generation)
            if (stale_cleanup is not None or stale_read) and is_stale:
                if stale_cleanup is not None:
                    try:
                        stale_cleanup(manager, result)
                    except Exception:
                        pass
                ctx.fail("owner_stale", "Proje değişti; işlem eski proje kökünde tamamlandı.")
            else:
                # Create/start/select/stop return the truthful receipt for the captured root.
                ctx.resolve(result)
        except Exception as exc:
            if exc.__class__.__name__ == "OwnerError":
                owner_code = getattr(exc, "code", None)
                message = _ERROR_MESSAGES.get(owner_code)
                ctx.fail("owner_" + owner_code if message is not None else _GENERIC[0],
                         message if message is not None else _GENERIC[1])
            else:
                ctx.fail(*_GENERIC)
        finally:
            _SLOTS.release()

    try:
        threading.Thread(target=work, name="owner-request", daemon=True).start()
    except Exception:
        _SLOTS.release()
        raise BridgeError(*_GENERIC) from None


def _text(params, key, maximum=512):
    value = params.get(key)
    if type(value) is not str or not value or len(value) > maximum:
        raise BridgeError("owner_invalid", "Sahip oturumu isteği geçersiz.")
    return value


def _members(params):
    members = params.get("memberIds")
    if (not isinstance(members, list) or not 1 <= len(members) <= 256 or
            any(type(item) is not str or not item or len(item) > 128 for item in members)):
        raise BridgeError("owner_invalid", "Üye listesi geçersiz.")
    return members


def _tasks(params):
    tasks = params.get("tasks")
    fields = {"id", "owner", "goal", "scopes", "status"}
    if not isinstance(tasks, list) or len(tasks) > 256:
        raise BridgeError("owner_invalid", "Görev listesi geçersiz.")
    for task in tasks:
        if (not isinstance(task, dict) or set(task) - fields or
                not {"id", "owner", "goal", "scopes"} <= set(task) or
                any(type(task.get(k)) is not str or not task[k] or len(task[k]) > 128
                    for k in ("id", "owner")) or
                type(task.get("goal")) is not str or not task["goal"] or len(task["goal"]) > 4096 or
                not isinstance(task.get("scopes"), list) or len(task["scopes"]) > 128 or
                any(type(scope) is not str or not scope or len(scope) > 512 for scope in task["scopes"]) or
                ("status" in task and (type(task["status"]) is not str or len(task["status"]) > 32))):
            raise BridgeError("owner_invalid", "Görev bilgileri geçersiz.")
    return tasks


def _product_identity(params):
    revision = _text(params, "expectedRevision", 40)
    session = _text(params, "expectedSessionId", 128)
    epoch = params.get("expectedEpoch")
    if type(epoch) is not int or epoch < 0:
        raise BridgeError("owner_invalid", "Oturum dönemi geçersiz.")
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise BridgeError("owner_invalid", "Oturum revizyonu geçersiz.")
    try:
        safe_id(session, "session id")
    except Exception:
        raise BridgeError("owner_invalid", "Oturum kimliği geçersiz.") from None
    return revision, epoch, session


@handler("collab.owner.snapshot")
def _product_snapshot(params, ctx):
    _params(params, set())
    project = _project(); root, generation = project.root, state.project_generation()
    _submit(ctx, root, generation, lambda manager, captured: manager.product_snapshot(captured), stale_read=True)
    return None


@handler("collab.owner.updateContext")
def _product_context(params, ctx):
    _params(params, {"confirm", "expectedRevision", "expectedEpoch", "expectedSessionId", "context"})
    if params.get("confirm") is not True:
        raise BridgeError("owner_confirmation_required", "Ortak bağlamı güncellemeyi açıkça onaylayın.")
    revision, epoch, session = _product_identity(params)
    context = params.get("context")
    try:
        if not isinstance(context, dict) or set(context) != {"goal", "decisions", "interfaces"}:
            raise ValueError
        validated_context = build_context(goal=context["goal"], decisions=context["decisions"], interfaces=context["interfaces"])
        if len(canonical_json_bytes({"schema": 1, "session_id": session, "target_version": "x",
                                     "base_commit": "0" * 40, "context": validated_context.to_dict(), "tasks": {}})) > MAX_JSON_BYTES:
            raise ValueError
        context = validated_context.to_dict()
    except Exception:
        raise BridgeError("owner_invalid", "Ortak bağlam geçersiz.")
    project = _project(); root, generation = project.root, state.project_generation()
    _submit(ctx, root, generation, lambda manager, captured: manager.update_product_context(
        captured, context=context, expected_revision=revision, expected_epoch=epoch, expected_session_id=session))
    return None


@handler("collab.owner.updateTaskStatus")
def _product_task(params, ctx):
    _params(params, {"confirm", "expectedRevision", "expectedEpoch", "expectedSessionId", "taskId", "status"})
    if params.get("confirm") is not True:
        raise BridgeError("owner_confirmation_required", "Görev durumunu değiştirmeyi açıkça onaylayın.")
    revision, epoch, session = _product_identity(params)
    task, status = _text(params, "taskId", 128), params.get("status")
    try:
        safe_id(task, "task id")
    except Exception:
        raise BridgeError("owner_invalid", "Görev kimliği geçersiz.") from None
    if type(status) is not str or status not in {"queued", "running", "waiting", "done"}:
        raise BridgeError("owner_invalid", "Görev durumu geçersiz.")
    project = _project(); root, generation = project.root, state.project_generation()
    _submit(ctx, root, generation, lambda manager, captured: manager.update_product_task(
        captured, task_id=task, status=status, expected_revision=revision,
        expected_epoch=epoch, expected_session_id=session))
    return None


@handler("collab.owner.createTask")
def _product_create_task(params, ctx):
    _params(params, {"confirm", "expectedRevision", "expectedEpoch", "expectedSessionId", "task"})
    if params.get("confirm") is not True:
        raise BridgeError("owner_confirmation_required", "Yeni görevi bu pano revizyonunda açıkça onaylayın.")
    revision, epoch, session = _product_identity(params)
    task = params.get("task")
    try:
        if not isinstance(task, dict) or set(task) != {"id", "owner", "goal", "scopes"}:
            raise ValueError
        if type(task["goal"]) is not str or not task["goal"].strip():
            raise ValueError
        if not isinstance(task["scopes"], list) or len(task["scopes"]) > 32:
            raise ValueError
        validated_task = build_task(task_id=task["id"], owner=task["owner"], goal=task["goal"],
                                    scopes=task["scopes"], status="queued", context_revision=revision)
    except Exception:
        raise BridgeError("owner_invalid", "Yeni görev bilgileri geçersiz.") from None
    project = _project(); root, generation = project.root, state.project_generation()
    _submit(ctx, root, generation, lambda manager, captured: manager.create_product_task(
        captured, task_id=validated_task.id, owner=validated_task.owner, goal=validated_task.goal,
        scopes=list(validated_task.scopes),
        expected_revision=revision, expected_epoch=epoch, expected_session_id=session))
    return None


@handler("collab.owner.proposals")
def _product_proposals(params, ctx):
    _params(params, {"expectedSessionId"})
    session = _text(params, "expectedSessionId", 128)
    try:
        safe_id(session, "session id")
    except Exception:
        raise BridgeError("owner_invalid", "Oturum kimliği geçersiz.") from None
    project = _project(); root, generation = project.root, state.project_generation()
    _submit(ctx, root, generation, lambda manager, captured: manager.product_proposals(
        captured, expected_session_id=session), stale_read=True)
    return None


@handler("collab.owner.changes")
def _product_changes(params, ctx):
    _params(params, {"expectedSessionId", "afterRevision", "limit"})
    session = _text(params, "expectedSessionId", 128)
    revision = _text(params, "afterRevision", 40)
    try:
        safe_id(session, "session id")
    except Exception:
        raise BridgeError("owner_invalid", "Oturum kimliği geçersiz.") from None
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise BridgeError("owner_invalid", "Oturum revizyonu geçersiz.")
    limit = params.get("limit", 16)
    if type(limit) is not int or not 1 <= limit <= 16:
        raise BridgeError("owner_invalid", "Geçmiş sayfa boyutu geçersiz.")
    project = _project(); root, generation = project.root, state.project_generation()
    _submit(ctx, root, generation, lambda manager, captured: manager.product_changes(
        captured, expected_session_id=session, after_revision=revision, limit=limit), stale_read=True)
    return None


@handler("collab.owner.previewCreate")
def _preview_create(params, ctx):
    required = {"sessionId", "targetVersion", "goal", "ownerId", "memberIds", "tasks"}
    _params(params, required)
    _text(params, "sessionId", 128); _text(params, "targetVersion", 128)
    _text(params, "goal", 4096); _text(params, "ownerId", 128)
    members, tasks = _members(params), _tasks(params)
    project = _project(); root, generation = project.root, state.project_generation()
    _submit(ctx, root, generation, lambda manager, captured: manager.preview_create(
        captured, session_id=params["sessionId"], target_version=params["targetVersion"],
        goal=params["goal"], owner_id=params["ownerId"], member_ids=members, tasks=tasks),
        stale_cleanup=lambda manager, result: manager.discard_create_preview(result["previewId"]))
    return None


@handler("collab.owner.create")
def _create(params, ctx):
    _params(params, {"previewId"}); preview = _text(params, "previewId", 256)
    project = _project(); root, generation = project.root, state.project_generation()
    _submit(ctx, root, generation, lambda manager, captured: manager.create(preview, captured))
    return None


@handler("collab.owner.select")
def _select(params, ctx):
    _params(params, {"storePath", "hubPath", "ownerId", "memberIds"})
    store, hub = _text(params, "storePath", 4096), _text(params, "hubPath", 4096)
    owner, members = _text(params, "ownerId", 128), _members(params)
    project = _project(); root, generation = project.root, state.project_generation()
    _submit(ctx, root, generation, lambda manager, captured: manager.select_existing(
        captured, store_path=Path(store), hub_path=Path(hub), owner_id=owner, member_ids=members))
    return None


@handler("collab.owner.start")
def _start(params, ctx):
    _params(params, {"port"})
    port = params.get("port", 0)
    if type(port) is not int or not 0 <= port <= 65535:
        raise BridgeError("owner_invalid", "Port geçersiz.")
    project = _project(); root, generation = project.root, state.project_generation()
    _submit(ctx, root, generation, lambda manager, captured: manager.start(captured, port=port))
    return None


@handler("collab.owner.startLAN")
def _start_lan(params, ctx):
    _params(params, {"bindAddress", "certificate", "privateKey", "controlPort", "proposalPort"})
    address = _text(params, "bindAddress", 64)
    certificate = _text(params, "certificate", 4096)
    private_key = _text(params, "privateKey", 4096)
    control_port, proposal_port = params.get("controlPort", 0), params.get("proposalPort", 0)
    if any(type(port) is not int or not 0 <= port <= 65535 for port in (control_port, proposal_port)):
        raise BridgeError("owner_invalid", "Port geçersiz.")
    if control_port and control_port == proposal_port:
        raise BridgeError("owner_invalid", "Kontrol ve öneri portları farklı olmalıdır.")
    project = _project(); root, generation = project.root, state.project_generation()
    _submit(ctx, root, generation, lambda manager, captured: manager.start(
        captured, port=control_port, proposal_port=proposal_port, allow_lan=True,
        bind_address=address, certificate=certificate, private_key=private_key),
        stale_cleanup=lambda manager, result: manager.stop(
            expected_epoch=result["epoch"], expected_project_root=result["projectRoot"]))
    return None


@handler("collab.owner.issueInvite")
def _issue_invite(params, ctx):
    _params(params, {"memberId", "expectedEpoch"})
    member = _text(params, "memberId", 128)
    project = _project(); root, generation = project.root, state.project_generation()
    manager = state.peek_owner_manager()
    if manager is None:
        raise BridgeError("owner_not_running", "Sahip oturumu çalışmıyor.")
    epoch = params.get("expectedEpoch", manager.status()["epoch"])
    if type(epoch) is not int or epoch < 0:
        raise BridgeError("owner_invalid", "Oturum dönemi geçersiz.")
    _submit(ctx, root, generation, lambda m, captured: m.issue_lan_invitation(
        member, project_root=captured, expected_epoch=epoch),
        stale_cleanup=lambda m, result: m.discard_lan_invitation(
            result["code"], project_root=root, expected_epoch=result["epoch"]))
    return None


@handler("collab.owner.revokeMember")
def _revoke_member(params, ctx):
    _params(params, {"memberId", "expectedEpoch"})
    member = _text(params, "memberId", 128)
    project = _project(); root, generation = project.root, state.project_generation()
    manager = state.peek_owner_manager()
    if manager is None:
        raise BridgeError("owner_not_running", "Sahip oturumu çalışmıyor.")
    epoch = params.get("expectedEpoch", manager.status()["epoch"])
    if type(epoch) is not int or epoch < 0:
        raise BridgeError("owner_invalid", "Oturum dönemi geçersiz.")
    _submit(ctx, root, generation, lambda m, captured: m.revoke_lan_member(
        member, project_root=captured, expected_epoch=epoch), stale_read=True)
    return None


@handler("collab.owner.cancelInvite")
def _cancel_invite(params, ctx):
    _params(params, {"code", "projectRoot", "expectedEpoch"})
    code = _text(params, "code", 128)
    root = _text(params, "projectRoot", 4096)
    epoch = params.get("expectedEpoch")
    if type(epoch) is not int or epoch < 0:
        raise BridgeError("owner_invalid", "Oturum dönemi geçersiz.")
    manager = state.peek_owner_manager()
    if manager is not None:
        _submit(ctx, root, state.project_generation(), lambda m, captured: (
            m.discard_lan_invitation(code, project_root=captured, expected_epoch=epoch) or {}))
        return None
    return {}


@handler("collab.owner.stop")
def _stop(params, ctx):
    _params(params, {"expectedEpoch", "expectedProjectRoot"})
    epoch, expected_root = params.get("expectedEpoch"), params.get("expectedProjectRoot")
    if epoch is not None and (type(epoch) is not int or epoch < 0):
        raise BridgeError("owner_invalid", "Oturum dönemi geçersiz.")
    if expected_root is not None:
        expected_root = _text(params, "expectedProjectRoot", 4096)
    manager = state.peek_owner_manager()
    if manager is None:
        return {"state": "unconfigured", "projectRoot": None, "sessionId": None,
                "targetVersion": None, "baseCommit": None, "revision": None, "goal": None,
                "ownerId": None, "memberIds": [], "tasks": [], "storePath": None,
                "hubPath": None, "endpoint": None, "epoch": 0, "exportedMembers": [],
                "retryRequired": False, "createdPaths": []}
    status = manager.status()
    _submit(ctx, status.get("projectRoot"), state.project_generation(), lambda m, _root: (
        m.stop() if epoch is None and expected_root is None else
        m.stop(expected_epoch=epoch, expected_project_root=expected_root)))
    return None


@handler("collab.owner.status")
def _status(params, ctx):
    _params(params, set())
    manager = state.peek_owner_manager()
    if manager is None:
        return {"state": "unconfigured", "projectRoot": None, "sessionId": None,
                "targetVersion": None, "baseCommit": None, "revision": None, "goal": None,
                "ownerId": None, "memberIds": [], "tasks": [], "storePath": None,
                "hubPath": None, "endpoint": None, "epoch": 0, "exportedMembers": [],
                "retryRequired": False, "createdPaths": []}
    return manager.status()


@handler("collab.owner.shareOnce")
def _share_once(params, ctx):
    _params(params, {"memberId", "confirmSecret"})
    member = _text(params, "memberId", 128)
    if params.get("confirmSecret") is not True:
        raise BridgeError("owner_confirmation_required", "Erişim bilgisini paylaşmayı açıkça onaylayın.")
    project = _project()
    manager = state.peek_owner_manager()
    if manager is None or Path(manager.status().get("projectRoot") or ".").resolve() != Path(project.root).resolve():
        raise BridgeError("owner_wrong_project", "Sahip oturumu bu projeye bağlı değil.")
    try:
        return manager.reveal_member_once(member)
    except Exception as exc:
        if exc.__class__.__name__ == "OwnerError": _owner_error(exc)
        raise BridgeError(*_GENERIC) from None


@handler("collab.owner.localPreview")
def _local_preview(params, ctx):
    _params(params, {"memberId", "taskId"})
    member, task = _text(params, "memberId", 128), _text(params, "taskId", 128)
    project = _project(); root, generation = project.root, state.project_generation()
    manager = state.peek_owner_manager()
    if manager is None or Path(manager.status().get("projectRoot") or ".").resolve() != Path(root).resolve():
        raise BridgeError("owner_wrong_project", "Sahip oturumu bu projeye bağlı değil.")
    def operation(owner_manager, captured):
        host = state.get_collaboration_host()
        result = owner_manager.preview_local_collaboration(host, captured, member_id=member, task_id=task)
        return {"preview": result["preview"], "storePath": result["storePath"],
                "hubPath": result["hubPath"], "endpoint": result["endpoint"],
                "memberId": result["memberId"], "taskId": result["taskId"], "epoch": result["epoch"]}
    _submit(ctx, root, generation, operation,
            stale_cleanup=lambda _manager, result: state.get_collaboration_host().drop_preview(
                result["preview"]["previewId"]))
    return None


def shutdown():
    """Start at most one owned shutdown worker; never block app close on drain."""
    global _SHUTDOWN_THREAD
    manager = state.peek_owner_manager()
    if manager is None or manager.status().get("state") not in {"running", "starting", "stopping", "cleanup_failed"}:
        return
    with _SHUTDOWN_LOCK:
        if _SHUTDOWN_THREAD is not None and _SHUTDOWN_THREAD.is_alive():
            return
        def stop_owned():
            try:
                manager.stop()
            except Exception:
                pass  # manager retains endpoint/tokens in cleanup_failed for explicit retry
        _SHUTDOWN_THREAD = threading.Thread(target=stop_owned, name="owner-shutdown", daemon=True)
        try:
            _SHUTDOWN_THREAD.start()
        except Exception:
            _SHUTDOWN_THREAD = None
