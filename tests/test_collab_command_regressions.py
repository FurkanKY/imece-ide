"""Regressions for collab_runtime.commands -- the opt-in loopback task client.

TEST-ONLY. Nothing here changes production behaviour and nothing fakes the
client boundary: every protocol case is delivered by a real ``AF_INET``
listener on ``127.0.0.1`` and parsed by the real stdlib ``http.client`` parser,
the real bounded response-head reader and the real budget/validation code; only
the peer's bytes are scripted. The hub cases use real temporary bare git
repositories, the real ``GitStore``/``Coordinator`` and the real
``LoopbackServer``; the one place where the transport itself is instrumented is
labelled as such (real git, real sockets, one wrapped reply writer).

Commit-outcome semantics are the point of this module. ``transport._domain_failure``
maps ``GitOperationError`` to 503 *after* the hub may already have accepted the
push, so 500 and 503 must never be reported as known non-commits, while 401,
403, 400 and 409 can. ``SOCKET_TIMEOUT`` (5 s) is a per-socket connect and idle
timeout, not an overall deadline for a command; the single timeout test
shortens it through the module attribute purely to keep the suite fast.
"""

from __future__ import annotations

import builtins
import contextlib
import http.client
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from collab_runtime import commands, transport
from collab_runtime.client import LoopbackSnapshotClient
from collab_runtime.commands import LoopbackTaskClient, TaskCommandError, TaskStatusReceipt
from collab_runtime.consumer import MAX_ERROR_BYTES
from collab_runtime.coordinator import Coordinator
from collab_runtime.errors import CollabError, GitOperationError, ValidationError
from collab_runtime.models import build_initial_state, build_task, canonical_json_bytes
from collab_runtime.store import SESSION_BRANCH, STATE_PATH, GitStore
from collab_runtime.transport import LoopbackServer

OWNER, ALICE, BOB = "O" * 40, "A" * 40, "B" * 40
SHA = "a" * 40
SHA0 = "b" * 40
TASK_ID = "task"
SESSION_ID = "command-regressions"
METADATA_MESSAGE = "imece-collab: update session"
# Nothing a peer or a hub ever says may reach the caller's error surface.
PEER_DETAIL = "hub detail 10.0.0.5/gitrepo.git and a bearer token must never surface"
MAX_SUCCESS_BYTES = 256  # the command client's own success-body budget
MAX_HEAD_BYTES = 64 * 1024  # the shared response-head budget of the v1 contract
_JSON_CT = b"Content-Type: application/json\r\n"
_CLOSE = b"Connection: close\r\n"

FIXED_MESSAGES = {
    "access_denied": "task status update was denied.",
    "stale_revision": "the reviewed revision is stale; fetch a fresh snapshot.",
    "invalid_request": "the task status request was rejected.",
    "session_unavailable": "the collaboration session is unavailable.",
    "server_error": "the task status server failed.",
    "protocol_error": "the task status response violated the local protocol.",
    "outcome_unknown": "the task status outcome is unknown; fetch a fresh snapshot.",
    "connection_error": "the local task status endpoint could not be reached.",
}


# ------------------------------------------------------------------ the peer

_HANG_UP = object()
"""Script sentinel: read the whole command, then hang up with no response."""


class _Peer:
    """One real AF_INET listener on 127.0.0.1 that scripts one byte script per
    connection.

    This is a NEW helper, not the ``_OneShotPeer`` of
    ``tests/test_collab_commands.py`` (which is left untouched): every recv loop
    here is bounded and breaks on EOF, on a short read or on its own deadline
    instead of spinning, so a truncated or hostile peer can never hang a test. A
    script is ``bytes`` to send, ``_HANG_UP`` for a silent hang-up, or
    ``script(peer, connection, request)`` for a connection the test drives
    itself (a silent peer, for instance). After a normal script the peer drains
    until the CLIENT closes, which is how a socket the client failed to release
    would show up.
    """

    def __init__(self, *scripts: object) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.1)
        self.port = int(self._listener.getsockname()[1])
        self.scripts = list(scripts)
        self.requests: list[bytes] = []
        self.client_closed: list[bool] = []
        self.connections = 0
        self.errors: list[BaseException] = []
        self._stop = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._serve, name="task-command-peer", daemon=True
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except (TimeoutError, OSError):
                continue  # an accept-loop timeout, never a test failure
            index = self.connections
            self.connections += 1
            try:
                self._handle(connection, index)
            except (BrokenPipeError, ConnectionResetError):
                self.client_closed.append(True)  # the client hung up first
            except OSError:
                self.client_closed.append(False)
            except BaseException as exc:  # never silently dropped
                self.errors.append(exc)
            finally:
                with contextlib.suppress(OSError):
                    connection.close()
        with contextlib.suppress(OSError):
            self._listener.close()

    def _handle(self, connection: socket.socket, index: int) -> None:
        connection.settimeout(0.5)
        request = self._read_request(connection)
        self.requests.append(request)
        if not self.scripts:
            return
        script = self.scripts[min(index, len(self.scripts) - 1)]
        if script is _HANG_UP:
            return  # the finally block closes; nothing left to drain
        if callable(script):
            script(self, connection, request)
        elif script:
            connection.settimeout(1.0)
            with contextlib.suppress(OSError):
                connection.sendall(script)
        if connection.fileno() < 0:
            return  # the script closed its own side; nothing left to drain
        self.client_closed.append(self._drain(connection))

    @staticmethod
    def _read_request(connection: socket.socket) -> bytes:
        """Read one head + body with bounded EOF handling on every recv loop."""
        buffer = b""
        while b"\r\n\r\n" not in buffer and len(buffer) <= MAX_HEAD_BYTES:
            chunk = connection.recv(4096)
            if not chunk:
                break  # EOF mid-head: record the partial head, never spin
            buffer += chunk
        head, marker, body = buffer.partition(b"\r\n\r\n")
        if not marker:
            return buffer
        length = 0
        for line in head.split(b"\r\n"):
            name, colon, value = line.partition(b":")
            if colon and name.strip().lower() == b"content-length":
                with contextlib.suppress(ValueError):
                    length = int(value.strip())
        while len(body) < length:
            chunk = connection.recv(4096)
            if not chunk:
                break  # EOF mid-body
            body += chunk
        return head + b"\r\n\r\n" + body

    def wait_for_requests(self, count: int, timeout: float = 10.0) -> None:
        until = time.monotonic() + timeout
        while len(self.requests) < count and time.monotonic() < until:
            time.sleep(0.02)
        assert len(self.requests) == count, (
            f"captured {len(self.requests)} of {count} expected requests"
        )

    def _drain(self, connection: socket.socket, deadline: float = 2.0) -> bool:
        """True once the client closed its side of the connection."""
        until = time.monotonic() + deadline
        while time.monotonic() < until:
            try:
                if connection.recv(1) == b"":
                    return True
            except socket.timeout:
                continue
            except OSError:
                return False
            time.sleep(0.01)
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._thread.join(5)
        assert not self._thread.is_alive(), "peer accept loop outlived the test"
        with contextlib.suppress(OSError):
            self._listener.close()
        if self.errors:
            raise AssertionError(f"peer thread failed: {self.errors!r}")


@pytest.fixture
def peer():
    """Every peer a test builds is torn down in one finally block."""
    built: list[_Peer] = []

    def factory(*scripts: object) -> _Peer:
        built.append(_Peer(*scripts))
        return built[-1]

    try:
        yield factory
    finally:
        for made in reversed(built):
            made.close()


# ------------------------------------------------------------- wire builders


def _json(payload: object) -> bytes:
    return canonical_json_bytes(payload)


def _head(status: int, lines: bytes, body: bytes = b"", *, version: str = "HTTP/1.0") -> bytes:
    return f"{version} {status} Reply\r\n".encode() + lines + b"\r\n" + body


def _framed(status: int, body: bytes) -> bytes:
    lines = _JSON_CT + b"Content-Length: " + str(len(body)).encode() + b"\r\n" + _CLOSE
    return _head(status, lines, body)


def _wire(status: int, body: bytes = b"", *, extra: bytes = b"") -> bytes:
    """A well-framed v1-shaped response: HTTP/1.0, Connection: close, one JSON
    content type and an exact Content-Length."""
    return _framed(status, body) if not extra else _head(
        status,
        extra + _JSON_CT + b"Content-Length: " + str(len(body)).encode() + b"\r\n" + _CLOSE,
        body,
    )


def _ok_body() -> bytes:
    return _json({"revision": SHA})


def _success() -> bytes:
    return _framed(200, _ok_body())


def _error_body(code: object, message: str = PEER_DETAIL) -> bytes:
    return _json({"error": {"code": code, "message": message}})


def _error_wire(status: int, code: object) -> bytes:
    return _framed(status, _error_body(code))


def _silent(peer_: _Peer, connection: socket.socket, request: bytes) -> None:
    """A peer that reads the whole command and then says nothing at all."""
    with contextlib.suppress(OSError):
        time.sleep(0.4)


def _requests(peer_: _Peer) -> list[bytes]:
    peer_.close()  # join first, so the captures are complete before asserting
    assert peer_.errors == []
    return peer_.requests


def _client(peer_: _Peer, credential: str = ALICE) -> LoopbackTaskClient:
    return LoopbackTaskClient(peer_.base_url, credential=credential)


def _command(client: LoopbackTaskClient, status: str = "running", revision: str = SHA):
    return client.update_task_status(task_id=TASK_ID, status=status, expected_revision=revision)


def _rendered(error: BaseException) -> str:
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


def _lone_surrogate_revision() -> bytes:
    return b'{"revision":"\xed\xa0\x80' + b"a" * 39 + b'"}'


# ----------------------------------------- status / wire-code agreement matrix

# status, error code on the wire, client code, outcome_uncertain
_STATUS_MATRIX = [
    # The v1 command envelope: only these five error statuses exist, and 500/503
    # are the uncertain pair because the hub may already have published.
    pytest.param(401, "access_denied", "access_denied", False, id="401-authentication"),
    pytest.param(403, "access_denied", "access_denied", False, id="403-authorization"),
    pytest.param(400, "invalid_request", "invalid_request", False, id="400-invalid"),
    pytest.param(409, "stale_revision", "stale_revision", False, id="409-stale"),
    pytest.param(500, "internal_error", "server_error", True, id="500-uncertain"),
    pytest.param(503, "session_unavailable", "session_unavailable", True, id="503-uncertain"),
    # Status and code must agree: a well-formed envelope for the wrong outcome
    # is refused rather than believed.
    pytest.param(401, "internal_error", "protocol_error", True, id="401-internal-code"),
    pytest.param(401, "stale_revision", "protocol_error", True, id="401-stale-code"),
    pytest.param(403, "internal_error", "protocol_error", True, id="403-internal-code"),
    pytest.param(403, "session_unavailable", "protocol_error", True, id="403-session-code"),
    pytest.param(400, "access_denied", "protocol_error", True, id="400-denied-code"),
    pytest.param(400, "stale_revision", "protocol_error", True, id="400-stale-code"),
    pytest.param(409, "invalid_request", "protocol_error", True, id="409-invalid-code"),
    pytest.param(409, "access_denied", "protocol_error", True, id="409-denied-code"),
    pytest.param(409, "internal_error", "protocol_error", True, id="409-internal-code"),
    pytest.param(500, "access_denied", "protocol_error", True, id="500-denied-code"),
    pytest.param(500, "session_unavailable", "protocol_error", True, id="500-session-code"),
    pytest.param(503, "internal_error", "protocol_error", True, id="503-internal-code"),
    pytest.param(503, "access_denied", "protocol_error", True, id="503-denied-code"),
    # Statuses the command route never produces (replay, routing, envelopes).
    pytest.param(410, "replay_unavailable", "protocol_error", True, id="410-gone"),
    pytest.param(404, "invalid_request", "protocol_error", True, id="404-unknown-route"),
    pytest.param(405, "invalid_request", "protocol_error", True, id="405-method"),
    pytest.param(413, "invalid_request", "protocol_error", True, id="413-too-large"),
    pytest.param(415, "invalid_request", "protocol_error", True, id="415-media-type"),
    pytest.param(404, "internal_error", "protocol_error", True, id="404-internal-code"),
]


@pytest.mark.parametrize("status,wire_code,expected,uncertain", _STATUS_MATRIX)
def test_only_agreeing_status_and_code_envelopes_are_believed(
    peer, status, wire_code, expected, uncertain
):
    """A fixed envelope maps to its fixed code; anything else is a protocol
    violation. 401/403/400/409 are known non-commits, 500/503 are not."""
    server = peer(_error_wire(status, wire_code))
    with pytest.raises(TaskCommandError) as caught:
        _command(_client(server))
    error = caught.value
    assert (error.code, error.outcome_uncertain) == (expected, uncertain)
    assert str(error) == FIXED_MESSAGES[expected]
    assert len(_requests(server)) == 1
    assert server.connections == 1


def test_a_valid_success_envelope_is_the_only_shape_that_returns_a_receipt(peer):
    server = peer(_success())
    assert _command(_client(server)) == TaskStatusReceipt(SHA, TASK_ID, "running")
    head, body = _requests(server)[0].split(b"\r\n\r\n", 1)
    assert head.split(b"\r\n")[0] == b"POST /v1/task-status HTTP/1.1"
    assert body == _json({"expected_revision": SHA, "task_id": TASK_ID, "status": "running"})
    assert server.connections == 1


# ------------------------------------------------------ response-head violations

_OK = _ok_body()
_LENGTH_55 = b"Content-Length: 55\r\n"

_HEAD_MATRIX = [
    pytest.param(_head(200, b"Transfer-Encoding: chunked\r\n" + _JSON_CT + _LENGTH_55 + _CLOSE, _OK),
                 id="chunked"),
    pytest.param(_head(200, b"Content-Encoding: gzip\r\n" + _JSON_CT + _LENGTH_55 + _CLOSE, _OK),
                 id="content-encoding"),
    pytest.param(_head(200, b"Trailer: x-trailer\r\n" + _JSON_CT + _LENGTH_55 + _CLOSE, _OK),
                 id="trailer"),
    pytest.param(_head(200, b"Upgrade: h2c\r\n" + _JSON_CT + _LENGTH_55 + _CLOSE, _OK),
                 id="upgrade"),
    pytest.param(_head(200, _JSON_CT + _LENGTH_55 + _LENGTH_55 + _CLOSE, _OK),
                 id="duplicate-content-length"),
    pytest.param(_head(200, _JSON_CT + _JSON_CT + _LENGTH_55 + _CLOSE, _OK),
                 id="duplicate-content-type"),
    pytest.param(_head(200, b"Content-Length:\r\n 55\r\n" + _JSON_CT + _CLOSE, _OK),
                 id="folded-content-length"),
    pytest.param(_head(200, _JSON_CT + b"Content-Type:\r\n application/json\r\n" + _LENGTH_55 + _CLOSE, _OK),
                 id="folded-content-type"),
    pytest.param(_head(200, b"Content Length: 55\r\n" + _JSON_CT + _CLOSE, _OK),
                 id="space-in-header-name"),
    pytest.param(_head(200, b"Content-Length: 5\x015\r\n" + _JSON_CT + _CLOSE, _OK),
                 id="control-character-in-value"),
    pytest.param(_head(200, b"Content-Length: abc\r\n" + _JSON_CT + _CLOSE, _OK),
                 id="non-numeric-length"),
    pytest.param(_head(200, _JSON_CT + b"Content-Length: 0\r\n" + _CLOSE), id="zero-length"),
    pytest.param(_head(200, _JSON_CT + b"Content-Length: 257\r\n" + _CLOSE, _OK),
                 id="length-over-success-budget"),
    pytest.param(_head(200, _JSON_CT + b"Content-Length: " + b"9" * 40 + b"\r\n" + _CLOSE, _OK),
                 id="absurd-length-never-converted"),
    pytest.param(_head(200, _JSON_CT + _CLOSE, _OK), id="missing-content-length"),
    pytest.param(_head(200, b"Content-Type: text/plain\r\n" + _LENGTH_55 + _CLOSE, _OK),
                 id="wrong-content-type"),
    pytest.param(_head(200, _JSON_CT + _LENGTH_55), id="missing-connection-close"),
    pytest.param(_head(200, _JSON_CT + _LENGTH_55 + b"Connection: keep-alive\r\n", _OK),
                 id="keep-alive"),
    pytest.param(_head(200, _JSON_CT + _LENGTH_55 + _CLOSE, _OK, version="HTTP/1.1"),
                 id="http-1-1-version"),
    pytest.param(_head(100, _JSON_CT + _LENGTH_55 + _CLOSE, b""), id="informational-100"),
    pytest.param(_head(199, _JSON_CT + _LENGTH_55 + _CLOSE, b""), id="informational-199"),
    pytest.param(_head(204, _JSON_CT + _LENGTH_55 + _CLOSE, _OK), id="no-content-204"),
    pytest.param(_head(600, _JSON_CT + _LENGTH_55 + _CLOSE, _OK), id="status-600"),
    pytest.param(b"HTTP/1.0 200\r\n" + _JSON_CT + _LENGTH_55 + _CLOSE + b"\r\n" + _OK,
                 id="status-without-reason"),
    pytest.param(b"HTTP/1.0 200 Reply" + _JSON_CT + _LENGTH_55 + _CLOSE + b"\r\n" + _OK,
                 id="unterminated-status-line"),
    pytest.param(
        _head(200, _JSON_CT + _LENGTH_55 + _CLOSE + b"X-Pad: " + b"p" * (MAX_HEAD_BYTES + 32) + b"\r\n", _OK),
        id="head-over-budget",
    ),
    pytest.param(_head(200, _JSON_CT + _LENGTH_55 + _CLOSE, _OK[:10]), id="truncated-body"),
]


@pytest.mark.parametrize("response", _HEAD_MATRIX)
def test_response_head_violations_are_uncertain_protocol_errors(peer, response):
    """Anything wrong in the head is a local protocol violation, and because the
    request was already transmitted the commit outcome cannot be claimed known."""
    server = peer(response)
    with pytest.raises(TaskCommandError) as caught:
        _command(_client(server))
    error = caught.value
    assert (error.code, error.outcome_uncertain) == ("protocol_error", True)
    assert len(_requests(server)) == 1
    assert server.connections == 1


# ----------------------------------------------------- response-body violations

_ERROR_SHAPES = [
    pytest.param(_json({"error": {"code": "session_unavailable"}}), id="error-without-message"),
    pytest.param(_json({"error": {"code": "session_unavailable", "message": PEER_DETAIL, "extra": 1}}),
                 id="error-with-extra-key"),
    pytest.param(_json({"error": {"code": "not_a_v1_code", "message": PEER_DETAIL}}),
                 id="error-with-unknown-code"),
    pytest.param(_json({"error": {"code": 503, "message": PEER_DETAIL}}), id="error-code-not-text"),
    pytest.param(_json({"error": {"code": "session_unavailable", "message": None}}),
                 id="error-message-not-text"),
    pytest.param(_json({"error": "session_unavailable"}), id="error-not-an-object"),
    pytest.param(_json({"code": "session_unavailable"}), id="error-key-missing"),
    pytest.param(_json({"error": {"code": "session_unavailable", "message": PEER_DETAIL},
                        "revision": SHA}), id="error-with-sibling-key"),
    pytest.param(
        b'{"error":{"code":"session_unavailable","message":"' + PEER_DETAIL.encode() + b'"'
        + b',"error":{"code":"session_unavailable","message":"x"}}',
        id="duplicate-error-keys",
    ),
    pytest.param(b"{not json", id="error-not-json"),
]

_BODY_MATRIX = [
    pytest.param(_framed(200, b'{"revision":"not-a-sha"}'), id="revision-not-hex"),
    pytest.param(_framed(200, _json({"revision": 40})), id="revision-not-text"),
    pytest.param(_framed(200, _json({"revision": None})), id="revision-null"),
    pytest.param(_framed(200, _json({"revision": ["a" * 40]})), id="revision-not-scalar"),
    pytest.param(_framed(200, _json({"revision": SHA, "extra": 1})), id="extra-key"),
    pytest.param(_framed(200, _json({})), id="revision-missing"),
    pytest.param(_framed(200, _json([{"revision": SHA}])), id="envelope-not-an-object"),
    pytest.param(_framed(200, _json(SHA)), id="envelope-not-an-object-at-all"),
    pytest.param(_framed(200, b'{"revision":"' + b"a" * 40 + b'","revision":"' + b"a" * 40 + b'"}'),
                 id="duplicate-json-keys"),
    pytest.param(_framed(200, b'{"revision":"\xff\xfe"}'), id="invalid-utf8"),
    pytest.param(_framed(200, _lone_surrogate_revision()), id="unpaired-surrogate"),
    pytest.param(_framed(200, b"not json at all"), id="not-json"),
    pytest.param(_framed(200, b'{"revision":"' + b"a" * 40 + b'"} trailing'), id="trailing-garbage"),
    pytest.param(_framed(200, b'{"revision":NaN}'), id="non-standard-constant"),
    pytest.param(_framed(200, b'{"revision":"' + b"a" * 400 + b'"}'), id="body-over-budget"),
    pytest.param(_framed(200, _error_body("session_unavailable")), id="error-envelope-on-200"),
    pytest.param(_framed(503, _json({"revision": SHA})), id="success-envelope-on-503"),
    pytest.param(_framed(503, b""), id="error-body-empty"),
] + _ERROR_SHAPES


@pytest.mark.parametrize("response", _BODY_MATRIX)
def test_response_body_violations_are_uncertain_protocol_errors(peer, response):
    """Body, JSON and envelope-shape violations are all local protocol errors with
    an uncertain outcome; the peer never gets a second chance."""
    server = peer(response)
    with pytest.raises(TaskCommandError) as caught:
        _command(_client(server))
    error = caught.value
    assert (error.code, error.outcome_uncertain) == ("protocol_error", True)
    assert len(_requests(server)) == 1
    assert server.connections == 1


def _error_body_of_exactly(size: int) -> bytes:
    """A well-formed v1 error envelope whose canonical form is exactly `size`."""
    empty = _error_body("session_unavailable", message="")
    padding = size - len(empty)
    assert padding >= 0
    body = _error_body("session_unavailable", message="p" * padding)
    assert len(body) == size
    return body


@pytest.mark.parametrize("oversize", [False, True], ids=["at-budget", "one-past-budget"])
def test_error_envelope_bodies_are_bounded_by_the_shared_error_budget(peer, oversize):
    """MAX_ERROR_BYTES is the error-body ceiling: an envelope exactly at it is
    accepted, one byte past it is refused before the body is read."""
    body = _error_body_of_exactly(MAX_ERROR_BYTES + (1 if oversize else 0))
    server = peer(_framed(503, body))
    with pytest.raises(TaskCommandError) as caught:
        _command(_client(server))
    expected = ("protocol_error", True) if oversize else ("session_unavailable", True)
    assert (caught.value.code, caught.value.outcome_uncertain) == expected
    assert len(_requests(server)) == 1


# ------------------------------------------------------------- error surface


@pytest.mark.parametrize("status,wire_code,code", [
    (401, "access_denied", "access_denied"),
    (400, "invalid_request", "invalid_request"),
    (409, "stale_revision", "stale_revision"),
    (500, "internal_error", "server_error"),
    (503, "session_unavailable", "session_unavailable"),
])
def test_every_failure_is_a_fixed_sanitized_and_cause_free_error(peer, status, wire_code, code):
    """The message is the fixed text for the code, the peer never influences it,
    and no local exception cause or traceback detail escapes."""
    server = peer(_error_wire(status, wire_code))
    with pytest.raises(TaskCommandError) as caught:
        _command(_client(server))
    error = caught.value
    assert isinstance(error, CollabError)
    assert error.code == code
    assert str(error) == FIXED_MESSAGES[code]
    assert error.args == (FIXED_MESSAGES[code],)
    # `from None`: the underlying transport/domain error is suppressed, never
    # rendered and never reachable as a cause.
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    rendered = _rendered(error)
    for secret in (ALICE, PEER_DETAIL, SHA, "10.0.0.5", "gitrepo", "Bearer"):
        assert secret not in str(error), secret
        assert secret not in repr(error), secret
        assert secret not in rendered, secret
    assert len(_requests(server)) == 1


@pytest.mark.parametrize("code,flag", [
    pytest.param("not_a_code", True, id="unknown-code"),
    pytest.param("", False, id="empty-code"),
    pytest.param("Access_Denied", False, id="wrong-case-code"),
    pytest.param("access_denied", 1, id="int-flag"),
    pytest.param("access_denied", "yes", id="string-flag"),
    pytest.param("access_denied", None, id="none-flag"),
    pytest.param("access_denied", object(), id="arbitrary-object-flag"),
])
def test_the_task_command_error_constructor_is_closed_and_zero_io(code, flag):
    """The error type accepts only its own fixed codes and a real bool."""
    with pytest.raises(ValueError):
        TaskCommandError(code, outcome_uncertain=flag)
    with pytest.raises(TypeError):
        TaskCommandError(code, flag)  # outcome_uncertain is keyword-only
    fixed = TaskCommandError("outcome_unknown", outcome_uncertain=True)
    assert isinstance(fixed, CollabError)
    assert fixed.exit_code == 1
    assert str(fixed) == FIXED_MESSAGES["outcome_unknown"]
    assert TaskCommandError._MESSAGES == FIXED_MESSAGES
    assert set(vars(fixed)) == {"code", "outcome_uncertain", "message"}
    assert fixed.args == (FIXED_MESSAGES["outcome_unknown"],)


# ------------------------------------------------- one command, one connection


@pytest.mark.parametrize("script", [
    pytest.param(_success(), id="success"),
    pytest.param(_error_wire(403, "access_denied"), id="authorization"),
    pytest.param(_error_wire(409, "stale_revision"), id="stale"),
    pytest.param(_framed(200, _json({"revision": "x"})), id="bad-revision"),
    pytest.param(_framed(200, b"x" * (MAX_SUCCESS_BYTES + 1)), id="oversized-body"),
    pytest.param(_HANG_UP, id="hang-up-without-a-response"),
])
def test_each_call_sends_exactly_one_command_and_is_never_retried(peer, script):
    """One explicit call is exactly one HTTP command on exactly one connection:
    nothing is retried, resynced or re-sent automatically."""
    server = peer(script)
    try:
        _command(_client(server))
    except TaskCommandError:
        pass
    assert server.connections == 1
    assert len(_requests(server)) == 1


def test_failures_release_the_socket_the_command_opened(peer):
    """Successes and failures alike leave nothing behind: the peer sees the
    client hang up after every single response shape."""
    scripts = [
        _success(),
        _error_wire(401, "access_denied"),
        _error_wire(503, "session_unavailable"),
        _head(200, _JSON_CT + b"Content-Length: 55\r\n" + b"Connection: keep-alive\r\n", _OK),
        _framed(200, b"not json at all"),
    ]
    server = peer(*scripts)
    client = _client(server)
    client.update_task_status(task_id=TASK_ID, status="running", expected_revision=SHA)
    for _ in scripts[1:]:
        with pytest.raises(TaskCommandError):
            _command(client)
    assert len(_requests(server)) == len(scripts)
    assert server.client_closed == [True] * len(scripts)


def test_repeated_explicit_calls_use_fresh_connections_and_the_exact_fixed_request(peer):
    """Six explicit calls, six connections, byte-identical commands: no
    connection reuse, no hidden state and no descriptor growth."""
    server = peer(_success())
    client = _client(server)
    before = len(list(Path("/proc/self/fd").iterdir())) if Path("/proc/self/fd").is_dir() else 0
    receipts = [
        client.update_task_status(task_id=TASK_ID, status="running", expected_revision=SHA)
        for _ in range(6)
    ]
    requests = _requests(server)
    assert receipts == [TaskStatusReceipt(SHA, TASK_ID, "running")] * 6
    assert server.connections == 6
    assert len(requests) == 6
    assert all(request == requests[0] for request in requests)
    assert server.client_closed == [True] * 6
    head, body = requests[0].split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    assert lines[0] == b"POST /v1/task-status HTTP/1.1"
    assert f"Host: 127.0.0.1:{server.port}".encode() in lines
    assert b"Authorization: Bearer " + ALICE.encode() in lines
    assert b"Content-Type: application/json" in lines
    assert b"Connection: close" in lines
    assert b"Content-Length: " + str(len(body)).encode() in lines
    assert body == _json({"expected_revision": SHA, "task_id": TASK_ID, "status": "running"})
    for forbidden in (b"Cookie:", b"Set-Cookie:", b"Origin:", b"Referer:",
                      b"Proxy-Authorization:", b"X-Api-Key:", b"Last-Event-ID:",
                      b"Transfer-Encoding:", b"Expect:"):
        assert forbidden not in head, forbidden
    assert b"?" not in lines[0] and b"@" not in lines[0]
    assert ALICE.encode() not in body and b"127.0.0.1" not in body
    if before:
        after = len(list(Path("/proc/self/fd").iterdir()))
        assert after - before <= 2, f"descriptors grew from {before} to {after}"


def test_the_command_path_never_reads_a_snapshot_a_cursor_or_a_file(monkeypatch, peer):
    """The client has exactly one public operation and performs no implicit
    read: no snapshot, no cursor/replay and no filesystem access."""
    opened: list[str] = []
    real_open = builtins.open

    def recording_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def forbidden_snapshot(self):
        raise AssertionError("the task command read a snapshot")

    monkeypatch.setattr(builtins, "open", recording_open)
    monkeypatch.setattr(LoopbackSnapshotClient, "snapshot", forbidden_snapshot)
    surface = sorted(name for name in dir(LoopbackTaskClient) if not name.startswith("_"))
    assert surface == ["update_task_status"], surface
    server = peer(_success())
    assert _command(_client(server)) == TaskStatusReceipt(SHA, TASK_ID, "running")
    assert opened == [], f"the command touched files: {opened}"
    assert len(_requests(server)) == 1


def test_the_receipt_is_frozen_and_detached_from_the_response(peer):
    server = peer(_success(), _success())
    client = _client(server)
    receipt = _command(client)
    with pytest.raises(FrozenInstanceError):
        receipt.revision = "c" * 40
    with pytest.raises(FrozenInstanceError):
        receipt.status = "done"
    assert receipt == TaskStatusReceipt(SHA, TASK_ID, "running")
    assert hash(receipt) == hash(TaskStatusReceipt(SHA, TASK_ID, "running"))
    assert TaskStatusReceipt(SHA, TASK_ID, "running") != TaskStatusReceipt(SHA0, TASK_ID, "running")
    # A second, equal receipt built from a second response is an independent object.
    again = _command(client)
    assert again == receipt and again is not receipt
    assert len(_requests(server)) == 2


# --------------------------------------------------- before / after transmission


@pytest.mark.parametrize("how", ["closed-port", "monkeypatched-refusal"])
def test_transport_failures_before_transmission_are_known_noncommits(how, monkeypatch):
    """Nothing was transmitted, so the outcome is a known non-commit -- even when
    the underlying transport error carries sensitive text."""
    if how == "monkeypatched-refusal":
        class Refused:
            def __init__(self, *_args, **_kwargs):
                pass

            def settimeout(self, _value):
                pass

            def connect(self, _address):
                raise ConnectionRefusedError(PEER_DETAIL)

            def close(self):
                pass

        monkeypatch.setattr(socket, "socket", Refused)
        port = 1234
    else:
        # A real bound-then-closed loopback port: a genuine ECONNREFUSED.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
        probe.close()
    client = LoopbackTaskClient(f"http://127.0.0.1:{port}", credential=ALICE)
    with pytest.raises(TaskCommandError) as caught:
        _command(client)
    error = caught.value
    assert (error.code, error.outcome_uncertain) == ("connection_error", False)
    assert str(error) == FIXED_MESSAGES["connection_error"]
    assert error.__cause__ is None and error.__suppress_context__ is True
    assert PEER_DETAIL not in _rendered(error)


def test_a_peer_that_hangs_up_after_the_command_is_still_uncertain(peer):
    """The bytes left this machine, so an abrupt hang-up cannot be a known
    non-commit even though nothing came back."""
    server = peer(_HANG_UP)
    with pytest.raises(TaskCommandError) as caught:
        _command(_client(server))
    assert (caught.value.code, caught.value.outcome_uncertain) == ("outcome_unknown", True)
    # A clean FIN with no response at all, distinct from the reset of the drop case.
    assert isinstance(caught.value.__context__, http.client.RemoteDisconnected)
    assert len(_requests(server)) == 1


def test_idle_after_transmission_times_out_uncertainly_and_quickly(monkeypatch, peer):
    """The idle timeout is per-socket, not an overall deadline: the 5 s default is
    shortened here only to keep the suite fast, and the failure it produces is the
    uncertain one because the command had already been transmitted."""
    monkeypatch.setattr(commands, "SOCKET_TIMEOUT", 0.1)
    assert commands.SOCKET_TIMEOUT == 0.1
    server = peer(_silent)
    started = time.monotonic()
    with pytest.raises(TaskCommandError) as caught:
        _command(_client(server))
    elapsed = time.monotonic() - started
    assert (caught.value.code, caught.value.outcome_uncertain) == ("outcome_unknown", True)
    assert isinstance(caught.value.__context__, (OSError, http.client.HTTPException))
    assert elapsed < 3.0, f"the idle timeout did not bound the call ({elapsed:.2f}s)"
    assert len(_requests(server)) == 1


# ------------------------------------------------------ zero-I/O local validation


@pytest.mark.parametrize("url,credential", [
    pytest.param("http://localhost:1234", ALICE, id="hostname-not-literal"),
    pytest.param("http://127.0.0.2:1234", ALICE, id="non-loopback-address"),
    pytest.param("http://0.0.0.0:1234", ALICE, id="wildcard-address"),
    pytest.param("https://127.0.0.1:1234", ALICE, id="wrong-scheme"),
    pytest.param("http://127.0.0.1", ALICE, id="no-port"),
    pytest.param("http://127.0.0.1:0", ALICE, id="port-zero"),
    pytest.param("http://127.0.0.1:65536", ALICE, id="port-over-range"),
    pytest.param("http://127.0.0.1:-1", ALICE, id="negative-port"),
    pytest.param("http://127.0.0.1:1234/", ALICE, id="trailing-slash"),
    pytest.param("http://127.0.0.1:1234/v1", ALICE, id="path-component"),
    pytest.param("http://user@127.0.0.1:1234", ALICE, id="userinfo"),
    pytest.param("http://127.000.0.1:1234", ALICE, id="octal-host"),
    pytest.param("http://127.0.0.1:01234", ALICE, id="leading-zero-port"),
    pytest.param("http://127.0.0.1:1234 ", ALICE, id="trailing-space"),
    pytest.param("", ALICE, id="empty-url"),
    pytest.param("http://127.0.0.1:1234", "", id="empty-credential"),
    pytest.param("http://127.0.0.1:1234", "A" * 31, id="credential-too-short"),
    pytest.param("http://127.0.0.1:1234", "A" * 257, id="credential-too-long"),
    pytest.param("http://127.0.0.1:1234", "A" * 31 + "+/=", id="credential-not-url-safe"),
    pytest.param("http://127.0.0.1:1234", ALICE + " ", id="credential-trailing-space"),
    pytest.param("http://127.0.0.1:1234", ALICE + "\n", id="credential-newline"),
    pytest.param("http://127.0.0.1:1234", None, id="credential-not-text"),
    pytest.param("http://127.0.0.1:1234", ALICE.encode(), id="credential-bytes"),
    pytest.param(None, ALICE, id="url-not-text"),
    pytest.param(1234, ALICE, id="url-not-a-string"),
])
def test_the_constructor_refuses_bad_endpoints_and_credentials_without_io(monkeypatch, url, credential):
    def no_socket(*_args, **_kwargs):
        raise AssertionError("the constructor attempted network access")

    monkeypatch.setattr(socket, "socket", no_socket)
    with pytest.raises(ValidationError):
        LoopbackTaskClient(url, credential=credential)


@pytest.mark.parametrize("task_id,status,expected_revision", [
    pytest.param("../escape", "done", SHA, id="id-traversal"),
    pytest.param("task/child", "done", SHA, id="id-with-slash"),
    pytest.param("", "done", SHA, id="id-empty"),
    pytest.param("task id", "done", SHA, id="id-with-space"),
    pytest.param("_leading", "done", SHA, id="id-leading-underscore"),
    pytest.param(".dotfirst", "done", SHA, id="id-leading-dot"),
    pytest.param("task\x00null", "done", SHA, id="id-with-nul"),
    pytest.param("a" * 129, "done", SHA, id="id-over-limit"),
    pytest.param("a" * 200, "done", SHA, id="id-too-long"),
    pytest.param(None, "done", SHA, id="id-none"),
    pytest.param(7, "done", SHA, id="id-not-text"),
    pytest.param(TASK_ID, "DONE", SHA, id="status-uppercase"),
    pytest.param(TASK_ID, "blocked", SHA, id="status-unknown"),
    pytest.param(TASK_ID, "", SHA, id="status-empty"),
    pytest.param(TASK_ID, True, SHA, id="status-bool"),
    pytest.param(TASK_ID, None, SHA, id="status-none"),
    pytest.param(TASK_ID, ["done"], SHA, id="status-not-text"),
    pytest.param(TASK_ID, "done", "A" * 40, id="revision-uppercase"),
    pytest.param(TASK_ID, "done", "a" * 39, id="revision-too-short"),
    pytest.param(TASK_ID, "done", "a" * 41, id="revision-too-long"),
    pytest.param(TASK_ID, "done", "g" * 40, id="revision-not-hex"),
    pytest.param(TASK_ID, "done", "", id="revision-empty"),
    pytest.param(TASK_ID, "done", None, id="revision-none"),
    pytest.param(TASK_ID, "done", "a" * 39 + " b", id="revision-embedded-space"),
])
def test_local_parameter_forms_are_refused_before_any_socket_exists(
    monkeypatch, task_id, status, expected_revision
):
    """Bad task ids, statuses and revisions are refused locally: no socket is
    created and the message says nothing about which field was wrong."""
    def no_socket(*_args, **_kwargs):
        raise AssertionError("local validation attempted network access")

    monkeypatch.setattr(socket, "socket", no_socket)
    client = LoopbackTaskClient("http://127.0.0.1:1234", credential=ALICE)
    with pytest.raises(ValidationError) as caught:
        client.update_task_status(
            task_id=task_id, status=status, expected_revision=expected_revision
        )
    assert str(caught.value) == "task status command parameters are invalid."
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


@pytest.mark.parametrize("status", ["queued", "running", "waiting", "done"])
def test_every_documented_status_is_accepted_and_sent_verbatim(peer, status):
    server = peer(_success())
    receipt = _command(_client(server), status=status)
    assert receipt.status == status
    head, body = _requests(server)[0].split(b"\r\n\r\n", 1)
    assert head.split(b"\r\n")[0] == b"POST /v1/task-status HTTP/1.1"
    assert body == _json({"expected_revision": SHA, "task_id": TASK_ID, "status": status})


# ------------------------------------------------------- real hub and real git

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")


def _git(repo: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, check=True, text=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    return cp.stdout


def _new_commits(store: GitStore, after: str, before: str) -> int:
    """How many commits `after` adds on top of `before` on the session branch."""
    return int(_git(store.store_path, "rev-list", "--count", after, f"^{before}").strip())


def _total_commits(store: GitStore, head: str) -> int:
    return int(_git(store.store_path, "rev-list", "--count", head).strip())


def _commit_facts(store: GitStore, commit: str) -> dict:
    return {
        "parents": _git(store.store_path, "log", "-1", "--format=%P", commit).split(),
        "paths": _git(store.store_path, "ls-tree", "-r", "--name-only", commit).split(),
        "message": _git(store.store_path, "log", "-1", "--format=%s", commit).strip(),
    }


@pytest.fixture
def live(tmp_path):
    """A real hub, a real client store, one session, one task and a started
    server; the server is closed in one finally block."""
    hub = GitStore.create_bare(tmp_path / "hub.git", what="hub")
    store = GitStore(
        store=GitStore.create_bare(tmp_path / "store.git", what="store"), remote=str(hub)
    )
    revision = store.init_session(build_initial_state(
        session_id=SESSION_ID, target_version="command regressions", base_commit=SHA0,
    ))
    revision = store.upsert_task(build_task(
        task_id=TASK_ID, owner="alice", goal="keep the command honest", scopes=["a.txt"],
        status="queued", context_revision=revision,
    ), expected_revision=revision)
    coordinator = Coordinator(
        store, session_id=SESSION_ID, owner_id="owner",
        member_credentials={"owner": OWNER, "alice": ALICE, "bob": BOB},
    )
    server = LoopbackServer(coordinator)
    server.start()
    try:
        yield SimpleNamespace(
            hub=hub, store=store, coordinator=coordinator, server=server, revision=revision
        )
    finally:
        server.close()


def _spy_on(coordinator: Coordinator, calls: list) -> None:
    """Count the real hub commands the loopback server executes.

    The attempt is recorded BEFORE the real call so that a command which fails
    after publishing is still counted as exactly one execution.
    """
    real = coordinator.update_task_status

    def counting(credential, *, task_id, status, expected_revision):
        record = {
            "credential": credential, "task_id": task_id, "status": status,
            "expected_revision": expected_revision, "revision": None,
        }
        calls.append(record)
        record["revision"] = real(
            credential, task_id=task_id, status=status, expected_revision=expected_revision
        )
        return record["revision"]

    coordinator.update_task_status = counting


def _drop_task_responses(monkeypatch, dropped: list) -> None:
    """INSTRUMENTATION (labelled as such): wrap the real response writer so the
    FIRST successful task-status response is never written and its socket is
    closed abortively (SO_LINGER 0 -> RST). The real git publication, the real
    coordinator and the real listener all run; only the reply is lost."""
    real_send_json = transport._LoopbackRequestHandler._send_json

    def dropping_send_json(handler, status, payload, extra):
        if (
            not dropped
            and status == 200
            and handler.command == "POST"
            and handler.path == "/v1/task-status"
            and set(payload) == {"revision"}
        ):
            dropped.append(dict(payload))
            handler.close_connection = True
            connection = handler.connection
            with contextlib.suppress(OSError):
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            with contextlib.suppress(OSError):
                connection.close()
            return None
        return real_send_json(handler, status, payload, extra)

    monkeypatch.setattr(transport._LoopbackRequestHandler, "_send_json", dropping_send_json)


@needs_git
def test_a_committed_publication_whose_response_is_dropped_is_uncertain_and_applied_once(
    live, monkeypatch
):
    """The hub committed, the reply was lost: the task client must report
    outcome_unknown with outcome_uncertain True after exactly ONE command, and a
    separate explicit snapshot client must find exactly one metadata child."""
    calls: list = []
    _spy_on(live.coordinator, calls)
    dropped: list = []
    _drop_task_responses(monkeypatch, dropped)

    baseline = LoopbackSnapshotClient(live.server.base_url, credential=ALICE).snapshot()
    assert baseline.revision == live.revision
    before = live.store.remote_head()
    assert before == live.revision

    client = LoopbackTaskClient(live.server.base_url, credential=ALICE)
    with pytest.raises(TaskCommandError) as caught:
        client.update_task_status(
            task_id=TASK_ID, status="running", expected_revision=baseline.revision
        )
    error = caught.value
    # The reply was dropped, not malformed: uncertain, with no local cause shown.
    assert (error.code, error.outcome_uncertain) == ("outcome_unknown", True)
    assert str(error) == FIXED_MESSAGES["outcome_unknown"]
    assert error.__cause__ is None and error.__suppress_context__ is True
    # A real abortive close (SO_LINGER 0 -> RST), not a clean end-of-stream.
    assert isinstance(error.__context__, ConnectionResetError), type(error.__context__)
    assert ALICE not in _rendered(error)

    # Exactly one hub command ran, and the revision it returned is a real commit.
    assert len(calls) == 1, "the command was retried or duplicated"
    committed = calls[0]["revision"]
    assert calls[0]["credential"] == ALICE
    assert calls[0]["expected_revision"] == baseline.revision
    assert dropped == [{"revision": committed}], "the response was not dropped"

    # Same store, real git: exactly one new metadata child, one parent, one path.
    assert _new_commits(live.store, committed, before) == 1
    facts = _commit_facts(live.store, committed)
    assert facts["parents"] == [before]
    assert facts["paths"] == [STATE_PATH]
    assert facts["message"] == METADATA_MESSAGE
    assert live.store.remote_head() == committed  # the authoritative hub ref
    assert _git(live.store.store_path, "rev-parse", "--verify", SESSION_BRANCH).strip() == committed
    assert _git(live.hub, "rev-parse", "--verify", SESSION_BRANCH).strip() == committed

    # A separate, explicit read-only client confirms the single application.
    verifier = LoopbackSnapshotClient(live.server.base_url, credential=ALICE)
    after = verifier.snapshot()
    assert after.revision == committed
    assert after.state.tasks[TASK_ID].status == "running"
    assert after.state.tasks[TASK_ID].owner == "alice"
    assert verifier.snapshot().revision == committed, "the write was applied twice"
    assert _new_commits(live.store, live.store.remote_head(), before) == 1
    assert len(calls) == 1


@needs_git
def test_a_publication_failure_after_the_hub_advanced_stays_uncertain(live, monkeypatch):
    """transport._domain_failure maps GitOperationError to 503, which can follow
    a push the hub already accepted: the client must report session_unavailable
    with outcome_uncertain True and must not claim a known non-commit."""
    calls: list = []
    _spy_on(live.coordinator, calls)
    store = live.store
    real_publish = store.publish

    def failing_publish(state, *, expected_revision):
        revision = real_publish(state, expected_revision=expected_revision)
        raise GitOperationError("the hub accepted the push but the confirmation was lost")

    monkeypatch.setattr(store, "publish", failing_publish)
    baseline = LoopbackSnapshotClient(live.server.base_url, credential=ALICE).snapshot()
    before = store.remote_head()

    client = LoopbackTaskClient(live.server.base_url, credential=ALICE)
    with pytest.raises(TaskCommandError) as caught:
        client.update_task_status(
            task_id=TASK_ID, status="waiting", expected_revision=baseline.revision
        )
    assert (caught.value.code, caught.value.outcome_uncertain) == ("session_unavailable", True)
    assert str(caught.value) == FIXED_MESSAGES["session_unavailable"]
    assert caught.value.__cause__ is None
    assert "confirmation" not in _rendered(caught.value)

    # The hub really did advance, so a known-noncommit report would be a lie.
    assert len(calls) == 1
    advanced = store.remote_head()
    assert advanced != before
    assert _new_commits(store, advanced, before) == 1
    state = LoopbackSnapshotClient(live.server.base_url, credential=ALICE).snapshot()
    assert state.revision == advanced
    assert state.state.tasks[TASK_ID].status == "waiting"


@needs_git
@pytest.mark.parametrize("credential,task_id,revision_choice,code,uncertain", [
    pytest.param(BOB, TASK_ID, "current", "access_denied", False, id="wrong-owner"),
    pytest.param(ALICE, "absent-task", "current", "invalid_request", False, id="unknown-task"),
    pytest.param(ALICE, TASK_ID, "stale", "stale_revision", False, id="stale-revision"),
])
def test_known_rejections_publish_no_child_and_leave_the_hub_head_untouched(
    live, credential, task_id, revision_choice, code, uncertain
):
    """Every known rejection is a known non-commit: the authoritative hub ref, the
    commit count and the published state are all untouched."""
    store = live.store
    alice = LoopbackTaskClient(live.server.base_url, credential=ALICE)
    baseline = LoopbackSnapshotClient(live.server.base_url, credential=ALICE).snapshot().revision
    # One accepted command first, so that "current" and "stale" can differ.
    moved = alice.update_task_status(
        task_id=TASK_ID, status="running", expected_revision=baseline
    ).revision
    head_before = store.remote_head()
    assert head_before == moved
    revision = baseline if revision_choice == "stale" else moved

    client = LoopbackTaskClient(live.server.base_url, credential=credential)
    with pytest.raises(TaskCommandError) as caught:
        client.update_task_status(
            task_id=task_id, status="done", expected_revision=revision
        )
    assert (caught.value.code, caught.value.outcome_uncertain) == (code, uncertain)
    assert str(caught.value) == FIXED_MESSAGES[code]
    assert store.remote_head() == head_before
    assert _new_commits(store, head_before, baseline) == 1  # only the accepted one
    assert _total_commits(store, head_before) == 3
    after = LoopbackSnapshotClient(live.server.base_url, credential=ALICE).snapshot()
    assert after.revision == head_before
    assert after.state.tasks[TASK_ID].status == "running"


@needs_git
def test_each_accepted_command_publishes_exactly_one_metadata_child(live):
    """Accepted commands extend the session branch by exactly one single-parent
    metadata commit each, and the revisions form an unbroken chain."""
    client = LoopbackTaskClient(live.server.base_url, credential=ALICE)
    reader = LoopbackSnapshotClient(live.server.base_url, credential=ALICE)
    seen = [live.revision]
    for status in ("running", "waiting", "done"):
        receipt = client.update_task_status(
            task_id=TASK_ID, status=status, expected_revision=seen[-1]
        )
        assert receipt.revision != seen[-1]
        assert _new_commits(live.store, receipt.revision, seen[-1]) == 1
        facts = _commit_facts(live.store, receipt.revision)
        assert facts["parents"] == [seen[-1]]
        assert facts["paths"] == [STATE_PATH]
        assert facts["message"] == METADATA_MESSAGE
        assert live.store.remote_head() == receipt.revision
        seen.append(receipt.revision)
    final = reader.snapshot()
    assert final.revision == seen[-1]
    assert final.state.tasks[TASK_ID].status == "done"
    assert final.state.tasks[TASK_ID].owner == "alice"
    assert _total_commits(live.store, seen[-1]) == 5


@needs_git
def test_the_source_project_worktree_head_index_refs_and_config_are_never_touched(tmp_path, live):
    """Metadata commands are hub-only: the user's own repository keeps its WIP
    bytes, index, HEAD, config and refs across accepted and rejected commands."""
    source = tmp_path / "project"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(source, "config", "user.name", "Tester")
    _git(source, "config", "user.email", "tester@example.com")
    (source / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "init")
    (source / "tracked.txt").write_text("dirty WIP that must survive\n", encoding="utf-8")
    (source / "untracked.txt").write_text("scratch\n", encoding="utf-8")
    _git(source, "add", "untracked.txt")  # a staged WIP change as well

    def fingerprint() -> tuple:
        git_dir = source / ".git"
        files = {
            path.relative_to(source).as_posix(): path.read_bytes()
            for path in sorted(source.rglob("*"))
            if path.is_file() and path.relative_to(source).parts[0] != ".git"
        }
        refs = {
            path.relative_to(git_dir).as_posix(): path.read_bytes()
            for path in sorted((git_dir / "refs").rglob("*")) if path.is_file()
        }
        packed = git_dir / "packed-refs"
        return (
            (git_dir / "HEAD").read_bytes(),
            (git_dir / "config").read_bytes(),
            (git_dir / "index").read_bytes(),
            _git(source, "for-each-ref", "--format=%(refname) %(objectname)"),
            packed.read_bytes() if packed.exists() else None,
            refs,
            files,
        )

    before = fingerprint()
    client = LoopbackTaskClient(live.server.base_url, credential=ALICE)
    revision = live.revision
    for status in ("running", "waiting"):
        revision = client.update_task_status(
            task_id=TASK_ID, status=status, expected_revision=revision
        ).revision
    with pytest.raises(TaskCommandError):
        client.update_task_status(
            task_id=TASK_ID, status="done", expected_revision=live.revision
        )
    assert fingerprint() == before, "the command touched the user's own repository"
    assert _total_commits(live.store, revision) == 4