"""Helpers for launching the ACP subprocess fixtures with an isolated env."""

from __future__ import annotations

import os


def fixture_child_env(supplied: dict[str, str] | None = None) -> dict[str, str]:
    """Preserve exact-env tests while supplying Windows' minimal OS variables."""
    env = dict(supplied or {})
    if os.name == "nt":
        for name in ("SystemRoot", "SystemDrive", "TEMP"):
            value = os.environ.get(name)
            if value is not None:
                env.setdefault(name, value)
    return env
