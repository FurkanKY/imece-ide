"""Local collaboration approval bridge. Secrets remain in host memory only."""
from __future__ import annotations

import threading
from pathlib import Path

from webhost import state
from webhost.bridge import BridgeError, handler


_SLOTS = threading.BoundedSemaphore(2)
_ERRORS = {
    "invalid": ("collab_invalid", "İşbirliği onayı geçersiz veya kullanılamıyor."),
    "stale": ("collab_stale", "Proje veya işbirliği oturumu değişti; yeniden önizleyin."),
    "busy": ("collab_busy", "İşbirliği işlemi meşgul; daha sonra yeniden deneyin."),
    "storage": ("collab_unavailable", "Yerel işbirliği kaynağı kullanılamıyor."),
}
_PARTICIPANT_UNKNOWN = (
    "collab_task_outcome_unknown",
    "Görev durumu yazımı tamamlanamadı; değişiklik YAPILMIŞ OLABİLİR. Otomatik yeniden deneme yapılmadı. Yeni bir önizleme alıp durumu uzlaştırın.",
)


def _params(params, allowed):
    if not isinstance(params, dict) or set(params) - allowed:
        raise BridgeError("collab_invalid", "İşbirliği isteği geçersiz.")


def _project():
    project = state.get_project()
    if project is None:
        raise BridgeError("no_project", "Önce bir proje açın.")
    return project


def _raise_host(exc):
    code, message = _ERRORS.get(getattr(exc, "code", None), _ERRORS["invalid"])
    raise BridgeError(code, message) from None


def _submit(ctx, project, operation):
    if not _SLOTS.acquire(blocking=False):
        raise BridgeError("collab_busy", "İşbirliği işlemi meşgul; daha sonra yeniden deneyin.")
    root, generation = project.root, state.project_generation()
    try:
        host = state.get_collaboration_host()
    except Exception:
        _SLOTS.release()
        raise BridgeError("collab_unavailable", "Yerel işbirliği isteği başlatılamadı.") from None

    def work():
        try:
            result = operation(host, root)
            current = state.get_project()
            if (current is None or current.root != root or
                    state.project_generation() != generation):
                # Do not leave a valid approval for a project the user no longer views.
                if isinstance(result, dict) and isinstance(result.get("previewId"), str):
                    host.drop_preview(result["previewId"])
                elif isinstance(result, dict) and isinstance(result.get("approvalHandle"), str):
                    try:
                        host.release(result["approvalHandle"])
                    except Exception:
                        pass
                ctx.fail("collab_stale", "Proje değişti; işbirliği işlemini yeniden deneyin.")
            else:
                ctx.resolve(result)
        except Exception as exc:
            if exc.__class__.__name__ == "HostCollaborationError":
                code, message = _ERRORS.get(getattr(exc, "code", None), _ERRORS["invalid"])
                ctx.fail(code, message)
            else:
                ctx.fail("collab_unavailable", "Yerel işbirliği isteği tamamlanamadı.")
        finally:
            _SLOTS.release()

    try:
        threading.Thread(target=work, name="collab-request", daemon=True).start()
    except Exception:
        _SLOTS.release()
        raise BridgeError("collab_unavailable", "Yerel işbirliği isteği başlatılamadı.") from None


@handler("collab.preview")
def _preview(params, ctx):
    required = {"endpoint", "credential", "memberId", "taskId"}
    _params(params, required)
    if any(type(params.get(k)) is not str or not params[k] for k in required):
        raise BridgeError("collab_invalid", "İşbirliği isteği geçersiz.")
    project = _project()
    _submit(ctx, project, lambda host, root: host.preview(
        root, params["endpoint"], params["credential"], params["memberId"], params["taskId"]))
    return None


@handler("collab.approve")
def _approve(params, ctx):
    _params(params, {"previewId", "resetCursor"})
    if (type(params.get("previewId")) is not str or not params["previewId"] or
            ("resetCursor" in params and type(params["resetCursor"]) is not bool)):
        raise BridgeError("collab_invalid", "İşbirliği isteği geçersiz.")
    project = _project()
    preview_id, reset = params["previewId"], params.get("resetCursor", False)
    _submit(ctx, project, lambda host, root: host.approve(preview_id, root, reset_cursor=reset))
    return None


@handler("collab.discard")
def _discard(params, ctx):
    _params(params, {"previewId", "approvalHandle"})
    preview, handle = params.get("previewId"), params.get("approvalHandle")
    if (preview is not None and type(preview) is not str or
            handle is not None and type(handle) is not str or
            preview is not None and handle is not None):
        raise BridgeError("collab_invalid", "İşbirliği isteği geçersiz.")
    host = state.get_collaboration_host()
    if preview:
        host.drop_preview(preview)
    elif handle:
        try:
            host.release(handle)
        except Exception:
            pass
    return {}


@handler("collab.status")
def _status(params, ctx):
    _params(params, {"runId"})
    run_id = params.get("runId")
    if run_id is not None and type(run_id) is not str:
        raise BridgeError("collab_invalid", "İşbirliği isteği geçersiz.")
    from webhost.api.run import get_collaboration_status
    return {"collaboration": get_collaboration_status(run_id)}


def _participant_error(ctx, exc, *, uncertain=False):
    if uncertain or getattr(exc, "outcome_uncertain", False):
        ctx.fail(*_PARTICIPANT_UNKNOWN)
        return
    code = getattr(exc, "code", None)
    if code in {"expired", "stale_revision"}:
        ctx.fail("collab_stale", "Görev durumu önizlemesi artık güncel değil; yeni bir önizleme alın.")
        return
    if isinstance(exc, RuntimeError) and str(exc) in {"busy", "stale", "invalid"}:
        code = str(exc)
    known = {
        **_ERRORS,
        "invalid": ("collab_invalid", "Katılımcı görev durumu isteği geçersiz veya kullanılamıyor."),
    }
    code, message = known.get(code, ("collab_unavailable", "Katılımcı görev durumu isteği tamamlanamadı."))
    ctx.fail(code, message)


def _participant_submit(ctx, run_id, operation, *, preview=False, confirming=False):
    from webhost.api import run as run_api
    project = _project()
    try:
        root = Path(project.root).resolve(strict=True)
    except Exception:
        raise BridgeError("collab_invalid", "Katılımcı görev durumu projesi kullanılamıyor.") from None
    generation = state.project_generation()
    if not _SLOTS.acquire(blocking=False):
        raise BridgeError("collab_busy", "İşbirliği işlemi meşgul; daha sonra yeniden deneyin.")

    def work():
        ticket = None
        started = False
        try:
            with run_api.borrow_participant_context(run_id) as borrowed:
                if (Path(borrowed["project_root"]).resolve(strict=True) != root or
                        borrowed["generation"] != generation):
                    raise RuntimeError("stale")
                session = borrowed["session"]
                if (session.run_id != run_id or
                        Path(session.project_root).resolve(strict=True) != root):
                    raise RuntimeError("stale")
                if confirming:
                    started = True
                result = operation(session)
                if preview:
                    ticket = result.get("ticketId") if isinstance(result, dict) else None
                    current = state.get_project()
                    if (current is None or Path(current.root).resolve(strict=True) != root or
                            state.project_generation() != generation):
                        if isinstance(ticket, str):
                            session.discard_task_status(ticket)
                        ctx.fail("collab_stale", "Proje değişti; önizleme geçersiz kılındı. Yeniden önizleyin.")
                        return
                # A committed status receipt remains truthful across a later project switch.
                ctx.resolve(result)
        except Exception as exc:
            _participant_error(ctx, exc, uncertain=bool(confirming and started and
                                                        exc.__class__.__name__ != "ParticipantCommandError"))
        finally:
            _SLOTS.release()

    try:
        threading.Thread(target=work, name="collab-participant-status", daemon=True).start()
    except Exception:
        _SLOTS.release()
        raise BridgeError("collab_unavailable", "Katılımcı görev durumu isteği başlatılamadı.") from None


@handler("collab.taskStatus.preview")
def _participant_preview(params, ctx):
    _params(params, {"runId", "targetStatus"})
    run_id, target = params.get("runId"), params.get("targetStatus")
    if (type(run_id) is not str or not 0 < len(run_id) <= 256 or
            type(target) is not str or target not in {"queued", "running", "waiting"}):
        raise BridgeError("collab_invalid", "Katılımcı görev durumu isteği geçersiz.")
    _participant_submit(ctx, run_id, lambda session: session.preview_task_status(target), preview=True)
    return None


@handler("collab.taskStatus.confirm")
def _participant_confirm(params, ctx):
    _params(params, {"runId", "ticketId", "confirm"})
    run_id, ticket = params.get("runId"), params.get("ticketId")
    if (type(run_id) is not str or not 0 < len(run_id) <= 256 or
            type(ticket) is not str or not 0 < len(ticket) <= 256 or
            type(params.get("confirm")) is not bool or params["confirm"] is not True):
        raise BridgeError("collab_invalid", "Katılımcı görev durumu isteği geçersiz.")
    _participant_submit(ctx, run_id, lambda session: session.confirm_task_status(ticket), confirming=True)
    return None


@handler("collab.taskStatus.discard")
def _participant_discard(params, ctx):
    _params(params, {"runId", "ticketId"})
    run_id, ticket = params.get("runId"), params.get("ticketId")
    if (type(run_id) is not str or not 0 < len(run_id) <= 256 or
            type(ticket) is not str or not 0 < len(ticket) <= 256):
        raise BridgeError("collab_invalid", "Katılımcı görev durumu isteği geçersiz.")
    from webhost.api import run as run_api
    # Closing/expired runs have already dropped their host-owned tickets.
    if _active_session_matches(run_id):
        try:
            with run_api.borrow_participant_context(run_id) as borrowed:
                borrowed["session"].discard_task_status(ticket)
        except Exception:
            pass
    return {}


def _active_session_matches(run_id):
    from webhost.api import run as run_api
    session = run_api._active.get("collab_session")
    return session is not None and run_api._active.get("run_id") == run_id and session.run_id == run_id
