"""collab_runtime.errors — typed, safe errors for the collaboration runtime.

Every message is fixed, actionable text. Raw git stderr, remote URLs and
unvalidated user fragments are never echoed, so messages are safe to display.
`exit_code` is the process exit code a CLI uses for this failure class.
"""

from __future__ import annotations


class CollabError(Exception):
    """Base class for all expected collab_runtime failures."""

    exit_code = 1

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ValidationError(CollabError):
    """Malformed ids/scopes/JSON/state or over-limit data."""

    exit_code = 2


class TaskExistsError(ValidationError):
    """Creation rejected because a task identifier is already assigned."""

    def __init__(self) -> None:
        super().__init__("task id already exists; tasks cannot be replaced.")


class TaskCapacityError(ValidationError):
    """Creation rejected because the bounded session cannot accept a task."""

    def __init__(self) -> None:
        super().__init__("task capacity or session state size limit reached.")


class StaleRevisionError(CollabError):
    """Publication rejected: another client advanced the session first."""

    exit_code = 3


class SessionNotFoundError(CollabError):
    """The hub carries no session branch yet."""

    exit_code = 4


class SessionAlreadyExistsError(CollabError):
    """One session per hub: the session branch already exists."""

    exit_code = 4


class ProjectStateError(CollabError):
    """The source project is unusable for metadata purposes (not a repo,
    no committed HEAD). The project itself is never modified."""

    exit_code = 2


class GitOperationError(CollabError):
    """A git transport/plumbing operation failed (fixed safe message only)."""

    exit_code = 5


class ReplayUnavailableError(CollabError):
    """The requested revision is outside the bounded replay window (unknown,
    foreign or too old); the fixed remedy is to fetch a new snapshot."""

    exit_code = 7


class AccessDeniedError(CollabError):
    """The supplied credential is invalid or lacks permission for the operation."""

    exit_code = 6
