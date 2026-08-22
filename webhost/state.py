"""
state.py — host tarafı paylaşılan oturum durumu.

Aktif Project örneği tek yerde tutulur; fs/project/run/search handler'ları buradan okur.
(Motor `project.py:Project` dokunulmadan sarmalanır.)

Ayrıca süreç boyunca PAYLAŞILAN tek bir RunRuntime (RunStore + EventBus)
örneğini tutar (bkz. get_run_runtime) — her API çağrısında yeni bir RunRuntime
OLUŞTURULMAZ, aksi halde EventBus aboneleri bölünürdü.
"""

import os

from project import Project
from run_runtime.service import RunRuntime
from run_runtime.store import RunStore
from runtime_paths import run_runtime_db_path

_active: Project | None = None
_run_runtime: RunRuntime | None = None


def set_project(root: str) -> Project:
    global _active
    _active = Project(root)
    return _active


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
