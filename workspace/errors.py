"""workspace.errors — izole çalışma alanı hata hiyerarşisi.

Alt-süreç (git) hataları burada tipli hatalara çevrilir; çağıranlar ham
subprocess.CalledProcessError ile uğraşmaz.
"""

from __future__ import annotations


class WorkspaceError(Exception):
    """Tüm workspace hatalarının temel sınıfı."""


class WorkspaceBoundaryError(WorkspaceError):
    """Bir yol işlemi workspace kökünün dışına (veya .git altına) çıkmaya çalıştı."""


class WorkspaceGitError(WorkspaceError):
    """Bir git alt-süreci beklenmedik şekilde başarısız oldu."""


class WorkspaceCreationError(WorkspaceError):
    """İzole workspace oluşturulamadı."""


class WorkspaceCleanupError(WorkspaceError):
    """İzole workspace temizlenemedi (dispose sırasında)."""


class UnsupportedRepositoryStateError(WorkspaceError):
    """Kaynak repo şu an desteklenmeyen bir durumda.

    Örnek: HEAD yok, çözülmemiş merge çakışması var.
    """
