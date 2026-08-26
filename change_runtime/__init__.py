"""change_runtime — provider-neutral cumulative workspace change capture.

Deliberately separate from `workspace/`: Workspace stays a filesystem/
lifecycle abstraction; this package owns diff/change-set concepts.
"""

from change_runtime.errors import ChangeCaptureError, ChangeInputError, ChangeRuntimeError
from change_runtime.git import GitWorktreeChangeProvider
from change_runtime.models import WorkspaceChangeSet
from change_runtime.provider import ChangeProvider

__all__ = [
    "ChangeRuntimeError",
    "ChangeInputError",
    "ChangeCaptureError",
    "WorkspaceChangeSet",
    "ChangeProvider",
    "GitWorktreeChangeProvider",
]
