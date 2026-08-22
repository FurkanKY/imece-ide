"""workspace.local — mevcut bir klasörü doğrudan temsil eden Workspace.

Project sınıfının (project.py) yerini almaz ve onu değiştirmez; gelecekteki
runtime kodunun Project yerine soyut Workspace'e bağımlı olabilmesi için
vardır.
"""

from __future__ import annotations

from pathlib import Path

from workspace.base import Workspace
from workspace.errors import WorkspaceCreationError


class LocalWorkspace(Workspace):
    """Kullanıcının gerçek proje klasörünü temsil eden, izole OLMAYAN workspace."""

    def __init__(self, root: str | Path):
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise WorkspaceCreationError(f"Klasör bulunamadı: {resolved}")
        self._root = resolved

    @property
    def root(self) -> Path:
        return self._root

    def dispose(self) -> None:
        """No-op: LocalWorkspace kullanıcının gerçek klasörünü temsil eder, silinmez."""
        return None
