"""Regressions for collab_runtime.client -- the loopback snapshot client.

TEST-ONLY. This module adds no production behaviour and mocks no HTTP: every
protocol case is delivered by a real ``AF_INET`` listener on ``127.0.0.1`` and
parsed by the real ``http.client`` parser, the real bounded head reader and the
real budget/validation code. Only the peer's bytes are scripted.

The peer is a fixture-owned object: a failing assertion can never leak a
listener, a socket or a thread, and a peer thread that raises is surfaced as a
test error instead of being swallowed.

The real-server cases at the bottom need ``git`` and are skipped individually,
so the protocol matrix still runs on a machine without it.
"""

from __future__ import annotations

import contextlib
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

from collab_runtime.client import (
    MAX_SNAPSHOT_RESPONSE_BYTES,
    LoopbackSnapshotClient,
    SnapshotClientError,
)
from collab_runtime.coordinator import Coordinator
from collab_runtime.errors import ValidationError
from collab_runtime.models import (
    MAX_JSON_BYTES,
    MAX_TASK_GOAL_CHARS,
    build_context,
    build_initial_state,
    build_task,
    canonical_json_bytes,
    parse_json_bytes,
    parse_state_dict,
)
from collab_runtime.store import GitStore
from collab_runtime.transport import LoopbackServer

TOKEN = "A" * 40
REPLACEMENT = "Z" * 40
NEVER_CONFIGURED = "M" * 40
SESSION_ID = "client-regressions"
SHA0 = "a" * 40
REVISION = "b" * 40
MAX_HEAD_BYTES = 65536  # the shared response-head budget of the v1 contract
FIXED_MESSAGES = {
    "access_denied": "snapshot access was denied.",
    "session_unavailable": "the collaboration session is unavailable.",
    "server_error": "the snapshot server failed.",
    "protocol_error": "the snapshot response violated the local protocol.",
    "connection_error": "the local snapshot endpoint could not be reached.",
}


# ------------------------------------------------------------------ the peer


class _Peer:
    """One real AF_INET listener that replays a byte script per connection.

    Requests are captured whole (head + body), so a test can prove how many
    requests the client made, with which authority and with which credential.
    A script is bytes to send and then close, or a callable
    ``script(peer, connection, request)`` for a connection the test drives.
    After each response the peer drains until the CLIENT closes, which is how a
    socket the client forgot to release would show up.
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
        self._thread = threading.Thread(
            target=self._serve, name="snapshot-client-peer", daemon=True
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except (TimeoutError, OSError):
                continue
            index = self.connections
            self.connections += 1
            try:
                self._handle(connection, index)
            except (BrokenPipeError, ConnectionResetError):
                # The client hung up instead of reading: expected, not a bug.
                self.client_closed.append(True)
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
        if callable(script):
            script(self, connection, request)
        elif script:
            with contextlib.suppress(OSError):
                connection.sendall(script)
        if connection.fileno() < 0:
            return  # the script closed its own side; there is nothing to drain
        self.client_closed.append(self._drain(connection))

    @staticmethod
    def _read_request(connection: socket.socket) -> bytes:
        buffer = b""
        while b"\r\n\r\n" not in buffer and len(buffer) <= MAX_HEAD_BYTES:
            chunk = connection.recv(4096)
            if not chunk:
                break
            buffer += chunk
        head, _, body = buffer.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip() or b"0")
        while len(body) < length:
            chunk = connection.recv(4096)
            if not chunk:
                break
            body += chunk
        return head + b"\r\n\r\n" + body

    def wait_for_requests(self, count: int, timeout: float = 10.0) -> None:
        """Bounded wait until exactly `count` requests have been captured."""
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
        self._stop.set()
        self._thread.join(5)
        assert not self._thread.is_alive(), "peer accept loop outlived the test"
        with contextlib.suppress(OSError):
            self._listener.close()
        if self.errors:
            raise AssertionError(f"peer thread failed: {self.errors!r}")


@pytest.fixture
def peer():
    """Every peer a test builds is closed in one finally block."""
    built: list[_Peer] = []

    def factory(*scripts: object) -> _Peer:
        built.append(_Peer(*scripts))
        return built[-1]

    try:
        yield factory
    finally:
        for built_peer in reversed(built):
            built_peer.close()


# ------------------------------------------------------------- wire builders


def _wire(status: int, body: bytes = b"", *, extra: bytes = b"") -> bytes:
    return (
        f"HTTP/1.0 {status} Reply\r\n".encode()
        + extra
        + b"Content-Type: application/json\r\n"
        + b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )


def _raw(head: bytes, body: bytes = b"") -> bytes:
    """A response whose head is written out in full, framing bugs included."""
    return head + body


def _error_body(code: object, message: str = "peer text that must never surface") -> bytes:
    return canonical_json_bytes({"error": {"code": code, "message": message}})


def _denied(status: int, code: str = "access_denied") -> bytes:
    return _wire(status, _error_body(code))


def _client(peer: _Peer, credential: str = TOKEN) -> LoopbackSnapshotClient:
    return LoopbackSnapshotClient(peer.base_url, credential=credential)


@contextlib.contextmanager
def _failure(peer: _Peer, credential: str = TOKEN):
    with pytest.raises(SnapshotClientError) as excinfo:
        _client(peer, credential).snapshot()
    yield excinfo.value


def _requests(peer: _Peer) -> list[bytes]:
    peer.close()  # join first: the captures must be complete before asserting
    assert peer.errors == []
    return peer.requests


def _one_protocol_error(peer: _Peer, credential: str = TOKEN) -> SnapshotClientError:
    with _failure(peer, credential) as failure:
        pass
    assert failure.code == "protocol_error"
    assert len(_requests(peer)) == 1
    assert peer.connections == 1
    return failure


# ------------------------------------------------------------ size budgeting


def _raw_state(task_count: int, last_goal: int) -> dict:
    tasks = {
        f"task-{index:02d}": {
            "owner": "alice",
            "goal": "g" * (last_goal if index == task_count - 1 else 3500),
            "scopes": ["a.txt", "src/main.py"],
            "status": "running",
            "context_revision": "c" * 40,
        }
        for index in range(task_count)
    }
    return {
        "schema": 1,
        "session_id": SESSION_ID,
        "target_version": "client size budget",
        "base_commit": SHA0,
        "context": {"goal": "keep the wire budget honest", "decisions": [], "interfaces": {}},
        "tasks": tasks,
    }


def _state_of_canonical_size(target: int) -> dict:
    """A valid schema1 state dict whose CANONICAL form is exactly `target`
    bytes, reached from 17+ tasks with 3500-character goals and the last goal
    padded. Nothing bypasses a per-field or per-collection limit: the result is
    run through `parse_state_dict` and re-encoded to prove it."""
    for task_count in range(17, 40):
        padding = target - len(canonical_json_bytes(_raw_state(task_count, 3500)))
        last_goal = 3500 + padding
        if 0 <= last_goal <= MAX_TASK_GOAL_CHARS:
            state = _raw_state(task_count, last_goal)
            assert len(canonical_json_bytes(state)) == target
            assert len(canonical_json_bytes(parse_state_dict(state).to_dict())) == target
            return state
    raise AssertionError("no schema1 state reaches the requested canonical size")


_BASE_STATE = build_initial_state(
    session_id=SESSION_ID, target_version="client regressions", base_commit=SHA0
).to_dict()


def _envelope(state: dict, revision: str = REVISION) -> bytes:
    return canonical_json_bytes({"revision": revision, "state": state})


def _ok(state: dict | None = None) -> bytes:
    return _wire(200, _envelope(_BASE_STATE if state is None else state))


def test_state_at_the_canonical_limit_is_accepted_although_the_envelope_exceeds_64k(peer):
    """The 64 KiB limit applies to the canonical STATE. The envelope around it
    legitimately runs past 64 KiB, up to MAX_SNAPSHOT_RESPONSE_BYTES."""
    state = _state_of_canonical_size(MAX_JSON_BYTES)
    envelope = _envelope(state)
    assert len(canonical_json_bytes(state)) == MAX_JSON_BYTES
    assert MAX_JSON_BYTES < len(envelope) <= MAX_SNAPSHOT_RESPONSE_BYTES
    server = peer(_wire(200, envelope))
    result = _client(server).snapshot()
    assert result.revision == REVISION
    assert result.state.session_id == SESSION_ID
    assert len(canonical_json_bytes(result.state.to_dict())) == MAX_JSON_BYTES
    assert len(_requests(server)) == 1


def test_state_one_byte_over_the_canonical_limit_is_refused_although_the_envelope_is_within_budget(peer):
    """An envelope inside the transport budget that carries an over-limit
    canonical state is a protocol violation, not a truncated success."""
    state = _state_of_canonical_size(MAX_JSON_BYTES + 1)
    envelope = _envelope(state)
    assert len(canonical_json_bytes(state)) > MAX_JSON_BYTES
    assert len(envelope) <= MAX_SNAPSHOT_RESPONSE_BYTES
    _one_protocol_error(peer(_wire(200, envelope)))


def test_envelope_of_exactly_the_response_budget_is_accepted(peer):
    """Whitespace is legal JSON, so the aggregate envelope reaches the exact
    response budget without changing the state."""
    envelope = _envelope(_state_of_canonical_size(MAX_JSON_BYTES))
    body = envelope + b" " * (MAX_SNAPSHOT_RESPONSE_BYTES - len(envelope))
    assert len(body) == MAX_SNAPSHOT_RESPONSE_BYTES
    result = _client(peer(_wire(200, body))).snapshot()
    assert result.revision == REVISION
    assert len(canonical_json_bytes(result.state.to_dict())) == MAX_JSON_BYTES


def test_envelope_one_byte_over_the_response_budget_is_refused(peer):
    envelope = _envelope(_state_of_canonical_size(MAX_JSON_BYTES))
    body = envelope + b" " * (MAX_SNAPSHOT_RESPONSE_BYTES + 1 - len(envelope))
    assert len(body) == MAX_SNAPSHOT_RESPONSE_BYTES + 1
    _one_protocol_error(peer(_wire(200, body)))


# ------------------------------------------------- the optional JSON bound


def test_default_byte_limit_rejects_what_the_explicit_bound_accepts():
    """parse_json_bytes defaults to 64 KiB; only framing may widen it."""
    raw = b'"' + b"x" * (MAX_JSON_BYTES - 1) + b'"'
    assert len(raw) == MAX_JSON_BYTES + 1
    with pytest.raises(ValidationError):
        parse_json_bytes(raw, what="framing probe")
    assert parse_json_bytes(raw, what="framing probe", max_bytes=len(raw)) == "x" * (MAX_JSON_BYTES - 1)
    assert parse_json_bytes(
        raw, what="framing probe", max_bytes=MAX_SNAPSHOT_RESPONSE_BYTES
    ) == "x" * (MAX_JSON_BYTES - 1)
    assert parse_json_bytes(b"[]", what="probe") == []
    assert parse_json_bytes(b'"' + b"x" * (MAX_JSON_BYTES - 2) + b'"', what="probe")


def test_invalid_json_byte_limits_are_rejected():
    for invalid in (True, False, 1.0, 0, -1, MAX_SNAPSHOT_RESPONSE_BYTES + 1, 1 << 40, "65536", None):
        with pytest.raises(ValidationError):
            parse_json_bytes(b"[]", what="probe", max_bytes=invalid)


def test_extended_byte_limit_keeps_every_strict_guard():
    """Widening the byte budget relaxes framing and nothing else."""
    hostile = {
        "duplicate keys": b'{"a":1,"a":2}',
        "NaN constant": b'{"a":NaN}',
        "Infinity constant": b'{"a":Infinity}',
        "unpaired surrogate": b'{"a":"\\ud800"}',
        "invalid utf-8": b'{"a":"\xff\xfe"}',
        "trailing bytes": b'{"a":1} trailing',
        "deep nesting": b"[" * 20000 + b"]" * 20000,
    }
    for label, raw in hostile.items():
        assert len(raw) <= MAX_SNAPSHOT_RESPONSE_BYTES, label
        for budget in (MAX_JSON_BYTES, MAX_SNAPSHOT_RESPONSE_BYTES):
            with pytest.raises(ValidationError):
                parse_json_bytes(raw, what="probe", max_bytes=budget)
    over = b'"' + b"x" * MAX_SNAPSHOT_RESPONSE_BYTES + b'"'
    with pytest.raises(ValidationError):
        parse_json_bytes(over, what="probe", max_bytes=MAX_SNAPSHOT_RESPONSE_BYTES)


# --------------------------------------------------------- success payloads


def test_minimal_success_envelope_is_accepted(peer):
    server = peer(_ok())
    result = _client(server).snapshot()
    assert result.revision == REVISION
    assert result.state.session_id == SESSION_ID
    assert result.state.tasks == {}
    head, body = _requests(server)[0].split(b"\r\n\r\n", 1)
    assert body == b"{}"
    assert head.startswith(b"POST /v1/snapshot HTTP/1.1\r\n")


def test_malformed_success_payloads_are_protocol_errors(peer):
    cases = {
        "missing state": b'{"revision":"' + REVISION.encode() + b'"}',
        "missing revision": b'{"state":' + canonical_json_bytes(_BASE_STATE) + b"}",
        "unknown root key": b'{"revision":"' + REVISION.encode() + b'","state":'
        + canonical_json_bytes(_BASE_STATE) + b',"extra":1}',
        "revision not lowercase hex": _envelope(_BASE_STATE, REVISION.upper()),
        "revision too short": _envelope(_BASE_STATE, "b" * 39),
        "revision not a string": canonical_json_bytes({"revision": 1, "state": _BASE_STATE}),
        "state is a list": canonical_json_bytes({"revision": REVISION, "state": []}),
        "state schema 2": _envelope(dict(_BASE_STATE, schema=2)),
        "state schema true": _envelope(dict(_BASE_STATE, schema=True)),
        "state unknown field": _envelope(dict(_BASE_STATE, nickname="x")),
        "task unknown field": _envelope(
            dict(_BASE_STATE, tasks={"t-1": {
                "owner": "alice", "goal": "g", "scopes": ["a.txt"], "status": "running",
                "context_revision": "c" * 40, "estimate": 3}})
        ),
        "task bad status": _envelope(
            dict(_BASE_STATE, tasks={"t-1": {
                "owner": "alice", "goal": "g", "scopes": ["a.txt"], "status": "paused",
                "context_revision": "c" * 40}})
        ),
        "task bad scope": _envelope(
            dict(_BASE_STATE, tasks={"t-1": {
                "owner": "alice", "goal": "g", "scopes": ["../escape"], "status": "running",
                "context_revision": "c" * 40}})
        ),
    }
    for label, body in cases.items():
        _one_protocol_error(peer(_wire(200, body)))


def test_hostile_success_payload_bytes_are_protocol_errors(peer):
    good = _envelope(_BASE_STATE)
    cases = {
        "invalid utf-8": b'{"revision":"\xff\xfe","state":{}}',
        "duplicate root key": good[:-1] + b',"state":{}}',
        "NaN constant": b'{"revision":"' + REVISION.encode() + b'","state":NaN}',
        "unpaired surrogate": b'{"revision":"\\ud800","state":{}}',
        "trailing bytes": good + b" tail",
        "not json at all": b"{}and then some",
        "empty body": b"",
    }
    for label, body in cases.items():
        _one_protocol_error(peer(_wire(200, body)))


# ------------------------------------------------------------------ framing


def test_broken_framing_is_a_protocol_error_with_exactly_one_request(peer):
    body = _envelope(_BASE_STATE)
    length = str(len(body)).encode()
    many_headers = b"".join(b"X-Pad-%03d: v\r\n" % index for index in range(150))
    huge_head = b"".join(b"X-Pad-%03d: %s\r\n" % (index, b"a" * 680) for index in range(100))
    assert len(huge_head) > MAX_HEAD_BYTES
    ct = b"Content-Type: application/json\r\n"
    close = b"Connection: close\r\n"
    cases = {
        "missing content-length": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + close + b"\r\n", body),
        "zero content-length": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + b"Content-Length: 0\r\n" + close + b"\r\n"),
        "negative content-length": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + b"Content-Length: -2\r\n" + close + b"\r\n", body),
        "comma separated content-length": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + b"Content-Length: " + length + b", " + length + b"\r\n" + close + b"\r\n", body),
        "duplicated content-length": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + b"Content-Length: " + length + b"\r\nContent-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "signed content-length": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + b"Content-Length: +2\r\n" + close + b"\r\n", body),
        "content-length with junk": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + b"Content-Length: 2 x\r\n" + close + b"\r\n", body),
        "content-length far over budget": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + b"Content-Length: 999999999999999999999999999999999999\r\n" + close + b"\r\n"),
        "content-length over response budget": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + b"Content-Length: " + str(MAX_SNAPSHOT_RESPONSE_BYTES + 1).encode() + b"\r\n" + close + b"\r\n"),
        "missing content-type": _raw(b"HTTP/1.0 200 Reply\r\nContent-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "content-type with charset": _raw(b"HTTP/1.0 200 Reply\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "text content-type": _raw(b"HTTP/1.0 200 Reply\r\nContent-Type: text/plain\r\nContent-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "duplicated content-type": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + ct + b"Content-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "folded header": _raw(b"HTTP/1.0 200 Reply\r\nX-Note: first\r\n  second\r\n" + ct + b"Content-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "transfer-encoding": _raw(b"HTTP/1.0 200 Reply\r\nTransfer-Encoding: chunked\r\n" + ct + b"Content-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "content-encoding": _raw(b"HTTP/1.0 200 Reply\r\nContent-Encoding: gzip\r\n" + ct + b"Content-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "trailer header": _raw(b"HTTP/1.0 200 Reply\r\nTrailer: X-Late\r\n" + ct + b"Content-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "upgrade header": _raw(b"HTTP/1.0 200 Reply\r\nUpgrade: websocket\r\n" + ct + b"Content-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "missing connection close": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + b"Content-Length: " + length + b"\r\n\r\n", body),
        "connection keep-alive": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + b"Content-Length: " + length + b"\r\nConnection: keep-alive\r\n\r\n", body),
        "duplicated connection header": _raw(b"HTTP/1.0 200 Reply\r\n" + ct + b"Content-Length: " + length + b"\r\n" + close + close + b"\r\n", body),
        "http 1.1 response": _raw(b"HTTP/1.1 200 Reply\r\n" + ct + b"Content-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "hundred headers": _raw(b"HTTP/1.0 200 Reply\r\n" + many_headers + ct + b"Content-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "head over the budget": _raw(b"HTTP/1.0 200 Reply\r\n" + huge_head + ct + b"Content-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "status 100": _raw(b"HTTP/1.0 100 Continue\r\n" + ct + b"Content-Length: 0\r\n" + close + b"\r\n"),
        "status 201": _raw(b"HTTP/1.0 201 Reply\r\n" + ct + b"Content-Length: " + length + b"\r\n" + close + b"\r\n", body),
        "status 204": _raw(b"HTTP/1.0 204 Reply\r\n" + ct + b"Content-Length: 0\r\n" + close + b"\r\n"),
        "status 302 redirect": _raw(b"HTTP/1.0 302 Found\r\nLocation: http://127.0.0.1:1/v1/snapshot\r\n" + ct + b"Content-Length: 0\r\n" + close + b"\r\n"),
        "status 404": _raw(b"HTTP/1.0 404 Reply\r\n" + ct + b"Content-Length: 0\r\n" + close + b"\r\n"),
        "status 502": _raw(b"HTTP/1.0 502 Reply\r\n" + ct + b"Content-Length: 0\r\n" + close + b"\r\n"),
        "status 600": _raw(b"HTTP/1.0 600 Reply\r\n" + ct + b"Content-Length: 0\r\n" + close + b"\r\n"),
        "status line too long": _raw(b"HTTP/1.0 200 " + b"r" * (MAX_HEAD_BYTES + 16) + b"\r\n\r\n"),
        "garbage status line": _raw(b"NOT-HTTP AT ALL\r\n\r\n"),
        "head truncated": _raw(b"HTTP/1.0 200 Reply\r\n" + ct),
        "status line truncated": _raw(b"HTTP/1.0 2"),
    }
    for label, response in cases.items():
        failure = _one_protocol_error(peer(response))
        assert str(failure) == FIXED_MESSAGES["protocol_error"], label


def test_truncated_bodies_are_protocol_errors_and_never_denials(peer):
    """A short body is a framing violation. It must never be reported as the
    peer's denial class, which would tell a caller its credential is wrong."""
    cases = {
        "success body truncated": _raw(b"HTTP/1.0 200 Reply\r\nContent-Type: application/json\r\nContent-Length: 4096\r\nConnection: close\r\n\r\n", _envelope(_BASE_STATE)),
        "success body cut in half": _raw(b"HTTP/1.0 200 Reply\r\nContent-Type: application/json\r\nContent-Length: 128\r\nConnection: close\r\n\r\n", _envelope(_BASE_STATE)[:64]),
        "denial body truncated": _raw(b"HTTP/1.0 401 Denied\r\nContent-Type: application/json\r\nContent-Length: 4096\r\nConnection: close\r\n\r\n", _error_body("access_denied")),
        "denial body cut in half": _raw(b"HTTP/1.0 403 Denied\r\nContent-Type: application/json\r\nContent-Length: 96\r\nConnection: close\r\n\r\n", _error_body("access_denied")[:40]),
        "session unavailable truncated": _raw(b"HTTP/1.0 503 Unavailable\r\nContent-Type: application/json\r\nContent-Length: 4096\r\nConnection: close\r\n\r\n", _error_body("session_unavailable")),
        "internal error truncated": _raw(b"HTTP/1.0 500 Reply\r\nContent-Type: application/json\r\nContent-Length: 4096\r\nConnection: close\r\n\r\n", _error_body("internal_error")),
        "denial with empty body": _raw(b"HTTP/1.0 401 Denied\r\nContent-Type: application/json\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"),
        "denial with no framing": _raw(b"HTTP/1.0 401 Denied\r\nContent-Type: application/json\r\n"),
    }
    for label, response in cases.items():
        _one_protocol_error(peer(response))


# ------------------------------------------------- status / code agreement


def test_mismatched_status_and_error_codes_are_protocol_errors(peer):
    cases = {
        "401 with internal_error": _denied(401, "internal_error"),
        "401 with session_unavailable": _denied(401, "session_unavailable"),
        "403 with internal_error": _denied(403, "internal_error"),
        "403 with session_unavailable": _denied(403, "session_unavailable"),
        "403 with stale_revision": _denied(403, "stale_revision"),
        "503 with internal_error": _denied(503, "internal_error"),
        "503 with access_denied": _denied(503, "access_denied"),
        "500 with access_denied": _denied(500, "access_denied"),
        "500 with session_unavailable": _denied(500, "session_unavailable"),
        "401 with unknown code": _denied(401, "not_a_real_code"),
        "401 with non-string code": _denied(401, 7),
        "401 without message": _wire(401, canonical_json_bytes({"error": {"code": "access_denied"}})),
        "401 with extra error field": _wire(401, canonical_json_bytes({"error": {"code": "access_denied", "message": "x", "hint": "y"}})),
        "401 with root extras": _wire(401, canonical_json_bytes({"error": {"code": "access_denied", "message": "x"}, "trace": "t"})),
        "401 with a flat code": _wire(401, canonical_json_bytes({"code": "access_denied"})),
        "401 with a json list": _wire(401, b'["access_denied"]'),
        "401 with a truncated json body": _raw(b"HTTP/1.0 401 Denied\r\nContent-Type: application/json\r\nContent-Length: 40\r\nConnection: close\r\n\r\n", b'{"error":{"code":"access_den'),
        "401 with a body over the error budget": _raw(b"HTTP/1.0 401 Denied\r\nContent-Type: application/json\r\nContent-Length: 65537\r\nConnection: close\r\n\r\n", _error_body("access_denied")),
        "401 with a text body": _raw(b"HTTP/1.0 401 Denied\r\nContent-Type: application/json\r\nContent-Length: 5\r\nConnection: close\r\n\r\n", b"denied"),
    }
    for label, response in cases.items():
        _one_protocol_error(peer(response))


def test_error_codes_and_messages_are_fixed_local_text(peer):
    """The peer's code, message, headers and HTTP diagnostics never surface."""
    secret = "member-a7f3 line 41 of /srv/imece/hub.git"
    agreed = (
        (401, "access_denied", "access_denied"),
        (403, "access_denied", "access_denied"),
        (503, "session_unavailable", "session_unavailable"),
        (500, "internal_error", "server_error"),
    )
    for status, wire_code, local_code in agreed:
        body = canonical_json_bytes({"error": {"code": wire_code, "message": secret}})
        response = _raw(
            f"HTTP/1.0 {status} {secret}\r\n".encode()
            + b"X-Internal-Path: /srv/imece/hub.git\r\n"
            + b"Content-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        server = peer(response)
        with _failure(server) as failure:
            pass
        assert failure.code == local_code, (status, wire_code)
        assert str(failure) == FIXED_MESSAGES[local_code]
        rendered = f"{failure}|{failure.code}|{failure.args!r}|{failure.exit_code}"
        for leak in (secret, "/srv/imece/hub.git", TOKEN, str(server.port), "Traceback", "HTTP/1.0"):
            assert leak not in rendered, (status, leak)
        if wire_code != local_code:
            assert wire_code not in rendered, (status, wire_code)
        assert len(_requests(server)) == 1
    for code, message in FIXED_MESSAGES.items():
        assert SnapshotClientError(code).code == code
        assert str(SnapshotClientError(code)) == message
    with pytest.raises(ValueError):
        SnapshotClientError("internal_error")


# ----------------------------------------------------- transport behaviour


def test_bare_close_without_any_response_is_a_connection_error(peer):
    def hang_up(_server, connection, _request):
        connection.close()  # accept, read the request, answer nothing

    server = peer(hang_up)
    with _failure(server) as failure:
        pass
    assert failure.code == "connection_error"
    assert str(failure) == FIXED_MESSAGES["connection_error"]
    assert len(_requests(server)) == 1


@pytest.mark.parametrize("status", [200, 401])
def test_timeout_after_a_valid_status_is_protocol_failure_not_valid_denial(peer, monkeypatch, status):
    import collab_runtime.client as client_module

    release = threading.Event()

    def incomplete_head(_server, connection, _request):
        connection.sendall(
            f"HTTP/1.0 {status} Reply\r\nContent-Type: application/json\r\n".encode()
        )
        assert release.wait(2.0), "test must release the peer before cleanup"

    server = peer(incomplete_head)
    monkeypatch.setattr(client_module, "SOCKET_TIMEOUT", 0.2)
    try:
        with pytest.raises(SnapshotClientError) as caught:
            _client(server).snapshot()
        assert caught.value.code == "protocol_error"
        assert str(caught.value) == FIXED_MESSAGES["protocol_error"]
    finally:
        release.set()
    assert len(_requests(server)) == 1


def test_refused_listener_is_a_connection_error():
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    port = int(closed.getsockname()[1])
    closed.close()
    client = LoopbackSnapshotClient(f"http://127.0.0.1:{port}", credential=TOKEN)
    with pytest.raises(SnapshotClientError) as excinfo:
        client.snapshot()
    assert excinfo.value.code == "connection_error"
    assert str(excinfo.value) == FIXED_MESSAGES["connection_error"]


def test_no_name_resolution_or_proxy_is_ever_consulted(peer, monkeypatch):
    """A literal AF_INET socket, so DNS, proxy environment and redirect logic
    are all unreachable. The trap port proves nothing else was dialled."""
    trap = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    trap.bind(("127.0.0.1", 0))
    trap.listen(1)
    trap.settimeout(0)
    trap_port = int(trap.getsockname()[1])
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{trap_port}")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    def no_resolution(*_args, **_kwargs):
        raise AssertionError("the snapshot client must never resolve a name")

    monkeypatch.setattr(socket, "getaddrinfo", no_resolution)
    try:
        server = peer(_ok())
        assert _client(server).snapshot().revision == REVISION
        with pytest.raises(BlockingIOError):
            trap.accept()  # nothing was ever sent to the "proxy"
        requests = _requests(server)
    finally:
        with contextlib.suppress(OSError):
            trap.close()
    assert len(requests) == 1


def test_every_call_uses_one_fresh_authenticated_connection(peer):
    """One client object, three calls, three independent connections; the
    credential rides in the Authorization header every single time."""
    server = peer(_ok())
    client = _client(server)
    assert [client.snapshot().revision for _ in range(3)] == [REVISION] * 3
    requests = _requests(server)
    assert server.connections == 3
    assert len(requests) == 3
    for request in requests:
        head, body = request.split(b"\r\n\r\n", 1)
        assert head.startswith(b"POST /v1/snapshot HTTP/1.1\r\n")
        assert head.count(b"Authorization: Bearer " + TOKEN.encode()) == 1
        assert body == b"{}"
        assert request.count(TOKEN.encode()) == 1


def test_failures_are_never_retried(peer):
    """One script is installed, so a second request would show up as a second
    connection and a second capture."""
    cases = {
        "denial": _denied(403),
        "protocol": _wire(404),
        "truncated": _raw(b"HTTP/1.0 200 Reply\r\nContent-Type: application/json\r\nContent-Length: 512\r\nConnection: close\r\n\r\n", b"{}"),
    }
    for label, response in cases.items():
        server = peer(response)
        with _failure(server) as failure:
            pass
        assert failure.code in {"access_denied", "protocol_error"}, label
        requests = _requests(server)
        assert len(requests) == 1, label
        assert server.connections == 1, label


def test_request_shape_is_exact_and_carries_no_extra_state_or_surface(peer):
    server = peer(_ok())
    _client(server).snapshot()
    head, body = _requests(server)[0].split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    assert lines[0] == b"POST /v1/snapshot HTTP/1.1"
    assert f"Host: 127.0.0.1:{server.port}".encode() in lines
    assert b"Content-Length: 2" in lines
    assert b"Content-Type: application/json" in lines
    assert b"Connection: close" in lines
    assert body == b"{}"
    for forbidden in (b"Cookie:", b"Set-Cookie:", b"Origin:", b"Referer:",
                      b"Proxy-Authorization:", b"X-Api-Key:", b"Last-Event-ID:"):
        assert forbidden not in head, forbidden
    # No query string, no userinfo and no credential in the request line.
    assert b"?" not in lines[0] and b"@" not in lines[0]
    assert TOKEN.encode() not in lines[0] and TOKEN.encode() not in body
    # The v1 slice is read-only: no write, replay, subscribe or cookie surface.
    surface = [name for name in dir(LoopbackSnapshotClient) if not name.startswith("_")]
    assert surface == ["snapshot"], surface


def test_repeated_mixed_calls_release_every_socket(peer):
    """Successes and early failures alike leave nothing behind: the peer sees
    the client hang up every time."""
    ok, broken = peer(_ok()), peer(_wire(404))
    client = _client(ok)
    for _ in range(6):
        client.snapshot()
    for _ in range(6):
        with _failure(broken):
            pass
    assert len(_requests(ok)) == 6
    assert len(_requests(broken)) == 6
    assert len(ok.client_closed) == 6 and all(ok.client_closed)
    assert len(broken.client_closed) == 6 and all(broken.client_closed)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="descriptor accounting needs /proc")
def test_repeated_mixed_calls_do_not_accumulate_descriptors(peer):
    """Twenty calls across two listeners leave no client descriptor behind.

    The peers are closed before the final count, so their own listener and
    connection descriptors cannot mask a client-side leak."""
    ok, broken = peer(_ok()), peer(_wire(404))
    client = _client(ok)
    client.snapshot()
    with _failure(broken):
        pass
    ok.wait_for_requests(1)
    broken.wait_for_requests(1)
    before = len(list(Path("/proc/self/fd").iterdir()))
    for _ in range(10):
        client.snapshot()
        with _failure(broken):
            pass
    ok.wait_for_requests(11)
    broken.wait_for_requests(11)
    _requests(ok)
    _requests(broken)
    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after - before <= 4, f"descriptors grew from {before} to {after}"


def test_snapshot_is_detached_from_the_response_and_immutable(peer):
    sent = dict(_BASE_STATE, tasks={
        "t-one": {"owner": "alice", "goal": "g" * 1800, "scopes": ["a.txt"],
                  "status": "running", "context_revision": "c" * 40},
    })
    server = peer(_ok(sent))
    result = _client(server).snapshot()
    assert result.state.tasks["t-one"].goal == "g" * 1800
    # Mutating anything the caller can reach never writes back into the snapshot.
    exported = result.state.to_dict()
    exported["tasks"].clear()
    exported["session_id"] = "hijacked"
    assert result.state.session_id == SESSION_ID
    assert result.state.tasks["t-one"].goal == "g" * 1800
    with pytest.raises(TypeError):
        result.state.tasks["injected"] = None
    assert "injected" not in result.state.tasks
    again = _client(server).snapshot()
    assert again.state.to_dict() == result.state.to_dict()
    assert again.state is not result.state
    assert len(_requests(server)) == 2


# ------------------------------------------- real loopback server (needs git)


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")


def _live(tmp_path, credentials, name):
    """A real temporary hub + client store with one session and one task."""
    hub = GitStore.create_bare(tmp_path / f"hub-{name}", what="hub")
    store = GitStore(store=GitStore.create_bare(tmp_path / name, what="store"), remote=str(hub))
    revision = store.init_session(
        build_initial_state(session_id=SESSION_ID, target_version="client regressions", base_commit=SHA0)
    )
    revision = store.upsert_task(
        build_task(task_id="t-ui", owner="alice", goal="make the snapshot real", scopes=["a.txt"],
                   status="running", context_revision=revision),
        expected_revision=revision,
    )
    return store, Coordinator(
        store, session_id=SESSION_ID, owner_id="alice", member_credentials=credentials
    ), revision


@pytest.fixture
def live():
    """Started servers are closed in one finally block."""
    started: list[LoopbackServer] = []

    def factory(coordinator: Coordinator) -> LoopbackServer:
        server = LoopbackServer(coordinator)
        server.start()
        started.append(server)
        return server

    try:
        yield factory
    finally:
        for server in started:
            server.close()


@needs_git
def test_invalid_member_is_denied_before_any_git_read(tmp_path, live):
    """Authentication precedes the route and the state read, so a foreign
    credential cannot make the client touch the store."""
    store, coordinator, _revision = _live(tmp_path, {"alice": TOKEN}, "denied-store.git")
    reads = []
    real_fetch = store.fetch_state

    def counting_fetch():
        reads.append(True)
        return real_fetch()

    store.fetch_state = counting_fetch  # type: ignore[method-assign]
    server = live(coordinator)
    with pytest.raises(SnapshotClientError) as excinfo:
        LoopbackSnapshotClient(server.base_url, credential=NEVER_CONFIGURED).snapshot()
    assert excinfo.value.code == "access_denied"
    assert str(excinfo.value) == FIXED_MESSAGES["access_denied"]
    assert reads == []
    # A configured member on the same listener works, and reads exactly once.
    assert LoopbackSnapshotClient(server.base_url, credential=TOKEN).snapshot().revision
    assert len(reads) == 1


@needs_git
def test_replaced_listener_refuses_the_old_credential_and_serves_the_new_one(tmp_path, live):
    """Membership replaced on a fresh listener: the old credential is denied
    and the new one works, one request per call, on both listeners."""
    store, first, _revision = _live(tmp_path, {"alice": TOKEN}, "rotate-store.git")
    old_server = live(first)
    old_client = LoopbackSnapshotClient(old_server.base_url, credential=TOKEN)
    baseline = old_client.snapshot().revision
    old_server.close()

    with pytest.raises(SnapshotClientError) as stopped:
        old_client.snapshot()
    assert stopped.value.code == "connection_error"

    second = Coordinator(
        store, session_id=SESSION_ID, owner_id="alice", member_credentials={"alice": REPLACEMENT}
    )
    new_server = live(second)
    stale = LoopbackSnapshotClient(new_server.base_url, credential=TOKEN)
    with pytest.raises(SnapshotClientError) as denied:
        stale.snapshot()  # the endpoint is fresh; the credential is stale
    assert denied.value.code == "access_denied"
    result = LoopbackSnapshotClient(new_server.base_url, credential=REPLACEMENT).snapshot()
    assert result.revision == baseline
    assert result.state.session_id == SESSION_ID
    assert result.state.tasks["t-ui"].owner == "alice"


@needs_git
def test_good_member_snapshot_is_repeatable_immutable_and_never_cached(tmp_path, live):
    store, coordinator, revision = _live(tmp_path, {"alice": TOKEN}, "repeat-store.git")
    server = live(coordinator)
    client = LoopbackSnapshotClient(server.base_url, credential=TOKEN)
    first = client.snapshot()
    second = client.snapshot()
    assert first.revision == second.revision == revision
    assert first.state.to_dict() == second.state.to_dict()
    assert first.state is not second.state
    with pytest.raises(TypeError):
        first.state.tasks["injected"] = None
    # A publish is visible on the very next call: nothing is cached.
    store.publish(
        store.fetch_state()[1].with_context(build_context(goal="fresh", decisions=[], interfaces={})),
        expected_revision=revision,
    )
    third = client.snapshot()
    assert third.revision != revision
    assert third.state.context.goal == "fresh"
    assert first.state.context.goal == ""
