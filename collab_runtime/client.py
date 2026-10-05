"""Finite, authenticated, read-only client for a native loopback snapshot endpoint."""

from __future__ import annotations

import http.client
import re
import socket
from types import MappingProxyType

from collab_runtime.consumer import (
    MAX_ERROR_BYTES, SOCKET_TIMEOUT, _BoundedResponse, _ProtocolError,
    _error_object, RevisionConsumer,
)
from collab_runtime.coordinator import Snapshot
from collab_runtime.errors import CollabError, ValidationError
from collab_runtime.models import (
    MAX_FRAMED_JSON_BYTES, MAX_JSON_BYTES, canonical_json_bytes, parse_json_bytes, parse_state_dict, sha_hex,
)

__all__ = ["LoopbackSnapshotClient", "SnapshotClientError", "MAX_SNAPSHOT_RESPONSE_BYTES"]

MAX_SNAPSHOT_RESPONSE_BYTES = MAX_FRAMED_JSON_BYTES
_URL_RE = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", re.ASCII)
_CREDENTIAL_RE = re.compile(r"[A-Za-z0-9_-]{32,256}", re.ASCII)


class SnapshotClientError(CollabError):
    """Fixed, safe snapshot-client failure; never contains peer diagnostics."""

    def __init__(self, code: str) -> None:
        messages = {
            "access_denied": "snapshot access was denied.",
            "session_unavailable": "the collaboration session is unavailable.",
            "server_error": "the snapshot server failed.",
            "protocol_error": "the snapshot response violated the local protocol.",
            "connection_error": "the local snapshot endpoint could not be reached.",
        }
        if code not in messages:
            raise ValueError("invalid local snapshot error code")
        self.code = code
        super().__init__(messages[code])


class LoopbackSnapshotClient:
    """Reusable configuration-only client; each call makes one fresh request."""

    def __init__(self, base_url: str, *, credential: str) -> None:
        match = _URL_RE.fullmatch(base_url) if isinstance(base_url, str) else None
        if match is None or int(match[1]) > 65535:
            raise ValidationError("an exact literal IPv4 loopback HTTP endpoint is required.")
        if not isinstance(credential, str) or not _CREDENTIAL_RE.fullmatch(credential):
            raise ValidationError("a valid local member credential is required.")
        self._port = int(match[1])
        self._credential = credential

    def snapshot(self) -> Snapshot:
        status_seen = False

        class SnapshotResponse(_BoundedResponse):
            def _read_status(self):
                nonlocal status_seen
                parsed = super()._read_status()
                status_seen = True
                return parsed

        connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=SOCKET_TIMEOUT)
        connection.response_class = SnapshotResponse
        sock: socket.socket | None = None
        response: _BoundedResponse | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(SOCKET_TIMEOUT)
            sock.connect(("127.0.0.1", self._port))
            connection.sock = sock
            connection.putrequest("POST", "/v1/snapshot")
            connection.putheader("Authorization", "Bearer " + self._credential)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Connection", "close")
            connection.putheader("Content-Length", "2")
            connection.endheaders(b"{}")
            response = connection.getresponse()
            headers = RevisionConsumer._response_headers(response)

            if response.status in (401, 403):
                self._read_error(response, headers, "access_denied")
            elif response.status == 503:
                self._read_error(response, headers, "session_unavailable")
            elif response.status == 500:
                self._read_error(response, headers, "server_error")
            elif response.status != 200:
                raise _ProtocolError()

            length = self._content_length(headers, MAX_SNAPSHOT_RESPONSE_BYTES, allow_zero=False)
            if headers.get("content-type", "").lower() != "application/json":
                raise _ProtocolError()
            raw = self._read_exact(response, length)
            obj = parse_json_bytes(raw, what="snapshot response", max_bytes=MAX_SNAPSHOT_RESPONSE_BYTES)
            if not isinstance(obj, dict) or set(obj) != {"revision", "state"}:
                raise _ProtocolError()
            revision = sha_hex(obj["revision"], "revision")
            state = parse_state_dict(obj["state"])
            if len(canonical_json_bytes(state.to_dict())) > MAX_JSON_BYTES:
                raise _ProtocolError()
            detached = type(state)(state.session_id, state.target_version, state.base_commit,
                                   state.context, MappingProxyType(dict(state.tasks)))
            return Snapshot(revision, detached)
        except SnapshotClientError:
            raise
        except (_ProtocolError, ValidationError, ValueError, TypeError, RecursionError):
            raise SnapshotClientError("protocol_error") from None
        except (OSError, http.client.RemoteDisconnected):
            raise SnapshotClientError("protocol_error" if status_seen else "connection_error") from None
        except http.client.HTTPException:
            # A peer that supplied a malformed response is a protocol failure,
            # not an unavailable endpoint. Truncated framed bodies are already
            # normalized to _ProtocolError by _read_exact.
            raise SnapshotClientError("protocol_error") from None
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

    @staticmethod
    def _content_length(headers: dict[str, str], maximum: int, *, allow_zero: bool) -> int:
        value = headers.get("content-length", "")
        if not value or not value.isascii() or not value.isdigit():
            raise _ProtocolError()
        normalized = value.lstrip("0") or "0"
        maximum_text = str(maximum)
        if len(normalized) > len(maximum_text) or (
            len(normalized) == len(maximum_text) and normalized > maximum_text
        ):
            raise _ProtocolError()
        length = int(normalized)
        if (not allow_zero and length == 0) or headers.get("content-type", "").lower() != "application/json":
            raise _ProtocolError()
        return length

    @staticmethod
    def _read_exact(response: _BoundedResponse, length: int) -> bytes:
        try:
            raw = response.read(length)
        except (OSError, http.client.HTTPException):
            raise _ProtocolError() from None
        if len(raw) != length:
            raise _ProtocolError()
        return raw

    def _read_error(self, response: _BoundedResponse, headers: dict[str, str], expected: str) -> None:
        length = self._content_length(headers, MAX_ERROR_BYTES, allow_zero=False)
        code = _error_object(self._read_exact(response, length))
        expected_wire = {"access_denied": "access_denied", "session_unavailable": "session_unavailable",
                         "server_error": "internal_error"}[expected]
        if code != expected_wire:
            raise _ProtocolError()
        raise SnapshotClientError(expected)
