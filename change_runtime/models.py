"""Immutable, provider-neutral workspace change-capture contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from workspace.base import normalize_workspace_relative_path
from workspace.errors import WorkspaceBoundaryError

from change_runtime.errors import ChangeInputError


def _validated_path(value: str) -> str:
    try:
        normalized = normalize_workspace_relative_path(value, allow_root=False)
    except WorkspaceBoundaryError as exc:
        raise ChangeInputError(f"changed_paths entry must be workspace-relative: {value!r}") from exc
    if normalized != value:
        raise ChangeInputError(f"changed_paths entry must already be normalized: {value!r}")
    return normalized


@dataclass(frozen=True, slots=True)
class WorkspaceChangeSet:
    """A cumulative snapshot-baseline -> current-workspace change capture.

    `diff_sha256` is intentionally NOT an init parameter — it is always
    computed from the exact accepted `diff` text, never caller-supplied.
    """

    diff: str
    changed_paths: tuple[str, ...]
    diff_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.diff, str) or "\x00" in self.diff:
            raise ChangeInputError("WorkspaceChangeSet.diff must be NUL-free text.")
        paths = tuple(_validated_path(path) for path in self.changed_paths)
        if len(set(paths)) != len(paths):
            raise ChangeInputError("WorkspaceChangeSet.changed_paths must be unique.")
        if tuple(sorted(paths)) != paths:
            raise ChangeInputError("WorkspaceChangeSet.changed_paths must be sorted deterministically.")
        object.__setattr__(self, "changed_paths", paths)
        object.__setattr__(self, "diff_sha256", hashlib.sha256(self.diff.encode("utf-8")).hexdigest())
