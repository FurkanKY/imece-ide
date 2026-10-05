"""Explicit loopback revision delivery with host-controlled durable acknowledgement.

The trusted host supplies an approved Snapshot and an existing checkpoint parent.
This module neither fetches/applies snapshots nor invokes application callbacks.
One writer per checkpoint is required; atomic replacement is not a power-loss
durability guarantee or protection against trusted same-user filesystem edits.
"""

from __future__ import annotations

import http.client
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
import threading
from typing import Any

from collab_runtime.coordinator import ReplayEvent, Snapshot
from collab_runtime.errors import ValidationError
from collab_runtime.models import (
    SessionState, canonical_json_bytes, parse_json_bytes, parse_state_dict,
    safe_id, sha_hex,
)

__all__ = ["RevisionConsumer"]

MAX_PENDING_EVENTS = 32
MAX_CHECKPOINT_BYTES = 4096
MAX_HEAD_BYTES = 64 * 1024
MAX_FRAME_BYTES = 64 * 1024 + 256
MAX_ERROR_BYTES = 64 * 1024
MAX_CHANGED_TASK_IDS = 512
SOCKET_TIMEOUT = 5.0
INITIAL_BACKOFF = 0.25
MAX_BACKOFF = 5.0

_URL_RE = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", re.ASCII)
_CREDENTIAL_RE = re.compile(r"[A-Za-z0-9_-]{32,256}", re.ASCII)
_HEADER_NAME_RE = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_STATUS_RE = re.compile(rb"HTTP/1\.0 [0-9]{3} [\x20-\x7e]*\r?\n")
_EVENT_KEYS = {"previous_revision", "revision", "context_changed", "changed_task_ids"}
_CHECKPOINT_KEYS = {
    "schema", "session_id", "base_commit", "target_version", "member_id",
    "consumed_revision",
}
_ERROR_CODES = {
    "access_denied", "stale_revision", "replay_unavailable", "invalid_request",
    "session_unavailable", "internal_error", "subscriber_limit",
}
_TERMINAL_STATES = {
    "access_denied", "resnapshot_required", "protocol_error", "server_error",
}


class _ProtocolError(Exception):
    """Internal signal; wire diagnostics are never exposed to callers."""


class _HeadReader:
    """Limit the entire response head during parsing, not after allocation."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.count = 0
        self.counting = True
        self.first = True

    def readline(self, size: int = -1) -> bytes:
        if not self.counting:
            return self.stream.readline(size)
        limit = MAX_HEAD_BYTES - self.count + 1
        if size >= 0:
            limit = min(limit, size)
        line = self.stream.readline(limit)
        self.count += len(line)
        if self.count > MAX_HEAD_BYTES:
            raise _ProtocolError()
        if self.first:
            self.first = False
            if line and not _STATUS_RE.fullmatch(line):
                raise _ProtocolError()
        else:
            if not line or not line.endswith(b"\n"):
                raise _ProtocolError()
            raw = line[:-1].removesuffix(b"\r")
            if raw:
                name, colon, value = raw.partition(b":")
                if not colon or not _HEADER_NAME_RE.fullmatch(name):
                    raise _ProtocolError()
                if any(byte < 32 and byte != 9 or byte == 127 for byte in value):
                    raise _ProtocolError()
        return line

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class _BoundedResponse(http.client.HTTPResponse):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fp = _HeadReader(self.fp)

    def _read_status(self) -> tuple[str, int, str]:
        version, status, reason = super()._read_status()
        if version != "HTTP/1.0" or not 200 <= status <= 599:
            raise _ProtocolError()
        return version, status, reason

    def begin(self) -> None:
        super().begin()
        self.fp.counting = False


def _snapshot_baseline(snapshot: Snapshot) -> tuple[tuple[str, str, str], str]:
    try:
        if not isinstance(snapshot, Snapshot) or not isinstance(snapshot.state, SessionState):
            raise ValueError()
        state = parse_state_dict(snapshot.state.to_dict())
        revision = sha_hex(snapshot.revision, "revision")
        return (state.session_id, state.base_commit, state.target_version), revision
    except (ValidationError, AttributeError, TypeError, ValueError, RecursionError):
        raise ValidationError("a valid host-approved schema1 snapshot is required.") from None


def _strict_json(raw: bytes) -> Any:
    try:
        return parse_json_bytes(raw, what="consumer data")
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise _ProtocolError() from None


def _error_object(raw: bytes) -> str:
    obj = _strict_json(raw)
    if not isinstance(obj, dict) or set(obj) != {"error"}:
        raise _ProtocolError()
    error = obj["error"]
    if not isinstance(error, dict) or set(error) != {"code", "message"}:
        raise _ProtocolError()
    code = error["code"]
    if not isinstance(code, str) or code not in _ERROR_CODES or not isinstance(error["message"], str):
        raise _ProtocolError()
    return code  # Neither the server's message nor its code enters status/errors.


def _revision_event(raw: bytes, event_id: str) -> ReplayEvent:
    obj = _strict_json(raw)
    try:
        if not isinstance(obj, dict) or set(obj) != _EVENT_KEYS:
            raise ValueError()
        previous = sha_hex(obj["previous_revision"], "previous revision")
        revision = sha_hex(obj["revision"], "revision")
        if revision != sha_hex(event_id, "event id") or previous == revision:
            raise ValueError()
        changed = obj["changed_task_ids"]
        if type(obj["context_changed"]) is not bool or not isinstance(changed, list):
            raise ValueError()
        if len(changed) > MAX_CHANGED_TASK_IDS:
            raise ValueError()
        ids = tuple(safe_id(value, "task id") for value in changed)
        if ids != tuple(sorted(set(ids))):
            raise ValueError()
        return ReplayEvent(previous, revision, obj["context_changed"], ids)
    except (ValidationError, TypeError, ValueError):
        raise _ProtocolError() from None


class RevisionConsumer:
    """A bounded inbox; only explicit safe-point operations persist its cursor.

    Public lifecycle operations serialize with each other. The worker uses only
    the separate condition lock, so joining it cannot create a lifecycle-lock
    cycle. ``peek`` and ``status`` are detached observations, not acknowledgements.
    """

    def __init__(
        self, base_url: str, *, credential: str, member_id: str,
        checkpoint_path: Path | str, initial_snapshot: Snapshot,
    ) -> None:
        match = _URL_RE.fullmatch(base_url) if isinstance(base_url, str) else None
        if match is None or int(match[1]) > 65535:
            raise ValidationError("an exact literal IPv4 loopback HTTP endpoint is required.")
        if not isinstance(credential, str) or not _CREDENTIAL_RE.fullmatch(credential):
            raise ValidationError("a valid local member credential is required.")
        try:
            member = safe_id(member_id, "member id")
        except ValidationError:
            raise ValidationError("a valid member id is required.") from None
        self._identity, baseline = _snapshot_baseline(initial_snapshot)
        try:
            self._checkpoint_path = Path(checkpoint_path).absolute()
            if not self._checkpoint_path.parent.is_dir():
                raise ValueError()
        except (OSError, TypeError, ValueError):
            raise ValidationError("an existing trusted checkpoint parent is required.") from None
        self._member_id = member
        self._port = int(match[1])
        self._credential = credential
        self._lifecycle = threading.Lock()
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._pending: list[ReplayEvent] = []
        self._last_event: ReplayEvent | None = None
        self._state = "stopped"
        self._code: str | None = None
        self._closed = False
        saved = self._load_checkpoint()
        if saved is None:
            self._persist_checkpoint(baseline)
        self._consumed = baseline if saved is None else saved
        self._received = self._consumed

    def _checkpoint_object(self, revision: str) -> dict[str, Any]:
        session_id, base_commit, target_version = self._identity
        return {
            "schema": 1, "session_id": session_id, "base_commit": base_commit,
            "target_version": target_version, "member_id": self._member_id,
            "consumed_revision": revision,
        }

    def _load_checkpoint(self) -> str | None:
        """No-follow, bounded regular-file read; missing is the only soft case."""
        fd: int | None = None
        try:
            try:
                before = self._checkpoint_path.lstat()
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(before.st_mode):
                raise ValueError()
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(self._checkpoint_path, flags | getattr(os, "O_CLOEXEC", 0))
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError()
            if info.st_size > MAX_CHECKPOINT_BYTES:
                raise ValueError()
            if os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.geteuid()):
                raise ValueError()
            with os.fdopen(fd, "rb") as stream:
                fd = None
                raw = stream.read(MAX_CHECKPOINT_BYTES + 1)
            if len(raw) > MAX_CHECKPOINT_BYTES:
                raise ValueError()
            obj = parse_json_bytes(raw, what="checkpoint")
            if not isinstance(obj, dict) or set(obj) != _CHECKPOINT_KEYS:
                raise ValueError()
            if type(obj["schema"]) is not int or obj["schema"] != 1:
                raise ValueError()
            revision = sha_hex(obj["consumed_revision"], "consumed revision")
            if obj != self._checkpoint_object(revision):
                raise ValueError()
            return revision
        except (OSError, ValidationError, ValueError, TypeError, RecursionError):
            raise ValidationError("the private revision checkpoint is invalid or unavailable.") from None
        finally:
            if fd is not None:
                os.close(fd)

    def _persist_checkpoint(self, revision: str) -> None:
        temporary: str | None = None
        fd: int | None = None
        try:
            self._load_checkpoint()  # Never overwrite a symlink/corrupt/foreign checkpoint.
            raw = canonical_json_bytes(self._checkpoint_object(revision))
            if len(raw) > MAX_CHECKPOINT_BYTES:
                raise ValueError()
            fd, temporary = tempfile.mkstemp(prefix=".revision-", dir=self._checkpoint_path.parent)
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._checkpoint_path)
            temporary = None
        except (OSError, ValidationError, ValueError, TypeError):
            raise ValidationError("the revision checkpoint could not be persisted.") from None
        finally:
            if fd is not None:
                os.close(fd)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _require_open(self) -> None:
        if self._closed:
            raise ValidationError("the revision consumer is closed.")

    def start(self) -> RevisionConsumer:
        with self._lifecycle:
            self._require_open()
            with self._condition:
                if self._state in _TERMINAL_STATES:
                    raise ValidationError("the revision consumer requires explicit recovery.")
                if self._worker is not None and self._worker.is_alive():
                    return self
                self._stop = threading.Event()
                self._state, self._code = "connecting", None
                self._worker = threading.Thread(target=self._run, name="revision-consumer", daemon=True)
                try:
                    self._worker.start()
                except RuntimeError:
                    self._worker = None
                    self._state = "stopped"
                    raise ValidationError("the revision worker could not be started.") from None
        return self

    def peek(self) -> tuple[ReplayEvent, ...]:
        with self._condition:
            return tuple(self._pending)

    @property
    def session_identity(self) -> tuple[str, str, str]:
        """Pinned ``(session_id, base_commit, target_version)`` identity."""
        return self._identity

    @property
    def member_id(self) -> str:
        """Trusted local checkpoint namespace; not subscription authentication."""
        return self._member_id

    def status(self) -> dict[str, Any]:
        with self._condition:
            return {
                "state": self._state, "consumed_revision": self._consumed,
                "received_revision": self._received, "pending_count": len(self._pending),
                "code": self._code,
            }

    def acknowledge_at_safe_point(self, revision: str) -> None:
        with self._lifecycle:
            self._require_open()
            try:
                sha_hex(revision, "revision")
            except ValidationError:
                raise ValidationError("a consumed or pending revision is required.") from None
            with self._condition:
                if revision == self._consumed:
                    return
                index = next((i for i, event in enumerate(self._pending) if event.revision == revision), None)
                if index is None:
                    raise ValidationError("a consumed or pending revision is required.")
                self._persist_checkpoint(revision)
                self._consumed = revision
                del self._pending[:index + 1]
                self._condition.notify_all()

    @staticmethod
    def _shutdown(sock: socket.socket | None) -> None:
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _stop_worker(self, preserve_terminal: bool = False) -> None:
        # Called with lifecycle ownership; the worker never needs that lock.
        with self._condition:
            self._stop.set()
            self._condition.notify_all()
            sock, worker = self._socket, self._worker
        self._shutdown(sock)
        if worker is not None:
            worker.join()
        with self._condition:
            self._worker = None
            if not preserve_terminal or self._state not in _TERMINAL_STATES:
                self._state, self._code = "stopped", None

    def reset_at_safe_point(self, snapshot: Snapshot) -> None:
        with self._lifecycle:
            self._require_open()
            self._stop_worker(preserve_terminal=True)
            identity, revision = _snapshot_baseline(snapshot)
            if identity != self._identity:
                raise ValidationError("the approved snapshot must match the pinned session identity.")
            with self._condition:
                self._persist_checkpoint(revision)
                self._consumed = self._received = revision
                self._pending.clear()
                self._last_event = None
                self._state, self._code = "stopped", None
                self._condition.notify_all()

    def close(self) -> None:
        with self._lifecycle:
            if self._closed:
                return
            self._stop_worker()
            with self._condition:
                self._closed = True
                self._state, self._code = "closed", None

    def _set_state(self, state: str, code: str | None = None) -> None:
        with self._condition:
            if not self._stop.is_set():
                self._state, self._code = state, code

    def _run(self) -> None:
        self._retry_delay = INITIAL_BACKOFF
        try:
            while not self._stop.is_set():
                with self._condition:
                    if len(self._pending) == MAX_PENDING_EVENTS:
                        self._state, self._code = "inbox_full", None
                        self._condition.wait_for(
                            lambda: self._stop.is_set() or len(self._pending) < MAX_PENDING_EVENTS
                        )
                    if self._stop.is_set():
                        return
                self._set_state("connecting")
                outcome = self._subscribe()
                if self._stop.is_set():
                    return
                if outcome == "inbox_full":
                    continue  # The socket is already closed before parking.
                if outcome in _TERMINAL_STATES:
                    self._set_state(outcome, outcome)
                    return
                self._set_state("retrying", "connection_retry")
                if self._stop.wait(self._retry_delay):
                    return
                self._retry_delay = min(MAX_BACKOFF, self._retry_delay * 2)
        except Exception:
            # A fixed terminal result, never a thread traceback with local/wire data.
            self._set_state("protocol_error", "protocol_error")

    @staticmethod
    def _response_headers(response: _BoundedResponse) -> dict[str, str]:
        headers: dict[str, str] = {}
        for name, value in response.getheaders():
            name = name.lower()
            if name in headers:
                raise _ProtocolError()
            headers[name] = value.strip()
        if response.version != 10 or headers.get("connection", "").lower() != "close":
            raise _ProtocolError()
        if any(name in headers for name in ("transfer-encoding", "content-encoding", "trailer", "upgrade")):
            raise _ProtocolError()
        return headers

    def _subscribe(self) -> str:
        connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=SOCKET_TIMEOUT)
        connection.response_class = _BoundedResponse
        response: _BoundedResponse | None = None
        sock: socket.socket | None = None
        interrupt_sock: socket.socket | None = None
        terminal_status: str | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(SOCKET_TIMEOUT)
            with self._condition:
                if self._stop.is_set():
                    return "retry"
                self._socket = sock  # Retain even after getresponse hands ownership to fp.
                cursor = self._received
            sock.connect(("127.0.0.1", self._port))
            if self._stop.is_set():
                return "retry"
            connection.sock = sock  # Literal AF_INET connection; no DNS/proxy/redirect path.
            connection.request(
                "POST", "/v1/subscribe",
                body=canonical_json_bytes({"after_revision": cursor}),
                headers={"Authorization": "Bearer " + self._credential,
                         "Content-Type": "application/json", "Connection": "close"},
            )
            # HTTPResponse may close the HTTPConnection's socket object while
            # its makefile reader is still active. Keep a distinct live handle
            # for lifecycle shutdown so Windows can interrupt that reader.
            interrupt_sock = sock.dup()
            with self._condition:
                if self._stop.is_set():
                    return "retry"
                self._socket = interrupt_sock
            response = connection.getresponse()
            headers = self._response_headers(response)
            if response.status in (401, 403):
                terminal_status = "access_denied"
            elif response.status == 410:
                terminal_status = "resnapshot_required"
            if response.status == 200:
                if headers.get("content-type", "").lower() != "text/event-stream" or "content-length" in headers:
                    raise _ProtocolError()
                self._set_state("streaming")
                return self._stream(response)
            if headers.get("content-type", "").lower() != "application/json":
                raise _ProtocolError()
            length_text = headers.get("content-length", "")
            if not re.fullmatch(r"[0-9]+", length_text, re.ASCII):
                raise _ProtocolError()
            digits = length_text.lstrip("0") or "0"
            if len(digits) > 5 or int(digits) > MAX_ERROR_BYTES:
                raise _ProtocolError()
            length = int(digits)
            raw = response.read(length)
            if len(raw) != length:
                raise _ProtocolError()
            code = _error_object(raw)
            if terminal_status is not None:
                expected_code = "access_denied" if terminal_status == "access_denied" else "replay_unavailable"
                if code != expected_code:
                    raise _ProtocolError()
                return terminal_status
            if 500 <= response.status <= 599:
                return "retry"
            return "protocol_error"
        except (http.client.RemoteDisconnected, TimeoutError, ConnectionError, OSError):
            return "protocol_error" if terminal_status is not None else "retry"
        except (http.client.HTTPException, _ProtocolError, ValueError, UnicodeError):
            return "protocol_error"
        finally:
            # Only this worker closes readers. Lifecycle shutdown interrupts them.
            try:
                if response is not None:
                    response.close()
            finally:
                try:
                    connection.close()
                finally:
                    try:
                        if sock is not None:
                            sock.close()
                    finally:
                        try:
                            if interrupt_sock is not None:
                                interrupt_sock.close()
                        finally:
                            with self._condition:
                                self._socket = None

    def _accept(self, event: ReplayEvent) -> None:
        with self._condition:
            if event.revision == self._received:
                if event != self._last_event:
                    raise _ProtocolError()
                return
            if event.previous_revision != self._received or event.revision == self._consumed:
                raise _ProtocolError()
            if any(old.revision == event.revision for old in self._pending):
                raise _ProtocolError()
            if len(self._pending) >= MAX_PENDING_EVENTS:
                raise _ProtocolError()
            self._pending.append(event)
            self._received = event.revision
            self._last_event = event
            self._condition.notify_all()

    def _stream(self, response: _BoundedResponse) -> str:
        fields: dict[str, str] = {}
        comment = False
        frame_bytes = 0
        while not self._stop.is_set():
            with self._condition:
                if len(self._pending) == MAX_PENDING_EVENTS:
                    return "inbox_full"
            line = response.readline(MAX_FRAME_BYTES + 1)
            if len(line) > MAX_FRAME_BYTES:
                raise _ProtocolError()
            if not line or not line.endswith(b"\n"):
                return "retry"  # Drop all partial frame data on EOF or reconnect.
            frame_bytes += len(line)
            if frame_bytes > MAX_FRAME_BYTES:
                raise _ProtocolError()
            text = line[:-1].removesuffix(b"\r").decode("utf-8", "strict")
            if "\r" in text or "\x00" in text:
                raise _ProtocolError()
            if text:
                if text.startswith(":"):
                    if fields:
                        raise _ProtocolError()
                    comment = True
                else:
                    name, colon, value = text.partition(":")
                    if comment or not colon or name not in {"event", "id", "data"} or name in fields:
                        raise _ProtocolError()
                    fields[name] = value.removeprefix(" ")
                continue
            if fields:
                name = fields.get("event")
                if name == "revision" and set(fields) == {"event", "id", "data"}:
                    self._accept(_revision_event(fields["data"].encode("utf-8"), fields["id"]))
                elif name in {"error", "resnapshot_required"} and set(fields) == {"event", "data"}:
                    raw = fields["data"].encode("utf-8")
                    if len(raw) > MAX_ERROR_BYTES:
                        raise _ProtocolError()
                    code = _error_object(raw)
                    if (name == "resnapshot_required") != (code == "replay_unavailable"):
                        raise _ProtocolError()
                    return "resnapshot_required" if name == "resnapshot_required" else "server_error"
                else:
                    raise _ProtocolError()
            self._retry_delay = INITIAL_BACKOFF
            fields.clear()
            comment = False
            frame_bytes = 0
        return "retry"
