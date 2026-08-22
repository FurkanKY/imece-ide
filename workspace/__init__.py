"""workspace — izole ajan çalışma alanı çatısı.

Workspace / LocalWorkspace / GitWorktreeWorkspace; gelecekteki ajan-koşum
(agent-loop) refactor'unun üzerine kurulacağı, salt dosya sistemi/yaşam
döngüsü temeli. project.py'deki mevcut Project sınıfının yerini ALMAZ.
"""

from workspace.base import Workspace
from workspace.errors import (
    UnsupportedRepositoryStateError,
    WorkspaceBoundaryError,
    WorkspaceCleanupError,
    WorkspaceCreationError,
    WorkspaceError,
    WorkspaceGitError,
)
from workspace.local import LocalWorkspace
from workspace.snapshot import WorkspaceSnapshot
from workspace.worktree import GitWorktreeWorkspace

__all__ = [
    "Workspace",
    "LocalWorkspace",
    "GitWorktreeWorkspace",
    "WorkspaceSnapshot",
    "WorkspaceError",
    "WorkspaceBoundaryError",
    "WorkspaceGitError",
    "WorkspaceCreationError",
    "WorkspaceCleanupError",
    "UnsupportedRepositoryStateError",
]
