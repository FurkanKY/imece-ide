"""Explicit local-only bridge for sharing native run proposals."""
from __future__ import annotations

import threading
from pathlib import Path

from webhost import state
from webhost.bridge import BridgeError, handler
from webhost.api import run as run_api

_SLOTS = threading.BoundedSemaphore(2)
_MESSAGES = {
    "invalid": ("delivery_invalid", "Teslimat isteği geçersiz."),
    "stale": ("delivery_stale", "Proje veya işbirliği bağlamı değişti."),
    "busy": ("delivery_busy", "Yerel koşu meşgul; daha sonra yeniden deneyin."),
    "unavailable": ("delivery_unavailable", "Yerel teslimat kaynağı kullanılamıyor."),
    "expired": ("delivery_expired", "Önizleme süresi doldu veya artık kullanılamıyor."),
    "conflict": ("delivery_conflict", "Seçilen öneriler çakışıyor."),
}


def _invalid():
    raise BridgeError(*_MESSAGES["invalid"])


def _params(params, allowed, required):
    if not isinstance(params, dict) or set(params) - allowed or not required <= set(params):
        _invalid()


def _text(value):
    return type(value) is str and bool(value) and len(value) <= 4096 and "\x00" not in value


def _assert_current(borrowed):
    current = state.get_project()
    try:
        same_root = (current is not None and
                     Path(current.root).resolve(strict=True) == borrowed["project_root"])
    except (OSError, RuntimeError, TypeError, ValueError):
        same_root = False
    if not same_root or state.project_generation() != borrowed["generation"]:
        raise RuntimeError("stale")


def _async(ctx, run_id, operation, *, mode="normal"):
    if not _SLOTS.acquire(blocking=False):
        raise BridgeError(*_MESSAGES["busy"])
    project = state.get_project()
    if project is None:
        _SLOTS.release()
        raise BridgeError("no_project", "Önce bir proje açın.")
    root, generation = project.root, state.project_generation()

    def work():
        try:
            with run_api.borrow_delivery_context(run_id) as borrowed:
                if borrowed["project_root"] != Path(root).resolve(strict=True):
                    raise RuntimeError("stale")
                service = state.get_delivery_service()
                result = operation(service, borrowed)
                current = state.get_project()
                still_current = (current is not None and current.root == root and
                                 state.project_generation() == generation)
                if not still_current and mode == "preview":
                    service.discard_publication(result["previewId"])
                    raise RuntimeError("stale")
            ctx.resolve(result)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if exc.__class__.__name__ == "DeliveryConflictError":
                ctx.resolve({"candidate": None, "conflicts": list(getattr(exc, "conflicts", ()))})
                return
            if code in _MESSAGES:
                wire, message = _MESSAGES[code]
            elif isinstance(exc, RuntimeError) and str(exc) in ("invalid", "stale", "busy"):
                wire, message = _MESSAGES[str(exc)]
            else:
                wire, message = _MESSAGES["unavailable"]
            ctx.fail(wire, message)
        finally:
            _SLOTS.release()

    try:
        threading.Thread(target=work, name="delivery-request", daemon=True).start()
    except Exception:
        _SLOTS.release()
        raise BridgeError(*_MESSAGES["unavailable"]) from None


@handler("collab.delivery.preview")
def _preview(params, ctx):
    required = {"runId", "storePath", "hubPath", "paths"}
    _params(params, required | {"proposalId"}, required)
    if (not all(_text(params.get(key)) for key in ("runId", "storePath", "hubPath")) or
            not isinstance(params["paths"], list) or not params["paths"] or
            len(params["paths"]) > 64 or any(not _text(p) for p in params["paths"]) or
            ("proposalId" in params and not _text(params["proposalId"]))):
        _invalid()
    def operation(service, b):
        if not set(params["paths"]) <= set(b.get("available_paths", ())):
            raise RuntimeError("invalid")
        return service.preview_publication(b["session"], b["workspace"],
            store_path=params["storePath"], hub_path=params["hubPath"], paths=params["paths"],
            proposal_id=params.get("proposalId"))
    _async(ctx, params["runId"], operation, mode="preview")
    return None


@handler("collab.delivery.publish")
def _publish(params, ctx):
    _params(params, {"runId", "previewId", "allowOutOfScope"}, {"runId", "previewId"})
    allow = params.get("allowOutOfScope", False)
    if not _text(params.get("runId")) or not _text(params.get("previewId")) or type(allow) is not bool:
        _invalid()
    def operation(service, b):
        _assert_current(b)
        return service.confirm_publication(params["previewId"], run_id=params["runId"],
            project_root=b["project_root"], allow_out_of_scope=allow)
    _async(ctx, params["runId"], operation)
    return None


@handler("collab.delivery.discard")
def _discard(params, ctx):
    _params(params, {"previewId"}, {"previewId"})
    if not _text(params.get("previewId")):
        _invalid()
    state.get_delivery_service().discard_publication(params["previewId"])
    return {}


@handler("collab.delivery.list")
def _list(params, ctx):
    required = {"runId", "storePath", "hubPath"}
    _params(params, required, required)
    if not all(_text(params.get(key)) for key in required):
        _invalid()
    _async(ctx, params["runId"], lambda service, b: {"proposals": service.list_shared_proposals(
        store_path=params["storePath"], hub_path=params["hubPath"], project_root=b["project_root"],
        binding=b["binding"])})
    return None


@handler("collab.delivery.candidate")
def _candidate(params, ctx):
    required = {"runId", "storePath", "hubPath", "proposalIds", "outputPath"}
    _params(params, required | {"verify"}, required)
    verify = params.get("verify", False)
    ids = params.get("proposalIds")
    if (not all(_text(params.get(key)) for key in required - {"proposalIds"}) or
            type(verify) is not bool or not isinstance(ids, list) or not 1 <= len(ids) <= 16 or
            any(not _text(item) for item in ids) or len(set(ids)) != len(ids)):
        _invalid()
    def operation(service, b):
        _assert_current(b)
        receipt = service.assemble_shared_candidate(project_root=b["project_root"],
            store_path=params["storePath"], hub_path=params["hubPath"], binding=b["binding"],
            proposal_ids=ids, output_path=params["outputPath"], verify=verify)
        return {"candidate": receipt, "conflicts": []}
    _async(ctx, params["runId"], operation)
    return None
