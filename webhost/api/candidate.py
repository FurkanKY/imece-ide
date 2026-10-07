"""Explicit native-task candidate assembly, verified integration and rollback."""
from contextlib import ExitStack
from pathlib import Path
import threading

from change_runtime.candidate import CombinedCandidates
from runtime_paths import workspaces_dir
from webhost import state
from webhost.bridge import handler, BridgeError
from webhost.api.run import (_require_project, _validate_run_rpc_params,
                             _project_apply_lock, _delivery_is_busy)


def _context():
    project = _require_project()
    root = str(Path(project.root).resolve())
    manager = state.get_owner_manager()
    def validate_owner(source, provenance):
        return manager.validate_shared_candidate(source, provenance)
    # Separate from owned worktrees; startup never prunes either automatically.
    service = CombinedCandidates(state.get_run_runtime(), workspaces_dir().parent / "candidates",
                                owner_context_supplier=validate_owner)
    return root, service


_SHARED_PREPARE_SLOTS = threading.BoundedSemaphore(2)


@handler("candidate.prepare")
def _prepare(params, ctx):
    _validate_run_rpc_params(params, {"runIds", "verify"}, required={"runIds", "verify"})
    root, service = _context()
    ids = params["runIds"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 2
            or any(type(value) is not str or not value for value in ids)
            or len(set(ids)) != len(ids) or type(params["verify"]) is not bool):
        raise BridgeError("invalid_params", "Bir veya iki farklı sonuç ve açık doğrulama seçimi gerekir.")
    if _delivery_is_busy():
        raise BridgeError("busy", "Teslimat işlemi sürüyor.")
    # Resolve the current registry dynamically (test/process replacement safe).
    from webhost.api import run
    slots = [run._run_registry.get(run_id) for run_id in sorted(ids)]
    if any(slot is None or slot.project_root != root for slot in slots):
        raise BridgeError("candidate_invalid", "Seçilen sonuç bu projede sahiplenilmemiş.")
    try:
        with ExitStack() as stack:
            stack.enter_context(_project_apply_lock(root))
            for slot in slots:
                stack.enter_context(slot.lock)
            return {"candidate": service.prepare(root, slots, verify=params["verify"])}
    except Exception as exc:
        raise BridgeError("candidate_invalid", str(exc)) from None


@handler("candidate.prepareShared")
def _prepare_shared(params, ctx):
    allowed = {"projectRoot", "expectedSessionId", "expectedEpoch", "expectedRevision", "proposalIds", "verify"}
    _validate_run_rpc_params(params, allowed, required=allowed)
    if (type(params["projectRoot"]) is not str or type(params["expectedSessionId"]) is not str
            or type(params["expectedEpoch"]) is not int or params["expectedEpoch"] < 0
            or type(params["expectedRevision"]) is not str
            or len(params["expectedRevision"]) != 40
            or any(c not in "0123456789abcdef" for c in params["expectedRevision"])
            or type(params["verify"]) is not bool or params["verify"] is not True
            or not isinstance(params["proposalIds"], list) or not 1 <= len(params["proposalIds"]) <= 2
            or any(type(item) is not str or not item or len(item) > 128 for item in params["proposalIds"])):
        raise BridgeError("invalid_params", "Bir veya iki teklif ve açık doğrulama seçimi gerekir.")
    project = _require_project()
    root = str(Path(project.root).resolve(strict=True))
    if params["projectRoot"] != root or not _SHARED_PREPARE_SLOTS.acquire(blocking=False):
        raise BridgeError("candidate_invalid", "Proje kimliği geçersiz veya aday işlemi meşgul.")
    generation = state.project_generation()
    try:
        manager = state.get_owner_manager()
    except Exception:
        _SHARED_PREPARE_SLOTS.release()
        raise BridgeError("candidate_invalid", "Sahip oturumu okunamadı.") from None
    def work():
        try:
            identity = manager.shared_candidate_store(Path(root), expected_session_id=params["expectedSessionId"],
                                                      expected_epoch=params["expectedEpoch"])
            with _project_apply_lock(root):
                current = state.get_project()
                if current is None or str(Path(current.root).resolve()) != root or state.project_generation() != generation:
                    raise RuntimeError("Project changed")
            service = CombinedCandidates(state.get_run_runtime(), workspaces_dir().parent / "candidates",
                owner_context_supplier=lambda source, provenance: manager.validate_shared_candidate(source, provenance))
            receipt = service.prepare_shared(root, identity["store"], params["proposalIds"],
                expected_revision=params["expectedRevision"], verify=True,
                session_id=params["expectedSessionId"], epoch=params["expectedEpoch"],
                store_path=identity["storePath"], hub_path=identity["hubPath"],
                context_supplier=lambda session, epoch, revision, context: manager.validate_shared_candidate(root, {
                    "sessionId": session, "epoch": epoch, "revision": revision,
                    "contextHash": context, "baseCommit": identity["baseCommit"],
                    "storePath": identity["storePath"], "hubPath": identity["hubPath"]}))
            with _project_apply_lock(root):
                current = state.get_project()
                if current is None or str(Path(current.root).resolve()) != root or state.project_generation() != generation:
                    raise RuntimeError("Project changed")
            ctx.resolve({"candidate": receipt})
        except Exception as exc:
            ctx.fail("candidate_invalid", "Teklifler, kaynak veya ortak bağlam değişmiş olabilir; adayı yeniden inceleyin.")
        finally:
            _SHARED_PREPARE_SLOTS.release()
    try:
        threading.Thread(target=work, name="shared-candidate-prepare", daemon=True).start()
    except Exception:
        _SHARED_PREPARE_SLOTS.release()
        raise BridgeError("candidate_invalid", "Aday hazırlama başlatılamadı.") from None
    return None


@handler("candidate.list")
def _list(params, ctx):
    _validate_run_rpc_params(params, set())
    root, service = _context()
    try:
        return {"candidates": service.list(root)}
    except Exception:
        raise BridgeError("candidate_unavailable", "Aday geçmişi okunamadı.") from None


def _validate_candidate_id(params):
    _validate_run_rpc_params(params, {"candidateId"}, required={"candidateId"})
    if type(params["candidateId"]) is not str or not 0 < len(params["candidateId"]) <= 128:
        raise BridgeError("invalid_params", "Aday kimliği geçersiz.")


@handler("candidate.apply")
def _apply(params, ctx):
    _validate_candidate_id(params)
    root, service = _context()
    if _delivery_is_busy():
        raise BridgeError("busy", "Teslimat işlemi sürüyor.")
    try:
        with _project_apply_lock(root):
            result = service.apply(root, params["candidateId"])
    except Exception as exc:
        raise BridgeError("candidate_apply_failed", str(exc)) from None
    ctx._bridge.emit_event("fs.changed", {"kind": "modified", "paths": result["applied"]})
    return result


@handler("candidate.rollback")
def _rollback(params, ctx):
    _validate_candidate_id(params)
    root, service = _context()
    if _delivery_is_busy():
        raise BridgeError("busy", "Teslimat işlemi sürüyor.")
    try:
        with _project_apply_lock(root):
            result = service.rollback(root, params["candidateId"])
    except Exception as exc:
        raise BridgeError("candidate_rollback_failed", str(exc)) from None
    ctx._bridge.emit_event("fs.changed", {"kind": "modified", "paths": result["restored"]})
    return result
