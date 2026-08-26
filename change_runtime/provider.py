"""Provider-neutral change-capture port."""

from __future__ import annotations

from typing import Protocol

from change_runtime.models import WorkspaceChangeSet


class ChangeProvider(Protocol):
    def capture(self, workspace) -> WorkspaceChangeSet:
        """Capture the cumulative change set between the workspace's snapshot
        baseline and its current working tree. Must never mutate the
        workspace, its Git index, or its source repository."""
