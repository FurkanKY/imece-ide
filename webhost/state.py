"""
state.py — host tarafı paylaşılan oturum durumu.

Aktif Project örneği tek yerde tutulur; fs/project/run/search handler'ları buradan okur.
(Motor `project.py:Project` dokunulmadan sarmalanır.)
"""

import os

from project import Project

_active: Project | None = None


def set_project(root: str) -> Project:
    global _active
    _active = Project(root)
    return _active


def get_project() -> Project | None:
    return _active


def project_name() -> str:
    return os.path.basename(_active.root) if _active else ""
