"""workspace.snapshot — izole workspace'in başlangıç durumunun değişmez kaydı.

snapshot_commit: ajanın çalışmaya BAŞLARKEN gördüğü tam Git commit/tree
durumu (kullanıcının HEAD'i + o anki staged/unstaged/untracked değişiklikleri
üst üste bindirilerek üretilen sentetik commit; bkz. workspace/worktree.py).

source_head: sentetik shadow snapshot'tan ÖNCEKİ, kullanıcının gerçek Git
HEAD'i.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    run_id: str
    source_root: Path
    repository_root: Path
    source_head: str
    snapshot_commit: str
    project_relative_root: Path
    tracked_dirty_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    created_at: datetime
