"""Explicit, bounded task-status commands for native loopback participants.

This client is deliberately separate from the read-only snapshot client. A
caller supplies a revision it has already reviewed; commands never reconcile,
retry, or infer participant permissions locally.
"""

from __future__ import annotations

import http.client
import re
import socket
from dataclasses import dataclass

from collab_runtime.client import LoopbackSnapshotClient
from collab_runtime.consumer import (
    MAX_ERROR_BYTES, SOCKET_TIMEOUT, _BoundedResponse, _ProtocolError,
    _error_object, RevisionConsumer,
)
from collab_runtime.errors import CollabError, ValidationError
from collab_runtime.models import canonical_json_bytes, safe_id, sha_hex

__all__ = ["LoopbackTaskClient", "TaskStatusReceipt", "TaskCommandError"]

_URL_RE = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", re.ASCII)
_CREDENTIAL_RE = re.compile(r"[A-Za-z0-9_-]{32,256}", re.ASCII)
_STATUSES = frozenset(("queued", "running", "waiting", "done"))
_MAX_SUCCESS_BYTES = 256


@dataclass(frozen=True)
class TaskStatusReceipt:
    revision: str
    task_id: str
    status: str


class TaskCommandError(CollabError):
    """Sanitized command failure with explicit commit-outcome uncertainty."""

    _MESSAGES = {
        "access_denied": "task status update was denied.",
        "stale_revision": "the reviewed revision is stale; fetch a fresh snapshot.",
        "invalid_request": "the task status request was rejected.",
        "session_unavailable": "the collaboration session is unavailable.",
        "server_error": "the task status server failed.",
        "protocol_error": "the task status response violated the local protocol.",
        "outcome_unknown": "the task status outcome is unknown; fetch a fresh snapshot.",
        "connection_error": "the local task status endpoint could not be reached.",
    }

    def __init__(self, code: str, *, outcome_uncertain: bool) -> None:
        if code not in self._MESSAGES or type(outcome_uncertain) is not bool:
            raise ValueError("invalid local task command error")
        self.code = code
        self.outcome_uncertain = outcome_uncertain
        super().__init__(self._MESSAGES[code])


class LoopbackTaskClient:
    """Opt-in status command client; each explicit call sends exactly once."""

    def __init__(self, base_url: str, *, credential: str) -> None:
        match = _URL_RE.fullmatch(base_url) if isinstance(base_url, str) else None
        if match is None or int(match[1]) > 65535:
            raise ValidationError("an exact literal IPv4 loopback HTTP endpoint is required.")
        if not isinstance(credential, str) or not _CREDENTIAL_RE.fullmatch(credential):
            raise ValidationError("a valid local member credential is required.")
        self._port = int(match[1])
        self._credential = credential

    def update_task_status(
        self, *, task_id: str, status: str, expected_revision: str
    ) -> TaskStatusReceipt:
        try:
            task_id = safe_id(task_id, "task id")
            if not isinstance(status, str) or status not in _STATUSES:
                raise ValidationError("task status is invalid.")
            expected_revision = sha_hex(expected_revision, "expected revision")
            body = canonical_json_bytes({
                "expected_revision": expected_revision,
                "task_id": task_id,
                "status": status,
            })
        except (ValidationError, TypeError, ValueError, RecursionError):
            raise ValidationError("task status command parameters are invalid.") from None

        connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=SOCKET_TIMEOUT)
        connection.response_class = _BoundedResponse
        sock: socket.socket | None = None
        response: _BoundedResponse | None = None
        request_started = False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(SOCKET_TIMEOUT)
            sock.connect(("127.0.0.1", self._port))
            connection.sock = sock
            connection.putrequest("POST", "/v1/task-status")
            connection.putheader("Authorization", "Bearer " + self._credential)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Connection", "close")
            connection.putheader("Content-Length", str(len(body)))
            request_started = True
            connection.endheaders(body)
            response = connection.getresponse()
            headers = RevisionConsumer._response_headers(response)
            if response.status == 200:
                length = LoopbackSnapshotClient._content_length(
                    headers, _MAX_SUCCESS_BYTES, allow_zero=False
                )
                raw = LoopbackSnapshotClient._read_exact(response, length)
                from collab_runtime.models import parse_json_bytes

                obj = parse_json_bytes(raw, what="task command response", max_bytes=_MAX_SUCCESS_BYTES)
                if not isinstance(obj, dict) or set(obj) != {"revision"}:
                    raise _ProtocolError()
                revision = sha_hex(obj["revision"], "revision")
                return TaskStatusReceipt(revision, task_id, status)

            expected = {
                401: ("access_denied", "access_denied"),
                403: ("access_denied", "access_denied"),
                400: ("invalid_request", "invalid_request"),
                409: ("stale_revision", "stale_revision"),
                503: ("session_unavailable", "session_unavailable"),
                500: ("server_error", "internal_error"),
            }.get(response.status)
            if expected is None:
                raise _ProtocolError()
            length = LoopbackSnapshotClient._content_length(headers, MAX_ERROR_BYTES, allow_zero=False)
            code = _error_object(LoopbackSnapshotClient._read_exact(response, length))
            if code != expected[1]:
                raise _ProtocolError()
            # 503 also maps GitOperationError, including publication failures:
            # this envelope cannot prove that the hub did not accept the write.
            uncertain = response.status in (500, 503)
            raise TaskCommandError(expected[0], outcome_uncertain=uncertain) from None
        except TaskCommandError:
            raise
        except ValidationError:
            raise TaskCommandError(
                "protocol_error", outcome_uncertain=request_started
            ) from None
        except (_ProtocolError, ValueError, TypeError, UnicodeError, RecursionError):
            raise TaskCommandError("protocol_error", outcome_uncertain=request_started) from None
        except (OSError, http.client.HTTPException):
            if request_started:
                raise TaskCommandError("outcome_unknown", outcome_uncertain=True) from None
            raise TaskCommandError("connection_error", outcome_uncertain=False) from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            try:
                connection.close()
            except Exception:
                pass
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
