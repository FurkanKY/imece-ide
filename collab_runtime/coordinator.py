"""Transport-neutral, authenticated control of one pinned metadata session.

Trusted local configuration supplies membership; credentials are retained only
as hashes. All Git/history access stays in GitStore, and local commands share
one lock. This access boundary covers this interface, not direct store access.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Any

from collab_runtime.errors import AccessDeniedError, StaleRevisionError, TaskCapacityError, TaskExistsError, ValidationError
from collab_runtime.models import (
    ALL_STATUSES,
    MAX_JSON_BYTES,
    MAX_TASKS,
    SessionState,
    SharedContext,
    build_context,
    build_task,
    parse_state_dict,
    safe_id,
    sha_hex,
    canonical_json_bytes,
)
from collab_runtime.store import MAX_REPLAY_PAGE, GitStore

_MAX_MEMBERS = 256
_CREDENTIAL_RE = re.compile(r"[A-Za-z0-9_-]{32,256}", re.ASCII)
_ACCESS_DENIED = "access denied: a valid member credential and permission are required."


@dataclass(frozen=True)
class Snapshot:
    """Detached schema1 metadata; the state's task mapping is read-only."""

    revision: str
    state: SessionState

    def to_dict(self) -> dict[str, Any]:
        return {"revision": self.revision, "state": self.state.to_dict()}


@dataclass(frozen=True)
class ReplayEvent:
    """One durable revision transition, without context or task payloads."""

    previous_revision: str
    revision: str
    context_changed: bool
    changed_task_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_revision": self.previous_revision,
            "revision": self.revision,
            "context_changed": self.context_changed,
            "changed_task_ids": list(self.changed_task_ids),
        }


@dataclass(frozen=True)
class ReplayPage:
    """Persist next_revision after consuming this replayable event page."""

    head_revision: str
    next_revision: str
    events: tuple[ReplayEvent, ...]
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "head_revision": self.head_revision,
            "next_revision": self.next_revision,
            "events": [event.to_dict() for event in self.events],
            "has_more": self.has_more,
        }


def _credential_hash(credential: Any) -> bytes | None:
    if (
        not isinstance(credential, str)
        or not 32 <= len(credential) <= 256
        or not _CREDENTIAL_RE.fullmatch(credential)
    ):
        return None
    return hashlib.sha256(credential.encode("ascii")).digest()


def _detached_state(state: SessionState) -> SessionState:
    if not isinstance(state, SessionState):
        raise ValidationError("the store must return a valid schema1 session state.")
    try:
        validated = parse_state_dict(state.to_dict())
    except (AttributeError, TypeError, ValueError, RecursionError):
        raise ValidationError("the store must return a valid schema1 session state.") from None
    return SessionState(
        validated.session_id,
        validated.target_version,
        validated.base_commit,
        validated.context,
        MappingProxyType(dict(validated.tasks)),
    )


def _validated_context(context: SharedContext | dict[str, Any]) -> SharedContext:
    if isinstance(context, SharedContext) and (
        not isinstance(context.decisions, tuple)
        or not isinstance(context.interfaces, tuple)
        or any(not isinstance(pair, tuple) or len(pair) != 2 for pair in context.interfaces)
    ):
        raise ValidationError("context must contain valid goal, decisions and interfaces fields.")
    try:
        raw = context.to_dict() if isinstance(context, SharedContext) else context
    except (AttributeError, TypeError, ValueError, RecursionError):
        raise ValidationError("context must contain valid goal, decisions and interfaces fields.") from None
    if not isinstance(raw, dict) or set(raw) != {"goal", "decisions", "interfaces"}:
        raise ValidationError("context must contain exactly goal, decisions and interfaces fields.")
    if isinstance(context, SharedContext) and len(raw["interfaces"]) != len(context.interfaces):
        raise ValidationError("context interface names must be distinct.")
    return build_context(
        goal=raw["goal"], decisions=raw["decisions"], interfaces=raw["interfaces"],
    )


class Coordinator:
    """Four serialized operations over an existing owner-side GitStore.

    Construction is trusted and fetches the hub to pin session identity.
    Each later call authenticates before state I/O; writes use the revision
    whose state supplied both authorization and the unchanged metadata.
    """

    def __init__(
        self,
        store: GitStore,
        *,
        session_id: str,
        owner_id: str,
        member_credentials: Mapping[str, str],
    ) -> None:
        expected_id = safe_id(session_id, "session_id")
        owner = safe_id(owner_id, "owner id")
        if not isinstance(member_credentials, Mapping) or not 1 <= len(member_credentials) <= _MAX_MEMBERS:
            raise ValidationError("member credentials must be a mapping of 1-256 principals including the owner.")
        hashes: dict[str, bytes] = {}
        seen: set[bytes] = set()
        for member_id, credential in member_credentials.items():
            principal = safe_id(member_id, "member id")
            digest = _credential_hash(credential)
            if digest is None:
                raise ValidationError("member credentials must be 32-256 URL-safe ASCII characters.")
            if digest in seen:
                raise ValidationError("each member must have a distinct credential.")
            seen.add(digest)
            hashes[principal] = digest
        if owner not in hashes:
            raise ValidationError("the owner must be included in member credentials.")

        self._lock = RLock()
        self._store = store
        self._owner_id = owner
        self._credential_hashes = MappingProxyType(hashes)
        revision, raw_state = store.fetch_state()
        sha_hex(revision, "session revision")
        state = _detached_state(raw_state)
        if state.session_id != expected_id:
            raise ValidationError("the hub session does not match the configured session_id.")
        self._identity = (state.session_id, state.base_commit, state.target_version)

    def _authenticate(self, credential: str) -> str:
        digest = _credential_hash(credential)
        if digest is None:
            raise AccessDeniedError(_ACCESS_DENIED)
        principal = None
        for member_id, stored_hash in self._credential_hashes.items():
            if hmac.compare_digest(digest, stored_hash):
                principal = member_id
        if principal is None:
            raise AccessDeniedError(_ACCESS_DENIED)
        return principal

    def check_access(self, credential: str) -> None:
        """Authentication-only seam for transport adapters.

        Acquires the same lock as the domain operations and verifies only
        the credential: no identity or metadata is returned and no Git I/O
        happens. Domain operations still authenticate and enforce their own
        permissions afterwards.
        """
        with self._lock:
            self._authenticate(credential)

    def _checked_state(self, raw_state: SessionState) -> SessionState:
        state = _detached_state(raw_state)
        if (state.session_id, state.base_commit, state.target_version) != self._identity:
            raise ValidationError("the hub session identity changed; reconnect with trusted configuration.")
        return state

    def _fetch_state(self) -> tuple[str, SessionState]:
        revision, raw_state = self._store.fetch_state()
        return sha_hex(revision, "session revision"), self._checked_state(raw_state)

    @staticmethod
    def _require_current(revision: str, expected: str) -> None:
        if revision != expected:
            raise StaleRevisionError("expected_revision is no longer current; fetch a new snapshot and retry.")

    def snapshot(self, credential: str) -> Snapshot:
        with self._lock:
            self._authenticate(credential)
            revision, state = self._fetch_state()
            return Snapshot(revision, state)

    def update_context(
        self,
        credential: str,
        context: SharedContext | dict[str, Any],
        *,
        expected_revision: str,
    ) -> str:
        with self._lock:
            if self._authenticate(credential) != self._owner_id:
                raise AccessDeniedError(_ACCESS_DENIED)
            expected = sha_hex(expected_revision, "expected_revision")
            validated = _validated_context(context)
            revision, current = self._fetch_state()
            self._require_current(revision, expected)
            return self._store.publish(current.with_context(validated), expected_revision=expected)

    def update_task_status(
        self,
        credential: str,
        *,
        task_id: str,
        status: str,
        expected_revision: str,
    ) -> str:
        with self._lock:
            principal = self._authenticate(credential)
            identifier = safe_id(task_id, "task id")
            if not isinstance(status, str) or status not in ALL_STATUSES:
                raise ValidationError("task status must be one of: queued, running, waiting, done.")
            expected = sha_hex(expected_revision, "expected_revision")
            revision, current = self._fetch_state()
            task = current.tasks.get(identifier)
            if task is None:
                raise ValidationError("task status can only be changed for an existing task.")
            if principal not in (self._owner_id, task.owner):
                raise AccessDeniedError(_ACCESS_DENIED)
            self._require_current(revision, expected)
            if not self._store.is_ancestor(task.context_revision, revision):
                raise ValidationError(
                    "task context_revision must be an existing revision of this session's published history."
                )
            updated = build_task(
                task_id=task.id,
                owner=task.owner,
                goal=task.goal,
                scopes=list(task.scopes),
                status=status,
                context_revision=task.context_revision,
            )
            # Publish this exact authorized state: a legacy reassignment after
            # the fetch must make our CAS fail rather than be overwritten.
            return self._store.publish(current.with_task(updated), expected_revision=expected)

    def create_task(
        self, credential: str, *, task_id: str, owner: str, goal: str,
        scopes: list[str], expected_revision: str,
    ) -> str:
        """Append one newly assigned queued task to the pinned session."""
        with self._lock:
            if self._authenticate(credential) != self._owner_id:
                raise AccessDeniedError(_ACCESS_DENIED)
            identifier = safe_id(task_id, "task id")
            assignee = safe_id(owner, "owner id")
            if assignee not in self._credential_hashes:
                raise ValidationError("task owner must be a configured member.")
            if not isinstance(goal, str) or not goal.strip():
                raise ValidationError("task goal must not be empty.")
            expected = sha_hex(expected_revision, "expected_revision")
            task = build_task(task_id=identifier, owner=assignee, goal=goal, scopes=scopes,
                              status="queued", context_revision=expected)
            revision, current = self._fetch_state()
            self._require_current(revision, expected)
            if identifier in current.tasks:
                raise TaskExistsError()
            if len(current.tasks) >= MAX_TASKS:
                raise TaskCapacityError()
            updated = current.with_task(task)
            if len(canonical_json_bytes(updated.to_dict())) > MAX_JSON_BYTES:
                raise TaskCapacityError()
            return self._store.publish(updated, expected_revision=expected)

    def replay(
        self, credential: str, *, after_revision: str, limit: int = MAX_REPLAY_PAGE,
    ) -> ReplayPage:
        with self._lock:
            self._authenticate(credential)
            cursor = sha_hex(after_revision, "after_revision")
            if type(limit) is not int or not 1 <= limit <= MAX_REPLAY_PAGE:
                raise ValidationError("limit must be an integer between 1 and 32.")
            page = self._store.replay_states(cursor, limit=limit)
            head = sha_hex(page.head_revision, "replay head revision")
            previous_revision = cursor
            previous = self._checked_state(page.previous_state)
            events: list[ReplayEvent] = []
            for revision, raw_state in page.states:
                revision = sha_hex(revision, "replay revision")
                state = self._checked_state(raw_state)
                changed = tuple(sorted(
                    task_id for task_id in previous.tasks.keys() | state.tasks.keys()
                    if previous.tasks.get(task_id) != state.tasks.get(task_id)
                ))
                events.append(ReplayEvent(
                    previous_revision, revision, previous.context != state.context, changed,
                ))
                previous_revision, previous = revision, state
            return ReplayPage(head, previous_revision, tuple(events), page.has_more)
