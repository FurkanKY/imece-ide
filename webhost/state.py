"""
state.py — host tarafı paylaşılan oturum durumu.

Aktif Project örneği tek yerde tutulur; fs/project/run/search handler'ları buradan okur.
(Motor `project.py:Project` dokunulmadan sarmalanır.)

Ayrıca süreç boyunca PAYLAŞILAN tek bir RunRuntime (RunStore + EventBus)
örneğini tutar (bkz. get_run_runtime) — her API çağrısında yeni bir RunRuntime
OLUŞTURULMAZ, aksi halde EventBus aboneleri bölünürdü.
"""

import os
import threading
from pathlib import Path

from project import Project
from run_runtime.service import RunRuntime
from run_runtime.store import RunStore
from runtime_paths import run_runtime_db_path, collab_cursors_dir

_active: Project | None = None
_run_runtime: RunRuntime | None = None
_collaboration_host = None
_project_generation = 0
_collaboration_lock = threading.RLock()
_collaboration_status_cache = None
_delivery_service = None
_owner_manager = None


def set_project(root: str) -> Project:
    global _active, _project_generation, _collaboration_status_cache
    previous = _active
    _active = Project(root)
    _project_generation += 1
    _collaboration_status_cache = None
    if previous is not None and previous.root != _active.root and _collaboration_host is not None:
        try:
            _collaboration_host.clear(previous.root)
        except Exception:
            pass
    return _active


def project_generation() -> int:
    return _project_generation


def get_collaboration_host():
    """Lazily create the inert host; construction performs no I/O."""
    global _collaboration_host
    with _collaboration_lock:
        if _collaboration_host is None:
            from collab_runtime.host import CollaborationHost
            _collaboration_host = CollaborationHost(collab_cursors_dir())
        return _collaboration_host


def set_collaboration_host(host) -> None:
    """Test injection/reset hook. Does not close resources owned by workers."""
    global _collaboration_host
    with _collaboration_lock:
        _collaboration_host = host


def get_delivery_service():
    """Create the in-memory publication-ticket service only when requested."""
    global _delivery_service
    with _collaboration_lock:
        if _delivery_service is None:
            from collab_runtime.delivery import SharedDeliveryService
            _delivery_service = SharedDeliveryService()
        return _delivery_service


def set_delivery_service(service) -> None:
    """Test injection/reset hook; delivery tickets are process-local only."""
    global _delivery_service
    with _collaboration_lock:
        _delivery_service = service


def get_owner_manager():
    """Lazily create the inert owner-side lifecycle manager."""
    global _owner_manager
    with _collaboration_lock:
        if _owner_manager is None:
            from collab_runtime.owner import OwnerSessionManager
            _owner_manager = OwnerSessionManager()
        return _owner_manager


def peek_owner_manager():
    """Return an existing manager without constructing one (shutdown path)."""
    with _collaboration_lock:
        return _owner_manager


def set_owner_manager(manager) -> None:
    """Inject/reset the process-local owner manager; performs no cleanup."""
    global _owner_manager
    with _collaboration_lock:
        _owner_manager = manager


def set_collaboration_status_cache(cache) -> None:
    """Keep only a detached terminal status DTO in process memory."""
    global _collaboration_status_cache
    _collaboration_status_cache = ({**cache, "status": dict(cache.get("status") or {})}
                                   if isinstance(cache, dict) else None)


def get_collaboration_status_cache(run_id=None):
    cache = _collaboration_status_cache
    if not isinstance(cache, dict):
        return None
    project = get_project()
    try:
        matches_root = (project is not None and
                        Path(project.root).resolve() == Path(cache.get("projectRoot", "")).resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        matches_root = False
    if (not matches_root or
            (run_id is not None and run_id != cache.get("runId"))):
        return None
    return dict(cache.get("status") or {})


def get_project() -> Project | None:
    return _active


def project_name() -> str:
    return os.path.basename(_active.root) if _active else ""


def get_run_runtime() -> RunRuntime:
    """Süreç boyunca paylaşılan tek RunRuntime'ı tembel (lazy) biçimde oluşturur/döndürür.

    Veritabanı dosyası yalnızca bu fonksiyon İLK KEZ çağrıldığında (RunStore
    ilk gerçek işlemini yaptığında) oluşur — modül İÇE AKTARILIRKEN (import)
    bir yan etki OLARAK asla dokunulmaz.
    """
    global _run_runtime
    if _run_runtime is None:
        _run_runtime = RunRuntime(RunStore(run_runtime_db_path()))
    return _run_runtime


def set_run_runtime(runtime: RunRuntime | None) -> None:
    """Testler için enjeksiyon/sıfırlama kancası.

    Gerçek kullanıcı uygulama veritabanına dokunmadan sahte/geçici bir
    RunRuntime enjekte etmeyi sağlar. None verilmesi, bir sonraki
    get_run_runtime() çağrısının varsayılan (gerçek yol) örneği yeniden
    tembel biçimde oluşturmasına yol açar.
    """
    global _run_runtime
    _run_runtime = runtime
