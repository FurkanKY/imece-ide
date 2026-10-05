"""Application-owned approval and native-run lifecycle for loopback collaboration.

No server, consumer, filesystem namespace, or model is created until the caller
explicitly activates a bound run from its worker thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import secrets
import stat
import subprocess
import threading
import time
from types import MappingProxyType
from typing import Any, Callable
import weakref

from agent_runtime.cancellation import OperationCancelledError
from collab_runtime.client import LoopbackSnapshotClient
from collab_runtime.commands import LoopbackTaskClient, TaskCommandError
from collab_runtime.consumer import RevisionConsumer
from collab_runtime.context import SharedSnapshot, parse_snapshot_dict
from collab_runtime.errors import CollabError
from collab_runtime.models import ACTIVE_STATUSES, Task, safe_id
from collab_runtime.safe_point import NativeWorkerSafePoint


class HostCollaborationError(CollabError):
    """Fixed-message native host error, safe to surface to the local UI."""

    _MESSAGES = {
        "invalid": "The local collaboration approval is invalid or unavailable.",
        "stale": "The local collaboration approval no longer matches this project or session.",
        "busy": "This collaboration checkpoint is already owned by another run.",
        "storage": "The private collaboration checkpoint is unavailable.",
        "cancelled": "Collaboration activation was cancelled.",
    }

    def __init__(self, code: str = "invalid") -> None:
        if code not in self._MESSAGES:
            code = "invalid"
        self.code = code
        super().__init__(self._MESSAGES[code])


class ParticipantCommandError(CollabError):
    """Fixed-message participant command failure with truthful uncertainty."""

    _MESSAGES = {
        "invalid": "The participant status ticket is invalid or unavailable.",
        "stale": "The participant status ticket no longer matches the reviewed task.",
        "busy": "Participant status commands are available only while idle.",
        "expired": "The participant status ticket has expired.",
        "access_denied": "The participant status update was denied.",
        "stale_revision": "The reviewed task status is stale; review a fresh snapshot.",
        "invalid_request": "The participant status update was rejected.",
        "session_unavailable": "The collaboration session is unavailable.",
        "server_error": "The task status server failed.",
        "protocol_error": "The task status response violated the local protocol.",
        "outcome_unknown": "The task status outcome is unknown; reconcile explicitly.",
        "connection_error": "The local task status endpoint could not be reached.",
    }

    def __init__(self, code: str = "invalid", *, outcome_uncertain: bool = False) -> None:
        if code not in self._MESSAGES or type(outcome_uncertain) is not bool:
            code, outcome_uncertain = "invalid", False
        self.code = code
        self.outcome_uncertain = outcome_uncertain
        super().__init__(self._MESSAGES[code])


_TTL = 600.0
_MAX_RECORDS = 8
_ACTIVE = set(ACTIVE_STATUSES)


def _immutable_snapshot(snapshot: SharedSnapshot) -> SharedSnapshot:
    validated = parse_snapshot_dict(snapshot.to_dict())
    state = validated.state
    immutable_state = type(state)(
        state.session_id, state.target_version, state.base_commit, state.context,
        MappingProxyType(dict(state.tasks)),
    )
    return SharedSnapshot(validated.revision, validated.context_hash, immutable_state, validated.task_id)


class _PreparedHostInput:
    """Host receipt wrapper; only a successful real safe-point ack earns receipt."""

    def __init__(self, session: "NativeRunCollaboration", prepared: Any, consumer: Any,
                 helper: Any, record: Any, generation: int) -> None:
        self.request = prepared.request
        self.binding = prepared.binding
        self._session, self._prepared = session, prepared
        self._consumer, self._helper, self._record = consumer, helper, record
        self._generation = generation

    def acknowledge(self, *, cancel_token: Any = None) -> None:
        self._prepared.acknowledge(cancel_token=cancel_token)
        self._session._record_accepted_binding(
            self.binding, self._consumer, self._helper, self._record, self._generation
        )


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        value = result.stdout.decode("ascii").strip()
        if result.returncode != 0 or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError()
        return value
    except Exception:
        raise HostCollaborationError("invalid") from None


def _safe_root(value: Any) -> Path:
    try:
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError()
        root = Path(value).resolve(strict=True)
        if not root.is_dir():
            raise ValueError()
        return root
    except Exception:
        raise HostCollaborationError("invalid") from None


def _task_dto(task: Task) -> dict[str, Any]:
    return {"owner": task.owner, "goal": task.goal, "scopes": list(task.scopes),
            "status": task.status, "contextRevision": task.context_revision}


def _preview_dto(record: "_Record") -> dict[str, Any]:
    state = record.snapshot.state
    return {
        "previewId": record.preview_id, "projectRoot": str(record.root),
        "endpoint": record.endpoint, "memberId": record.member_id,
        "taskId": record.task_id, "sessionId": state.session_id,
        "targetVersion": state.target_version, "baseCommit": state.base_commit,
        "revision": record.snapshot.revision, "task": _task_dto(record.task),
        "context": {"goal": state.context.goal, "decisions": list(state.context.decisions),
                    "interfaces": dict(state.context.interfaces)},
    }


@dataclass(repr=False)
class _Record:
    preview_id: str
    root: Path
    endpoint: str
    member_id: str
    task_id: str
    snapshot: Any
    task: Task
    client: Any = field(repr=False)
    credential: str = field(repr=False)
    created: float = field(default_factory=time.monotonic)
    approval_handle: str | None = None
    reset_cursor: bool = False


class CollaborationHost:
    """Local trust boundary. Construction is inert; records are memory-only."""

    def __init__(self, cursor_root: Path, *, head_reader: Callable[[Path], str] | None = None,
                 client_factory: Callable[..., Any] | None = None,
                 task_client_factory: Callable[..., Any] | None = None) -> None:
        self._cursor_root = Path(cursor_root)
        self._head_reader = head_reader or _git_head
        self._client_factory = client_factory or LoopbackSnapshotClient
        self._task_client_factory = task_client_factory or LoopbackTaskClient
        self._lock = threading.RLock()
        self._candidates: dict[str, _Record] = {}
        self._approved: dict[str, _Record] = {}
        self._sessions: weakref.WeakSet[NativeRunCollaboration] = weakref.WeakSet()

    def _prune(self) -> None:
        now = time.monotonic()
        for records in (self._candidates, self._approved):
            for key, record in tuple(records.items()):
                if now - record.created > _TTL:
                    records.pop(key, None)

    def preview(self, project_root: Path, base_url: str, credential: str,
                member_id: str, task_id: str) -> dict[str, Any]:
        try:
            root = _safe_root(project_root)
            safe_id(member_id, "member id")
            safe_id(task_id, "task id")
            head = self._head_reader(root)
            client = self._client_factory(base_url, credential=credential)
            snapshot = client.snapshot()
            state = snapshot.state
            task = state.tasks[task_id]
            if state.base_commit != head or task.owner != member_id or task.status not in _ACTIVE:
                raise ValueError()
            record = _Record(secrets.token_urlsafe(24), root, base_url, member_id,
                             task_id, snapshot, task, client, credential)
            with self._lock:
                self._prune()
                if len(self._candidates) + len(self._approved) >= _MAX_RECORDS:
                    raise ValueError()
                self._candidates[record.preview_id] = record
            return _preview_dto(record)
        except HostCollaborationError:
            raise
        except Exception:
            raise HostCollaborationError("invalid") from None

    def approve(self, preview_id: str, project_root: Path, *, reset_cursor: bool = False) -> dict[str, Any]:
        if type(reset_cursor) is not bool:
            raise HostCollaborationError("invalid")
        root = _safe_root(project_root)
        with self._lock:
            self._prune()
            record = self._candidates.get(preview_id) if isinstance(preview_id, str) else None
            if record is None or record.root != root:
                raise HostCollaborationError("invalid")
        try:
            current_head = self._head_reader(root)
        except Exception:
            raise HostCollaborationError("invalid") from None
        if current_head != record.snapshot.state.base_commit:
            raise HostCollaborationError("stale")
        handle = secrets.token_urlsafe(32)
        with self._lock:
            if self._candidates.pop(record.preview_id, None) is not record:
                raise HostCollaborationError("invalid")
            record.approval_handle = handle
            record.reset_cursor = reset_cursor
            self._approved[handle] = record
        return {"approvalHandle": handle, "preview": _preview_dto(record), "resetCursor": reset_cursor}

    def release(self, approval_handle: str) -> None:
        if not isinstance(approval_handle, str):
            raise HostCollaborationError("invalid")
        with self._lock:
            record = self._approved.pop(approval_handle, None)
            if record is None:
                raise HostCollaborationError("invalid")

    def drop_preview(self, preview_id: str) -> None:
        """Idempotently discard one unapproved candidate and its credential."""
        if not isinstance(preview_id, str):
            return
        with self._lock:
            self._candidates.pop(preview_id, None)

    def clear(self, project_root: Path | None = None) -> None:
        """Invalidate preview/approval credentials without closing run-owned sessions."""
        root = _safe_root(project_root) if project_root is not None else None
        with self._lock:
            for records in (self._candidates, self._approved):
                for key, record in tuple(records.items()):
                    if root is None or record.root == root:
                        records.pop(key, None)

    def bind_run(self, approval_handle: str, project_root: Path, run_id: str) -> "NativeRunCollaboration":
        if not isinstance(approval_handle, str):
            raise HostCollaborationError("invalid")
        root = _safe_root(project_root)
        try:
            safe_id(run_id, "run id")
        except Exception:
            raise HostCollaborationError("invalid") from None
        with self._lock:
            self._prune()
            record = self._approved.get(approval_handle)
            if record is None or record.root != root:
                raise HostCollaborationError("invalid")
        try:
            current_head = self._head_reader(root)
        except Exception:
            raise HostCollaborationError("invalid") from None
        if current_head != record.snapshot.state.base_commit:
            raise HostCollaborationError("stale")
        with self._lock:
            if self._approved.get(approval_handle) is not record:
                raise HostCollaborationError("invalid")
            if len(self._sessions) >= _MAX_RECORDS:
                raise HostCollaborationError("busy")
            session = NativeRunCollaboration(self, record, run_id,
                                             command_factory=self._task_client_factory)
            self._sessions.add(session)
            return session


class _CheckpointLease:
    def __init__(self, path: Path) -> None:
        self.fd: int | None = None
        fd: int | None = None
        try:
            try:
                before = path.lstat()
            except FileNotFoundError:
                before = None
            if before is not None and not stat.S_ISREG(before.st_mode):
                raise ValueError()
            if os.name == "posix":
                import fcntl
                flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                fd = os.open(path, flags, 0o600)
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_size > 64 or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or (before is not None and
                        (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino))):
                    raise ValueError()
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise HostCollaborationError("busy") from None
                self.fd = fd
                fd = None
            elif os.name == "nt":
                import msvcrt
                fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_size > 64 or
                        (before is not None and (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino))):
                    raise ValueError()
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise HostCollaborationError("busy") from None
                self.fd = fd
                fd = None
            else:
                raise ValueError()
        except HostCollaborationError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise HostCollaborationError("storage") from None

    def close(self) -> None:
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)
        except Exception:
            self.fd = fd
            raise HostCollaborationError("storage") from None


class NativeRunCollaboration:
    """Stable safe-point proxy and explicit per-worker consumer lifecycle."""

    def __init__(self, host: CollaborationHost, record: _Record, run_id: str,
                 *, command_factory: Callable[..., Any] | None = None) -> None:
        self._host, self._record, self.run_id = host, record, run_id
        self._state_lock = threading.RLock()
        self._op_lock = threading.RLock()
        self._command_factory = command_factory or LoopbackTaskClient
        self._participant_tickets: dict[str, tuple[dict[str, Any], float, _Record, int]] = {}
        self._consumer = None
        self._safe_point = None
        self._lease = None
        self._active = False
        self._closed = False
        self._generation = 0
        self._accepted_binding: SharedSnapshot | None = None
        self._pinned_identity = (record.root, record.member_id, record.task_id, run_id,
                                 record.snapshot.state.session_id, record.snapshot.state.base_commit,
                                 record.snapshot.state.target_version)
        self._last = {"state": "inactive", "code": None, "consumedRevision": record.snapshot.revision,
                      "receivedRevision": record.snapshot.revision, "pendingCount": 0}

    def __repr__(self) -> str:
        return "<NativeRunCollaboration>"

    @property
    def active(self) -> bool:
        with self._state_lock:
            return self._active

    @property
    def project_root(self) -> Path:
        return self._record.root

    @property
    def safe_point(self) -> "NativeRunCollaboration":
        return self

    @property
    def accepted_binding(self) -> SharedSnapshot | None:
        with self._state_lock:
            binding = self._accepted_binding
            record = self._record
            pins = (record.root, record.member_id, record.task_id, self.run_id,
                    record.snapshot.state.session_id, record.snapshot.state.base_commit,
                    record.snapshot.state.target_version)
            if binding is None or pins != self._pinned_identity:
                return None
            try:
                return _immutable_snapshot(binding)
            except Exception:
                return None

    def _record_accepted_binding(self, binding: Any, consumer: Any, helper: Any,
                                 record: Any, generation: int) -> None:
        if not isinstance(binding, SharedSnapshot):
            return
        try:
            detached = _immutable_snapshot(binding)
            consumed_revision = consumer.status().get("consumed_revision")
        except Exception:
            return
        with self._state_lock:
            if (self._active and self._generation == generation and self._consumer is consumer
                    and self._safe_point is helper and self._record is record
                    and consumed_revision == binding.revision):
                self._accepted_binding = detached

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            active, consumer, current = self._active, self._consumer, dict(self._last)
        if active and consumer is not None:
            try:
                live = consumer.status()
                current.update(live)
            except Exception:
                pass
        return {"state": current.get("state", "inactive"), "code": current.get("code"),
                "consumedRevision": current.get("consumed_revision", current.get("consumedRevision")),
                "receivedRevision": current.get("received_revision", current.get("receivedRevision")),
                "pendingCount": current.get("pending_count", current.get("pendingCount", 0)),
                "sessionId": self._record.snapshot.state.session_id,
                "taskId": self._record.task_id, "memberId": self._record.member_id,
                "active": active}

    def snapshot(self) -> dict[str, Any]:
        """Detached, credential-free polling DTO (not the shared session snapshot)."""
        return dict(self.status())

    def reapprove(self, approval_handle: str) -> None:
        """Replace approval credentials only while idle and for identical identity."""
        with self._op_lock:
            with self._state_lock:
                self._invalidate_participant_tickets()
                if self._closed or self._active:
                    raise HostCollaborationError("busy")
                original = self._record
            host = self._host
            with host._lock:
                host._prune()
                candidate = host._approved.get(approval_handle) if isinstance(approval_handle, str) else None
            if candidate is None or candidate.root != original.root:
                raise HostCollaborationError("invalid")
            old_state, new_state = original.snapshot.state, candidate.snapshot.state
            if ((old_state.session_id, old_state.base_commit, old_state.target_version,
                 original.member_id, original.task_id, original.task.owner, original.task.goal,
                 original.task.scopes) !=
                (new_state.session_id, new_state.base_commit, new_state.target_version,
                 candidate.member_id, candidate.task_id, candidate.task.owner, candidate.task.goal,
                 candidate.task.scopes)):
                raise HostCollaborationError("stale")
            try:
                current_head = host._head_reader(original.root)
            except Exception:
                raise HostCollaborationError("invalid") from None
            if current_head != old_state.base_commit:
                raise HostCollaborationError("stale")
            with host._lock:
                if host._approved.get(approval_handle) is not candidate:
                    raise HostCollaborationError("invalid")
                host._approved.pop(approval_handle, None)
                candidate.approval_handle = None
                self._record = candidate

    def _fresh_approval_check(self, cancel_token: Any = None) -> None:
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        fresh = self._record.client.snapshot()
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        task = fresh.state.tasks[self._record.task_id]
        if (fresh.state.session_id, fresh.state.target_version, fresh.state.base_commit) != (
            self._record.snapshot.state.session_id, self._record.snapshot.state.target_version,
            self._record.snapshot.state.base_commit
        ) or (task.owner, task.goal, task.scopes) != (
            self._record.task.owner, self._record.task.goal, self._record.task.scopes
        ) or task.status not in _ACTIVE:
            raise HostCollaborationError("stale")

    def activate(self, workspace: Any, *, cancel_token: Any = None) -> None:
        with self._op_lock:
            with self._state_lock:
                self._invalidate_participant_tickets()
                if self._closed or self._active:
                    raise HostCollaborationError("invalid")
            record = self._record
            consumer = lease = None
            try:
                if cancel_token is not None:
                    cancel_token.raise_if_cancelled()
                snap = workspace.snapshot
                if Path(snap.source_root).resolve() != record.root or snap.source_head != record.snapshot.state.base_commit:
                    raise HostCollaborationError("stale")
                if self._host._head_reader(record.root) != record.snapshot.state.base_commit:
                    raise HostCollaborationError("stale")
                self._fresh_approval_check(cancel_token)
                if cancel_token is not None:
                    cancel_token.raise_if_cancelled()
                cursor_root = self._host._cursor_root.resolve(strict=False)
                if cursor_root == record.root or cursor_root.is_relative_to(record.root):
                    raise HostCollaborationError("storage")
                directory = _private_cursor_dir(self._host._cursor_root)
                name = hashlib.sha256("\0".join((str(record.root), record.snapshot.state.session_id,
                    record.snapshot.state.base_commit, record.snapshot.state.target_version,
                    record.member_id, record.task_id)).encode()).hexdigest()
                lease = _CheckpointLease(directory / (name + ".lock"))
                consumer = RevisionConsumer(record.endpoint, credential=record.credential,
                    member_id=record.member_id, checkpoint_path=directory / (name + ".json"),
                    initial_snapshot=record.snapshot)
                helper = NativeWorkerSafePoint(consumer, record.client.snapshot,
                    initial_snapshot=record.snapshot, member_id=record.member_id,
                    approved_task=record.task, replay_wait_timeout=5)
                with self._host._lock:
                    reset_pending = record.reset_cursor
                if reset_pending:
                    # The accepted snapshot must be bound at the first input
                    # boundary before this consumer is started or reset.
                    self._pending_reset_helper = helper
                else:
                    if cancel_token is not None:
                        cancel_token.raise_if_cancelled()
                    consumer.start()
                    if cancel_token is not None:
                        cancel_token.raise_if_cancelled()
                with self._state_lock:
                    self._consumer, self._safe_point, self._lease = consumer, helper, lease
                    self._active = True
            except OperationCancelledError:
                self._cleanup_partial(consumer, lease)
                raise
            except HostCollaborationError:
                self._cleanup_partial(consumer, lease)
                raise
            except Exception:
                self._cleanup_partial(consumer, lease)
                raise HostCollaborationError("invalid") from None

    def _cleanup_partial(self, consumer: Any, lease: Any) -> None:
        consumer_closed = consumer is None
        if consumer is not None:
            try:
                consumer.close()
                consumer_closed = True
            except Exception:
                consumer_closed = False
        lease_closed = lease is None
        if lease is not None and consumer_closed:
            try:
                lease.close()
                lease_closed = True
            except Exception:
                lease_closed = False
        if not consumer_closed or not lease_closed:
            # Keep ownership reachable and retryable; never report it released.
            with self._state_lock:
                self._consumer, self._lease = consumer, lease
                self._safe_point = None
                self._active = True

    def prepare(self, request: Any, workspace: Any, *, cancel_token: Any = None) -> Any:
        with self._op_lock:
            with self._state_lock:
                if not self._active or self._safe_point is None:
                    raise HostCollaborationError("invalid")
                helper, consumer = self._safe_point, self._consumer
            try:
                if cancel_token is not None:
                    cancel_token.raise_if_cancelled()
                with self._host._lock:
                    reset = self._record.reset_cursor
                if reset:
                    # Consent binds the displayed revision, never a silently fetched one.
                    self._fresh_approval_check(cancel_token)
                    approved = self._record.snapshot
                    bound = helper.accept_reset(approved, request, workspace, cancel_token=cancel_token)
                    # Reset persistence succeeded. Consume consent exactly once before
                    # any subsequent cancellation/start failure can cause a retry.
                    with self._host._lock:
                        self._record.reset_cursor = False
                    if cancel_token is not None:
                        cancel_token.raise_if_cancelled()
                    consumer.start()
                    if cancel_token is not None:
                        cancel_token.raise_if_cancelled()
                    prepared = helper.prepare(bound, workspace, cancel_token=cancel_token)
                else:
                    prepared = helper.prepare(request, workspace, cancel_token=cancel_token)
                with self._state_lock:
                    generation = self._generation
                return _PreparedHostInput(self, prepared, consumer, helper, self._record, generation)
            except OperationCancelledError:
                raise
            except Exception as exc:
                from collab_runtime.safe_point import SafePointError
                if isinstance(exc, SafePointError):
                    raise
                if isinstance(exc, HostCollaborationError):
                    raise
                raise HostCollaborationError("invalid") from None

    def deactivate(self) -> None:
        with self._op_lock:
            with self._state_lock:
                if not self._active:
                    return
                consumer, lease = self._consumer, self._lease
            try:
                # Capture terminal recovery state before close normalizes it.
                before = consumer.status() if consumer is not None else dict(self._last)
                if consumer is not None:
                    consumer.close()
                    after = consumer.status()
                else:
                    after = before
                recovered = before.get("state") in {"access_denied", "resnapshot_required", "protocol_error", "server_error"}
                cached = dict(after)
                if recovered:
                    cached["state"], cached["code"] = before.get("state"), before.get("code")
                with self._state_lock:
                    self._last = cached
                lease.close()
            except Exception:
                # Keep ownership state intact so a caller can retry cleanup.
                raise HostCollaborationError("storage") from None
            with self._state_lock:
                self._consumer = self._safe_point = self._lease = None
                self._active = False
                self._generation += 1

    def close(self) -> None:
        with self._op_lock:
            with self._state_lock:
                self._invalidate_participant_tickets()
            self.deactivate()
            with self._state_lock:
                self._closed = True

    def _invalidate_participant_tickets(self) -> None:
        self._participant_tickets.clear()

    def _prune_participant_tickets(self) -> None:
        now = time.monotonic()
        for key, (_, created, _, _) in tuple(self._participant_tickets.items()):
            if now - created >= 300.0:
                self._participant_tickets.pop(key, None)

    def preview_task_status(self, target_status: str) -> dict[str, Any]:
        """Review an own-task status change; this grants no pipeline acknowledgement."""
        if not isinstance(target_status, str) or target_status not in {"queued", "running", "waiting"}:
            raise ParticipantCommandError("invalid")
        with self._op_lock:
            with self._state_lock:
                if self._closed or self._active:
                    raise ParticipantCommandError("busy")
                record = self._record
                identity = (record.root, record.member_id, record.task_id, self.run_id,
                            record.snapshot.state.session_id, record.snapshot.state.base_commit,
                            record.snapshot.state.target_version)
                if identity != self._pinned_identity:
                    raise ParticipantCommandError("stale")
                generation = self._generation
            try:
                if self._host._head_reader(record.root) != record.snapshot.state.base_commit:
                    raise ParticipantCommandError("stale")
                fresh = record.client.snapshot()
                state = fresh.state
                task = state.tasks[record.task_id]
                if ((state.session_id, state.base_commit, state.target_version) !=
                    (record.snapshot.state.session_id, record.snapshot.state.base_commit,
                     record.snapshot.state.target_version) or task.owner != record.member_id or
                    task.status not in _ACTIVE):
                    raise ParticipantCommandError("stale")
                # HTTP may take long enough for the checked-out source to move.
                if self._host._head_reader(record.root) != record.snapshot.state.base_commit:
                    raise ParticipantCommandError("stale")
            except ParticipantCommandError:
                raise
            except Exception:
                raise ParticipantCommandError("stale") from None
            dto = {
                "ticketId": secrets.token_urlsafe(24), "runId": self.run_id,
                "projectRoot": str(record.root), "sessionId": state.session_id,
                "taskId": record.task_id, "memberId": record.member_id,
                "fromStatus": task.status, "targetStatus": target_status,
                "expectedRevision": fresh.revision, "contextHash": state.context.content_hash,
                "freshContextDiffersFromAccepted": (
                    None if self._accepted_binding is None else
                    state.context.content_hash != self._accepted_binding.state.context.content_hash
                ),
            }
            with self._state_lock:
                self._prune_participant_tickets()
                while len(self._participant_tickets) >= 4:
                    oldest = min(self._participant_tickets, key=lambda key: self._participant_tickets[key][1])
                    self._participant_tickets.pop(oldest, None)
                if (self._closed or self._active or self._record is not record or
                        self._generation != generation or self._pinned_identity != identity):
                    raise ParticipantCommandError("stale")
                self._participant_tickets[dto["ticketId"]] = (dict(dto), time.monotonic(), record, generation)
            return dto

    def confirm_task_status(self, ticket_id: str) -> dict[str, Any]:
        """Spend one reviewed ticket and issue exactly one CAS status command.

        This is not a native accepted-binding or cursor acknowledgement and
        does not change pipeline status. No post-commit snapshot is performed.
        """
        with self._op_lock:
            with self._state_lock:
                if self._closed or self._active:
                    raise ParticipantCommandError("busy")
                self._prune_participant_tickets()
                stored = self._participant_tickets.get(ticket_id) if isinstance(ticket_id, str) else None
                if stored is None:
                    raise ParticipantCommandError("expired")
                dto, created, ticket_record, ticket_generation = stored
                if time.monotonic() - created >= 300.0:
                    self._participant_tickets.pop(ticket_id, None)
                    raise ParticipantCommandError("expired")
                record = self._record
                identity = (record.root, record.member_id, record.task_id, self.run_id,
                            record.snapshot.state.session_id, record.snapshot.state.base_commit,
                            record.snapshot.state.target_version)
                if (ticket_record is not record or ticket_generation != self._generation or
                    identity != self._pinned_identity or (dto["runId"], dto["projectRoot"],
                    dto["sessionId"], dto["taskId"], dto["memberId"]) != (
                    self.run_id, str(record.root), record.snapshot.state.session_id,
                    record.task_id, record.member_id)):
                    raise ParticipantCommandError("stale")
                try:
                    source_head = self._host._head_reader(record.root)
                except Exception:
                    raise ParticipantCommandError("stale") from None
                if source_head != record.snapshot.state.base_commit:
                    raise ParticipantCommandError("stale")
                self._participant_tickets.pop(ticket_id, None)
            try:
                client = self._command_factory(record.endpoint, credential=record.credential)
                receipt = client.update_task_status(task_id=record.task_id,
                    status=dto["targetStatus"], expected_revision=dto["expectedRevision"])
            except TaskCommandError as exc:
                raise ParticipantCommandError(exc.code, outcome_uncertain=exc.outcome_uncertain) from None
            except Exception:
                raise ParticipantCommandError("protocol_error", outcome_uncertain=True) from None
            try:
                valid_revision = (isinstance(receipt.revision, str) and len(receipt.revision) == 40 and
                                  all(char in "0123456789abcdef" for char in receipt.revision))
                valid_receipt = (valid_revision and receipt.task_id == record.task_id and
                                 receipt.status == dto["targetStatus"])
            except Exception:
                valid_receipt = False
            if not valid_receipt:
                raise ParticipantCommandError("protocol_error", outcome_uncertain=True)
            return {"revision": receipt.revision, "taskId": receipt.task_id, "status": receipt.status}

    def discard_task_status(self, ticket_id: str) -> None:
        """Idempotently discard a local review ticket without network I/O."""
        with self._state_lock:
            if isinstance(ticket_id, str):
                self._participant_tickets.pop(ticket_id, None)


def _private_cursor_dir(path: Path) -> Path:
    try:
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode):
            raise ValueError()
        if os.name == "posix" and (parent.st_uid != os.geteuid() or parent.st_mode & 0o022):
            raise ValueError()
        # Existing trust root must not be a symlink or permissive; create exactly this
        # opt-in namespace once and never repair/modify an existing directory.
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError()
            if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise ValueError()
        else:
            path.mkdir(mode=0o700, parents=False, exist_ok=False)
            created = path.lstat()
            if not stat.S_ISDIR(created.st_mode) or stat.S_ISLNK(created.st_mode):
                raise ValueError()
            if os.name == "posix" and (created.st_uid != os.geteuid() or created.st_mode & 0o077):
                raise ValueError()
        return path
    except Exception:
        raise HostCollaborationError("storage") from None


__all__ = ["CollaborationHost", "HostCollaborationError", "NativeRunCollaboration",
           "ParticipantCommandError"]
