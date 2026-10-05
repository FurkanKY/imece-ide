from __future__ import annotations

import json
import socket
import threading

import pytest

from collab_runtime.client import LoopbackSnapshotClient, SnapshotClientError
from collab_runtime.errors import ValidationError
from collab_runtime.models import build_initial_state, canonical_json_bytes


TOKEN = "A" * 40


class Peer:
    def __init__(self, response: bytes):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.settimeout(0.1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.response = response
        self.requests = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._active = None
        self.failed = False
        self.thread = threading.Thread(target=self.serve, daemon=False)
        self.thread.start()

    def serve(self):
        try:
            while not self._stop.is_set():
                try:
                    connection, _ = self.listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        return
                    self.failed = True
                    return
                with self._lock:
                    self._active = connection
                try:
                    connection.settimeout(2)
                    raw = bytearray()
                    while b"\r\n\r\n" not in raw:
                        chunk = connection.recv(min(4096, 65537 - len(raw)))
                        if not chunk or len(raw) + len(chunk) > 65536:
                            raise ValueError()
                        raw.extend(chunk)
                    header, _, body = bytes(raw).partition(b"\r\n\r\n")
                    lengths = [line.split(b":", 1)[1].strip() for line in header.split(b"\r\n")
                               if line.lower().startswith(b"content-length:")]
                    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                        raise ValueError()
                    length = int(lengths[0])
                    if length > 65536 or len(body) > length:
                        raise ValueError()
                    while len(body) < length:
                        chunk = connection.recv(min(4096, length - len(body)))
                        if not chunk:
                            raise EOFError()
                        body += chunk
                    self.requests.append((header, body))
                    try:
                        connection.sendall(self.response)
                    except (BrokenPipeError, ConnectionResetError):
                        # The client may reject a malformed response and close early.
                        pass
                except (EOFError, OSError, TimeoutError, ValueError):
                    if not self._stop.is_set():
                        self.failed = True
                finally:
                    with self._lock:
                        self._active = None
                    try:
                        connection.close()
                    except OSError:
                        pass
        except Exception:
            # Keep thread failures observable without retaining peer diagnostics.
            self.failed = True
        finally:
            try:
                self.listener.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        with self._lock:
            active = self._active
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                active.close()
            except OSError:
                pass
        try:
            self.listener.close()
        except OSError:
            pass
        self.thread.join(3)
        assert not self.thread.is_alive(), "temporary peer did not stop"
        assert not self.failed, "temporary peer failed"


def wire(status: int, body: bytes, headers: bytes = b"Content-Type: application/json\r\n") -> bytes:
    return (f"HTTP/1.0 {status} Reply\r\n".encode() + headers +
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)


def test_constructor_is_configuration_only_and_endpoint_is_exact(monkeypatch):
    def no_io(*args, **kwargs):
        raise AssertionError("constructor performed I/O")
    monkeypatch.setattr(socket, "socket", no_io)
    client = LoopbackSnapshotClient("http://127.0.0.1:1234", credential=TOKEN)
    assert "A" * 40 not in repr(client)
    for endpoint in ("http://localhost:1234", "http://127.0.0.1:1234/", "http://127.0.0.1:1234?x",
                     "https://127.0.0.1:1234", "http://user@127.0.0.1:1234", "http://127.0.0.1:65536"):
        with pytest.raises(ValidationError):
            LoopbackSnapshotClient(endpoint, credential=TOKEN)
    for credential in ("x" * 31, "x" * 257, "x" * 32 + "!", "é" * 32):
        with pytest.raises(ValidationError):
            LoopbackSnapshotClient("http://127.0.0.1:1234", credential=credential)


def test_snapshot_request_is_exact_and_returns_detached_snapshot():
    state = build_initial_state(session_id="s1", target_version="v1", base_commit="a" * 40)
    envelope = {"revision": "b" * 40, "state": state.to_dict()}
    peer = Peer(wire(200, canonical_json_bytes(envelope)))
    try:
        client = LoopbackSnapshotClient(f"http://127.0.0.1:{peer.port}", credential=TOKEN)
        result = client.snapshot()
        peer.close()
        assert result.revision == "b" * 40
        assert result.state.session_id == "s1"
        assert type(result.state.tasks).__name__ == "mappingproxy"
        head, body = peer.requests[0]
        assert head.startswith(b"POST /v1/snapshot HTTP/1.1\r\n")
        assert b"Authorization: Bearer " + TOKEN.encode() in head
        assert b"Content-Type: application/json" in head
        assert b"Connection: close" in head
        assert body == b"{}"
    finally:
        peer.close()


@pytest.mark.parametrize("status,code,expected", [
    (403, "access_denied", "access_denied"),
    (401, "access_denied", "access_denied"),
    (503, "session_unavailable", "session_unavailable"),
    (500, "internal_error", "server_error"),
])
def test_fixed_error_mapping(status, code, expected):
    peer = Peer(wire(status, json.dumps({"error": {"code": code, "message": "secret peer text"}}).encode()))
    try:
        client = LoopbackSnapshotClient(f"http://127.0.0.1:{peer.port}", credential=TOKEN)
        with pytest.raises(SnapshotClientError) as exc:
            client.snapshot()
        assert exc.value.code == expected
        assert "secret peer text" not in str(exc.value)
        peer.close()
        assert len(peer.requests) == 1
    finally:
        peer.close()


@pytest.mark.parametrize("response", [
    b"HTTP/1.1 200 Reply\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
    b"HTTP/1.0 204 Reply\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    b"HTTP/1.0 200 Reply\r\nContent-Length: 2\r\nContent-Length: 2\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{}",
    b"HTTP/1.0 200 Reply\r\nContent-Length: 999999999999999999999999999999999999\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n",
])
def test_bad_framing_is_protocol_error_without_retry(response):
    peer = Peer(response)
    try:
        client = LoopbackSnapshotClient(f"http://127.0.0.1:{peer.port}", credential=TOKEN)
        with pytest.raises(SnapshotClientError) as exc:
            client.snapshot()
        assert exc.value.code == "protocol_error"
        peer.close()
        assert len(peer.requests) == 1
    finally:
        peer.close()


def test_malformed_status_and_headers_are_protocol_error():
    peer = Peer(b"HTTP/1.0 200 Reply\r\n" + b"X-Long: " + b"x" * 65540 + b"\r\n\r\n")
    try:
        client = LoopbackSnapshotClient(f"http://127.0.0.1:{peer.port}", credential=TOKEN)
        with pytest.raises(SnapshotClientError) as exc:
            client.snapshot()
        assert exc.value.code == "protocol_error"
    finally:
        peer.close()


def test_too_many_headers_with_large_body_is_protocol_error_and_peer_drains():
    headers = b"".join(b"X-Test: value\r\n" for _ in range(101))
    response = (b"HTTP/1.0 200 Reply\r\n" + headers +
                b"Content-Type: application/json\r\nContent-Length: 1000000\r\n"
                b"Connection: close\r\n\r\n" + b"x" * 1000000)
    peer = Peer(response)
    try:
        client = LoopbackSnapshotClient(f"http://127.0.0.1:{peer.port}", credential=TOKEN)
        with pytest.raises(SnapshotClientError) as exc:
            client.snapshot()
        assert exc.value.code == "protocol_error"
    finally:
        peer.close()
