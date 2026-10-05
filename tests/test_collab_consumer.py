"""Bounded tests for collab_runtime.consumer (the native revision consumer v1).

Every wire interaction is a real socket to the literal IPv4 address 127.0.0.1:
a real LoopbackServer with real Git for arrival versus acknowledgement, readonly
peek, ordered prefix acknowledgement and its persistence-failure rollback,
restart/redelivery, the bounded full inbox and its finite commands, EOF
reconnect, live window expiry with the explicit reset, and the stop-and-replace
credential lifecycle; plus temporary scripted loopback peers, also over real
sockets, only for wire shapes the trusted listener cannot produce (bad status
lines, headers, framing, JSON and events, oversize head/frame/error data,
partial frames at EOF, duplicates, unknown fields and terminal error frames).
Only temp bare hub/store metadata and temp checkpoint directories are created
(base 'a'*40, no source checkout, no install, key, stage, commit or cleanup
outside tmp_path).

Covers the "Native revision consumer v1 contract" of
docs/COLLABORATION.md: the literal endpoint and host-approved snapshot rules,
checkpoint binding/privacy/atomicity, arrival versus acknowledgement, readonly
peek, ordered prefix acknowledgement and its persistence-failure rollback,
separate consumed/received cursors across reconnect and restart, the bounded
32-entry inbox with its park/resume, 410 and terminal-frame parking, the
explicit same-identity reset, retry backoff, 401 parking and credential
replacement, close/concurrent-closer/lock independence, and the absence of any
DNS, proxy, redirect or echo path.

No sleep drives correctness: every wait is a bounded poll of a real state or a
threading Event; every consumer, listener, peer socket and thread is released
in a fixture finally block.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from collab_runtime import store as store_module  # noqa: E402
from collab_runtime import consumer as consumer_module  # noqa: E402
from collab_runtime.consumer import (  # noqa: E402
    INITIAL_BACKOFF,
    MAX_BACKOFF,
    MAX_CHANGED_TASK_IDS,
    MAX_CHECKPOINT_BYTES,
    MAX_ERROR_BYTES,
    MAX_FRAME_BYTES,
    MAX_HEAD_BYTES,
    MAX_PENDING_EVENTS,
    SOCKET_TIMEOUT,
    RevisionConsumer,
    _TERMINAL_STATES,
)
from collab_runtime.coordinator import Coordinator, Snapshot  # noqa: E402
from collab_runtime.errors import ValidationError  # noqa: E402
from collab_runtime.models import SessionState, build_initial_state  # noqa: E402
from collab_runtime.store import GitStore  # noqa: E402
from test_collab_transport import (  # noqa: E402
    ALICE,
    BOB,
    CONTEXT,
    DAVE,
    OTHER,
    SESSION_ID,
    SHA0,
    _call,
    _json,
    _route_body,
    _silent,
    hub,  # noqa: F401 - reused by the wired fixture
    server,  # noqa: F401 - reused fixture
    servers,
    wired,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadı")

SUBSCRIBE = b"/v1/subscribe"
# The pinned state of the synthetic anchor: no Git area, no listener, one
# baseline revision that every scripted peer chains its first frame from.
ANCHOR = "b" * 40
NEXT = "c" * 40
AFTER = "d" * 40
FOREIGN = "e" * 40
CREDENTIAL = ALICE
BEARER = b"Authorization: Bearer " + ALICE.encode()
POSIX = os.name == "posix"
NOT_ROOT = POSIX and os.geteuid() != 0
# Bounded polls only: wide enough for a retry backoff plus a live observation
# interval, never a correctness sleep.
SETTLE = 25.0
# The ten state names the contract documents for status()["state"].
STATES = {
    "stopped", "connecting", "streaming", "retrying", "inbox_full", "access_denied",
    "resnapshot_required", "protocol_error", "server_error", "closed",
}


# ------------------------------------------------------------------ plumbing


def _wait_until(predicate, timeout=SETTLE, interval=0.01):
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _wait_for(consumer, predicate, what, timeout=SETTLE):
    assert _wait_until(lambda: predicate(consumer.status()), timeout), (
        what, consumer.status(), [event.revision for event in consumer.peek()],
    )


def _wait_state(consumer, state, timeout=SETTLE):
    _wait_for(consumer, lambda status: status["state"] == state, f"state {state}", timeout)


def _observe(consumer, seconds, interval=0.02):
    """Every state the consumer shows while nothing else touches it."""
    seen, deadline = set(), time.monotonic() + seconds
    while time.monotonic() < deadline:
        seen.add(consumer.status()["state"])
        time.sleep(interval)
    return seen


def _prompt(action, limit=3.0):
    """Run `action` and return how long it took, so a bound can be asserted."""
    started = time.monotonic()
    action()
    return time.monotonic() - started


def _assert_prompt(consumer, limit=3.0):
    assert _prompt(consumer.close, limit) < limit
    assert consumer._worker is None
    assert consumer.status() == {
        "state": "closed", "consumed_revision": consumer.status()["consumed_revision"],
        "received_revision": consumer.status()["received_revision"],
        "pending_count": len(consumer.peek()), "code": None,
    }


def _snapshot_at(revision):
    """A host-approved snapshot value for the already pinned session identity."""
    return Snapshot(
        revision=revision,
        state=build_initial_state(
            session_id=SESSION_ID, target_version="v0.1 consumer", base_commit=SHA0
        ),
    )


def _checkpoint(path):
    return Path(path).read_bytes()


def _cursor_revision(path):
    return json.loads(_checkpoint(path))["consumed_revision"]


def _published(legacy, count, start=None):
    """Publish `count` no-op commits through a second GitStore client.

    A no-op commit still advances the durable revision, so the replay stream
    carries exactly one event per commit without touching any task or context.
    """
    revision = start or legacy.remote_head()
    published = []
    for _ in range(count):
        revision, state = legacy.fetch_state()
        revision = legacy.publish(state, expected_revision=revision)
        published.append(revision)
    return published


def _legacy_store(wired, name="consumer-legacy.git"):
    path = GitStore.create_bare(wired.store.store_path.parent / name, what="store")
    return GitStore(store=path, remote=str(wired.hub))


def _assert_prefix(consumer, published):
    """The consumed cursor plus the inbox is one ordered, gap-free prefix."""
    events = consumer.peek()
    revisions = [consumer.status()["consumed_revision"]] + [e.revision for e in events]
    assert len(revisions) == len(set(revisions)), revisions
    for index, event in enumerate(events):
        assert event.previous_revision == revisions[index], event
    positions = [published.index(revision) for revision in revisions]
    assert positions == list(range(positions[0], positions[0] + len(positions))), positions


# ------------------------------------------------------------- checkpoint dir


@pytest.fixture
def home(tmp_path):
    """An existing trusted private parent directory for the durable cursor."""
    directory = tmp_path / "cursor-home"
    directory.mkdir(mode=0o700)
    return directory


@pytest.fixture
def cursor(home):
    return home / "revision-cursor.json"


@pytest.fixture
def anchor():
    """A valid host-approved schema1 snapshot that needs no Git area at all."""
    return _snapshot_at(ANCHOR)


def _close_all(built):
    for item in reversed(built):
        try:
            item.close()
        except Exception:  # noqa: BLE001 - cleanup must not mask a failure
            pass


@pytest.fixture
def draft(anchor, cursor):
    """Consumers of a scripted peer: synthetic snapshot, no Git, no listener."""
    built = []

    def factory(base_url, **kwargs):
        options = {
            "base_url": base_url, "credential": CREDENTIAL, "member_id": "alice",
            "checkpoint_path": cursor, "initial_snapshot": anchor,
        }
        options.update(kwargs)
        consumer = RevisionConsumer(**options)
        built.append(consumer)
        return consumer

    try:
        yield factory
    finally:
        _close_all(built)


@pytest.fixture
def live(wired, server, cursor):
    """Consumers of the real listener, each with a fresh host snapshot."""
    built = []

    def factory(base_url=None, **kwargs):
        options = {
            "base_url": server.base_url if base_url is None else base_url,
            "credential": CREDENTIAL, "member_id": "alice",
            "checkpoint_path": cursor,
            "initial_snapshot": kwargs.pop("initial_snapshot", None)
            or wired.coordinator.snapshot(ALICE),
        }
        options.update(kwargs)
        consumer = RevisionConsumer(**options)
        built.append(consumer)
        return consumer

    try:
        yield factory
    finally:
        _close_all(built)


@pytest.fixture
def wire():
    """Scripted literal-loopback peers; every socket and thread is released."""
    built = []

    def factory():
        peer = _ScriptedPeer()
        built.append(peer)
        return peer

    try:
        yield factory
    finally:
        for peer in reversed(built):
            peer.close()


# ------------------------------------------------------------ scripted peers


class _ScriptedPeer:
    """One temporary AF_INET listener that replays a byte script per connection.

    The consumer side stays real: a real socket, a real `http.client` parser and
    the real budget/framing code. Only the peer side is scripted, so a response
    the trusted listener would never send can still be delivered on a real wire.
    Each script is either bytes to send and then close, or a callable
    `script(peer, connection, request)` for a connection that must stay open.
    """

    def __init__(self, *scripts):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.1)
        self.port = int(self._listener.getsockname()[1])
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.scripts = list(scripts)
        self.requests: list[bytes] = []
        self.arrivals: list[float] = []
        self.connections = 0
        self.release = threading.Event()
        self._stop = threading.Event()
        self._current: socket.socket | None = None
        self._thread = threading.Thread(
            target=self._serve, name="scripted-peer", daemon=True
        )
        self._thread.start()

    def script(self, *scripts):
        """Install the per-connection script list; index 0 answers first."""
        self.scripts = list(scripts)
        return self

    def cursors(self):
        """The after_revision the consumer sent, in connection order."""
        return [
            json.loads(raw.split(b"\r\n\r\n", 1)[1].decode("utf-8"))["after_revision"]
            for raw in self.requests
        ]

    def paths(self):
        return [raw.split(b" ")[1] for raw in self.requests]

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except (TimeoutError, OSError):
                continue
            index = self.connections
            self.connections += 1
            self.arrivals.append(time.monotonic())
            self._current = connection
            try:
                connection.settimeout(0.5)
                request = self._read_request(connection)
                self.requests.append(request)
                script = self.scripts[min(index, len(self.scripts) - 1)] if self.scripts else b""
                if callable(script):
                    script(self, connection, request)
                elif script:
                    connection.sendall(script)
            except OSError:
                pass
            finally:
                self._current = None
                try:
                    connection.close()
                except OSError:
                    pass
        try:
            self._listener.close()
        except OSError:
            pass

    @staticmethod
    def _read_request(connection):
        buffer = b""
        while b"\r\n\r\n" not in buffer and len(buffer) <= 65536:
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

    def close(self):
        self.release.set()
        self._stop.set()
        current = self._current
        if current is not None:
            try:
                current.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._thread.join(timeout=5.0)


# ------------------------------------------------------------- wire builders


def _head(status=b"200 OK", *, version=b"HTTP/1.0", lines=(b"Content-Type: text/event-stream",
                                                           b"Connection: close"), body=b""):
    return b" ".join((version, status)) + b"\r\n" + b"\r\n".join(lines) + b"\r\n\r\n" + body


def _padded_head(total):
    """A complete, parseable 200 event-stream head of exactly `total` bytes."""
    lines = [b"HTTP/1.0 200 OK", b"Content-Type: text/event-stream", b"Connection: close"]
    fixed = b"\r\n".join(lines) + b"\r\n"
    padding = total - len(fixed) - len(b"X-Pad: ") - len(b"\r\n\r\n")
    assert padding >= 1
    head = fixed + b"X-Pad: " + b"p" * padding + b"\r\n\r\n"
    assert len(head) == total, (len(head), total)
    return head


def _error_body(code, message="a fixed message"):
    return json.dumps({"error": {"code": code, "message": message}}).encode("utf-8")


def _raw_json_head(status, body):
    """A denial whose declared length always matches the bytes on the wire."""
    return _head(
        status,
        lines=(b"Content-Type: application/json", b"Connection: close",
               b"Content-Length: %d" % len(body)),
        body=body,
    )


def _json_head(status, code, message="a fixed message", *, length=None, length_text=None):
    body = _error_body(code, message)
    declared = length_text if length_text is not None else (
        b"%d" % (len(body) if length is None else length))
    return _head(
        status,
        lines=(b"Content-Type: application/json", b"Connection: close",
               b"Content-Length: " + declared),
        body=body,
    )


def _event(name, data, *, identifier=None):
    raw = b"event: " + name + b"\n"
    if identifier is not None:
        raw += b"id: " + (identifier if isinstance(identifier, bytes)
                          else identifier.encode("ascii")) + b"\n"
    return raw + b"data: " + data + b"\n\n"


def _revision_payload(previous, revision, *, context=b"false", changed=(b'"t-ui"',)):
    return (
        '{"previous_revision": "%s", "revision": "%s", "context_changed": %s, '
        '"changed_task_ids": [%s]}'
        % (previous, revision, context.decode("ascii"),
           b", ".join(changed).decode("ascii"))
    ).encode("utf-8")


def _revision_frame(previous, revision, *, payload=None, **kwargs):
    if payload is None:
        payload = _revision_payload(previous, revision, **kwargs)
    return _event(b"revision", payload, identifier=revision)


def _held(payload, release=None):
    """A script that sends `payload` and then keeps the connection open."""

    def script(peer, connection, request):
        if payload:
            connection.sendall(payload)
        (release if release is not None else peer.release).wait(30.0)

    return script


def _parked(draft, wire, *scripts):
    """A silent peer plus one consumer that has not been started yet."""
    peer = wire()
    if scripts:
        peer.script(*scripts)
    return peer, draft(peer.base_url)


# ------------------------------------------ construction, binding, validation


def test_the_bounded_constants_and_states_match_the_contract():
    assert MAX_PENDING_EVENTS == 32
    assert MAX_CHECKPOINT_BYTES == 4096
    assert MAX_HEAD_BYTES == 64 * 1024
    assert MAX_FRAME_BYTES == 64 * 1024 + 256
    assert MAX_ERROR_BYTES == 64 * 1024
    assert MAX_CHANGED_TASK_IDS == 512
    assert SOCKET_TIMEOUT == 5.0
    assert INITIAL_BACKOFF == 0.25
    assert MAX_BACKOFF == 5.0
    assert _TERMINAL_STATES == {
        "access_denied", "resnapshot_required", "protocol_error", "server_error",
    }
    assert {"stopped", "connecting", "streaming", "retrying", "inbox_full",
            "closed"} | _TERMINAL_STATES == STATES


def test_construction_persists_the_baseline_and_starts_no_network(
    wired, server, live, cursor
):
    consumer = live()
    assert consumer.status() == {
        "state": "stopped", "consumed_revision": wired.head,
        "received_revision": wired.head, "pending_count": 0, "code": None,
    }
    assert consumer.peek() == () and consumer._worker is None
    assert len(server._subscriptions) == 0 and len(server._workers) == 0
    assert _cursor_revision(cursor) == wired.head
    assert _call(server).status == 200  # the listener never saw the consumer


def test_the_status_dict_is_detached_and_carries_only_contract_keys(draft, cursor):
    consumer = draft("http://127.0.0.1:1")
    status = consumer.status()
    assert set(status) == {
        "state", "consumed_revision", "received_revision", "pending_count", "code",
    }
    status["state"] = "streaming"
    status["consumed_revision"] = FOREIGN
    assert consumer.status()["state"] == "stopped"
    assert consumer.status()["consumed_revision"] == ANCHOR


BAD_URLS = [
    "http://127.0.0.1:8080/v1/subscribe", "http://127.0.0.1:8080?a=1",
    "http://127.0.0.1:8080#f", "http://member@127.0.0.1:8080", "http://localhost:8080",
    "http://127.0.0.1", "http://127.0.0.1:", "http://127.0.0.1:0", "http://127.0.0.1:65536",
    "http://127.0.0.1:08080", "http://127.0.0.1:8080 ", "https://127.0.0.1:8080",
    "HTTP://127.0.0.1:8080", "http://127.0.0.1:*", "http://[::1]:8080",
    "http://127.1.0.1:8080", "127.0.0.1:8080", "http://127.0.0.1:8.0", "",
    None, 8080, b"http://127.0.0.1:8080",
]


@pytest.mark.parametrize("base_url", BAD_URLS, ids=range(len(BAD_URLS)))
def test_only_an_exact_literal_loopback_endpoint_is_accepted(draft, cursor, base_url):
    with pytest.raises(ValidationError):
        draft(base_url)
    assert not cursor.exists()


@pytest.mark.parametrize("credential,member,parent", [
    ("a" * 31, "alice", True), ("a" * 257, "alice", True), ("a" * 31 + " ", "alice", True),
    ("a" * 32 + "\n", "alice", True), ("ünïcode" + "a" * 32, "alice", True),
    (None, "alice", True), (ALICE, "", True), (ALICE, "a b", True), (ALICE, "a" * 129, True),
    (ALICE, None, True), (ALICE, "alice", False), (ALICE, "alice", "file"),
])
def test_the_credential_member_and_checkpoint_parent_are_validated(
    draft, cursor, tmp_path, credential, member, parent
):
    if parent is False:
        target = tmp_path / "absent" / "revision-cursor.json"
    elif parent == "file":
        target = tmp_path / "a-plain-file"
        target.write_bytes(b"{}")
    else:
        target = cursor
    with pytest.raises(ValidationError):
        draft("http://127.0.0.1:1", credential=credential, member_id=member,
              checkpoint_path=target)
    assert not cursor.exists()


def _snapshot_case(case, state):
    """One invalid `initial_snapshot` value, built only when it is requested.

    `build_initial_state` validates its own arguments, so a state that must
    carry an invalid field is assembled directly instead.
    """
    if case == "none":
        return None
    if case == "dict":
        return {"revision": ANCHOR, "state": state.to_dict()}
    if case == "object":
        return object()
    if case == "bad-revision":
        return Snapshot(revision="z" * 40, state=state)
    if case == "short-revision":
        return Snapshot(revision="b" * 39, state=state)
    if case == "int-revision":
        return Snapshot(revision=1, state=state)
    if case == "state-not-a-state":
        return Snapshot(revision=ANCHOR, state=state.to_dict())
    if case == "bad-base-commit":
        return Snapshot(
            revision=ANCHOR,
            state=SessionState(SESSION_ID, "v0.1", "z" * 40, state.context, state.tasks),
        )
    if case == "bad-session-id":
        return Snapshot(
            revision=ANCHOR,
            state=SessionState("not a session", "v0.1", SHA0, state.context, state.tasks),
        )
    raise AssertionError(case)


@pytest.mark.parametrize("case", [
    "none", "dict", "object", "bad-revision", "short-revision", "int-revision",
    "state-not-a-state", "bad-base-commit", "bad-session-id",
])
def test_only_a_valid_host_approved_snapshot_is_accepted(draft, cursor, anchor, case):
    with pytest.raises(ValidationError):
        draft("http://127.0.0.1:1", initial_snapshot=_snapshot_case(case, anchor.state))
    assert not cursor.exists()


def _checkpoint_doc(case, revision):
    document = {
        "schema": 1, "session_id": SESSION_ID, "base_commit": SHA0,
        "target_version": "v0.1 consumer", "member_id": "alice",
        "consumed_revision": revision,
    }
    if case == "member":
        document["member_id"] = "bob"
    elif case == "session":
        document["session_id"] = "other-1"
    elif case == "base":
        document["base_commit"] = "d" * 40
    elif case == "version":
        document["target_version"] = "v0.2 consumer"
    elif case == "schema-two":
        document["schema"] = 2
    elif case == "schema-bool":
        document["schema"] = True
    elif case == "schema-float":
        document["schema"] = 1.0
    elif case == "extra":
        document["context"] = "keep it small"
    elif case == "missing":
        del document["member_id"]
    elif case == "bad-revision":
        document["consumed_revision"] = "z" * 40
    elif case == "oversize":
        document["target_version"] = "v" * 5000
    return document


BAD_CHECKPOINTS = [
    "member", "session", "base", "version", "schema-two", "schema-bool", "schema-float",
    "extra", "missing", "bad-revision", "oversize", "corrupt", "empty", "list",
    "trailing", "token",
]


@pytest.mark.parametrize("case", BAD_CHECKPOINTS)
def test_an_existing_checkpoint_must_bind_identity_member_and_schema(
    draft, cursor, case
):
    if case == "corrupt":
        raw = b'{"schema": 1, "session_id": '
    elif case == "empty":
        raw = b""
    elif case == "list":
        raw = b"[]"
    elif case == "trailing":
        raw = json.dumps(_checkpoint_doc("member", ANCHOR)).encode() + b" trailing"
    elif case == "token":
        raw = json.dumps(dict(_checkpoint_doc("member", ANCHOR), token=ALICE)).encode()
    else:
        raw = json.dumps(_checkpoint_doc(case, ANCHOR)).encode("utf-8")
    cursor.write_bytes(raw)
    os.chmod(cursor, 0o600)
    with pytest.raises(ValidationError):
        draft("http://127.0.0.1:1")
    assert cursor.read_bytes() == raw  # a foreign cursor is never adopted


@pytest.mark.skipif(not POSIX, reason="POSIX symlink/fifo/privacy guards")
def test_symlink_nonregular_and_nonprivate_checkpoints_are_refused(draft, cursor):
    good = json.dumps(_checkpoint_doc("member", ANCHOR)).replace('"bob"', '"alice"')
    real = cursor.with_name("real-cursor.json")
    real.write_bytes(good.encode())
    os.chmod(real, 0o600)
    cursor.symlink_to(real)
    with pytest.raises(ValidationError):
        draft("http://127.0.0.1:1")

    cursor.unlink()
    cursor.mkdir()
    with pytest.raises(ValidationError):
        draft("http://127.0.0.1:1")
    cursor.rmdir()

    os.mkfifo(cursor)
    with pytest.raises(ValidationError):
        draft("http://127.0.0.1:1")
    cursor.unlink()

    private = cursor.with_name("private-cursor.json")
    private.write_bytes(good.encode())
    os.chmod(private, 0o600)
    consumer = draft("http://127.0.0.1:1", checkpoint_path=private)
    assert consumer.status()["consumed_revision"] == ANCHOR

    world = cursor.with_name("world-cursor.json")
    world.write_bytes(good.encode())
    os.chmod(world, 0o644)
    with pytest.raises(ValidationError):
        draft("http://127.0.0.1:1", checkpoint_path=world)
    os.chmod(world, 0o600)


# --------------------------------------- arrival, readonly peek, acknowledge


def test_arrival_and_readonly_peek_never_persist_or_advance(wired, server, live, cursor):
    consumer = live()
    consumer.start()
    _wait_for(consumer, lambda s: s["state"] == "streaming", "streaming")
    before = _checkpoint(cursor)
    published = _published(_legacy_store(wired), 2)
    _wait_for(consumer, lambda s: s["pending_count"] == 2, "two pending events")

    first, second = consumer.peek()
    assert consumer.peek() == (first, second) and isinstance(consumer.peek(), tuple)
    assert (first.revision, second.revision) == tuple(published)
    assert first.previous_revision == wired.head
    status = consumer.status()
    assert status["consumed_revision"] == wired.head
    assert status["received_revision"] == published[-1]
    assert _checkpoint(cursor) == before  # arrival is not an acknowledgement
    _assert_prefix(consumer, [wired.head, *published])


def test_an_ordered_prefix_ack_persists_before_it_trims_the_inbox(
    wired, server, live, cursor, home
):
    consumer = live()
    consumer.start()
    _wait_for(consumer, lambda s: s["state"] == "streaming", "streaming")
    published = _published(_legacy_store(wired), 3)
    _wait_for(consumer, lambda s: s["pending_count"] == 3, "three pending events")

    consumer.acknowledge_at_safe_point(published[1])
    status = consumer.status()
    assert status["consumed_revision"] == published[1]
    assert status["received_revision"] == published[2]
    assert [event.revision for event in consumer.peek()] == [published[2]]
    assert _cursor_revision(cursor) == published[1]
    assert consumer.acknowledge_at_safe_point(published[1]) is None  # idempotent
    assert consumer.status()["consumed_revision"] == published[1]
    assert [path.name for path in home.iterdir()] == [cursor.name]


def test_acknowledging_rejects_everything_outside_the_inbox(wired, server, live, cursor):
    consumer = live()
    consumer.start()
    _wait_for(consumer, lambda s: s["state"] == "streaming", "streaming")
    published = _published(_legacy_store(wired), 2)
    _wait_for(consumer, lambda s: s["pending_count"] == 2, "two pending events")
    consumer.acknowledge_at_safe_point(published[0])
    before = consumer.status()
    raw = _checkpoint(cursor)

    for revision in ("f" * 40, "z" * 40, "b" * 39, "", None, 7, [], {}, True):
        with pytest.raises(ValidationError):
            consumer.acknowledge_at_safe_point(revision)
    assert consumer.status() == before
    assert [event.revision for event in consumer.peek()] == [published[1]]
    assert _checkpoint(cursor) == raw


@pytest.mark.parametrize("case", ["readonly-parent", "replace-fails"])
def test_a_failed_persistence_leaves_the_cursor_inbox_and_file_unchanged(
    draft, cursor, home, wire, monkeypatch, case
):
    peer = wire()
    consumer = draft(peer.base_url)
    peer.script(_held(_head() + _revision_frame(ANCHOR, NEXT)))
    consumer.start()
    _wait_for(consumer, lambda s: s["pending_count"] == 1, "one pending event")
    before = consumer.status()
    raw = _checkpoint(cursor)
    if case == "readonly-parent":
        if not NOT_ROOT:
            pytest.skip("a read-only parent is not enforced for this user")
        original = stat.S_IMODE(home.stat().st_mode)
        os.chmod(home, 0o500)
        restore = lambda: os.chmod(home, original)  # noqa: E731 - one-shot restore
    else:
        def broken(*args, **kwargs):
            raise OSError(5, "simulated rename failure")

        monkeypatch.setattr(os, "replace", broken)
        restore = lambda: monkeypatch.undo()  # noqa: E731 - one-shot restore
    try:
        with pytest.raises(ValidationError):
            consumer.acknowledge_at_safe_point(NEXT)
    finally:
        restore()
    assert consumer.status() == before
    assert [event.revision for event in consumer.peek()] == [NEXT]
    assert _checkpoint(cursor) == raw
    consumer.acknowledge_at_safe_point(NEXT)
    assert consumer.status()["consumed_revision"] == NEXT
    assert consumer.peek() == ()
    assert _cursor_revision(cursor) == NEXT


def test_the_persisted_cursor_is_private_and_carries_no_context_or_credential(
    wired, server, live, cursor
):
    consumer = live()
    consumer.start()
    _wait_for(consumer, lambda s: s["state"] == "streaming", "streaming")
    _published(_legacy_store(wired), 1)
    _wait_for(consumer, lambda s: s["pending_count"] == 1, "one pending event")
    consumer.acknowledge_at_safe_point(consumer.peek()[0].revision)
    raw = _checkpoint(cursor)
    document = json.loads(raw)
    assert set(document) == {
        "schema", "session_id", "base_commit", "target_version", "member_id",
        "consumed_revision",
    }
    assert document["schema"] == 1 and document["member_id"] == "alice"
    assert document["session_id"] == SESSION_ID and document["base_commit"] == SHA0
    if POSIX:
        assert stat.S_IMODE(cursor.stat().st_mode) == 0o600
    for needle in (ALICE, BOB, OTHER, DAVE, "shared goal", "keep it small", "t-ui",
                   str(cursor), "context", "credential"):
        assert needle.encode() not in raw, needle
    assert len(raw) <= MAX_CHECKPOINT_BYTES


def test_the_supplied_snapshot_revision_is_the_baseline_and_its_backlog_replays(
    wired, server, live, cursor
):
    baseline = wired.coordinator.snapshot(ALICE)
    published = _published(_legacy_store(wired), 2)
    consumer = live(initial_snapshot=baseline)
    assert consumer.status()["consumed_revision"] == baseline.revision
    assert _cursor_revision(cursor) == baseline.revision
    consumer.start()
    _wait_for(consumer, lambda s: s["pending_count"] == 2, "the whole backlog")
    assert [event.revision for event in consumer.peek()] == published
    assert consumer.status()["consumed_revision"] == baseline.revision
    assert _cursor_revision(cursor) == baseline.revision


def test_a_restart_with_a_newer_snapshot_uses_the_persisted_cursor(
    wired, server, live, cursor
):
    consumer = live()
    consumer.start()
    _wait_for(consumer, lambda s: s["state"] == "streaming", "streaming")
    published = _published(_legacy_store(wired), 3)
    _wait_for(consumer, lambda s: s["pending_count"] == 3, "three pending events")
    consumer.acknowledge_at_safe_point(published[0])
    consumer.close()

    newer = wired.coordinator.snapshot(ALICE)  # the host has a newer snapshot
    assert newer.revision == published[-1]
    restarted = live(initial_snapshot=newer)
    assert restarted.status()["consumed_revision"] == published[0]
    assert _cursor_revision(cursor) == published[0]
    restarted.start()
    _wait_for(restarted, lambda s: s["pending_count"] == 2, "unacked redelivery")
    assert [event.revision for event in restarted.peek()] == published[1:]
    assert restarted.status()["consumed_revision"] == published[0]
    assert _cursor_revision(cursor) == published[0]
    _assert_prefix(restarted, [wired.head, *published])


# ------------------------------------------------- full inbox, finite command


def test_a_full_inbox_closes_the_stream_parks_and_resumes_without_losing_events(
    wired, server, live, cursor
):
    consumer = live()
    published = _published(_legacy_store(wired), MAX_PENDING_EVENTS + 1)
    consumer.start()
    _wait_for(consumer, lambda s: s["state"] == "inbox_full", "a full inbox")
    assert consumer.status()["pending_count"] == MAX_PENDING_EVENTS
    assert [event.revision for event in consumer.peek()] == published[:MAX_PENDING_EVENTS]
    assert _wait_until(lambda: len(server._subscriptions) == 0), "the stream is closed"
    assert consumer.status()["consumed_revision"] == wired.head
    assert _cursor_revision(cursor) == wired.head

    write = _call(server, CONTEXT, _route_body(CONTEXT, published[-1]))
    assert write.status == 200, write.body  # a finite command still works
    fresh = _json(write)["revision"]
    assert _observe(consumer, 1.5) == {"inbox_full"}  # parked: nothing else is read
    parked = consumer.status()
    assert parked["pending_count"] == MAX_PENDING_EVENTS
    assert parked["received_revision"] == published[MAX_PENDING_EVENTS - 1]
    assert _cursor_revision(cursor) == wired.head

    consumer.acknowledge_at_safe_point(published[0])
    _wait_for(consumer, lambda s: s["received_revision"] == published[-1], "the tail")
    assert consumer.status()["state"] == "inbox_full"
    assert consumer.status()["pending_count"] == MAX_PENDING_EVENTS
    consumer.acknowledge_at_safe_point(published[1])
    _wait_for(consumer, lambda s: s["received_revision"] == fresh, "the finite write")
    assert [event.revision for event in consumer.peek()] == published[2:] + [fresh]
    assert consumer.status()["consumed_revision"] == published[1]
    _assert_prefix(consumer, [wired.head, *published, fresh])


# ------------------------------------------------- reconnect, retry, denial


def test_eof_reconnect_keeps_the_inbox_tail_and_reauthenticates(wired, servers, cursor):
    """Real listener, real port, real stop-and-replace credential rotation."""
    first = servers(wired.coordinator).start()
    consumer = RevisionConsumer(
        first.base_url, credential=ALICE, member_id="alice", checkpoint_path=cursor,
        initial_snapshot=wired.coordinator.snapshot(ALICE),
    )
    legacy = _legacy_store(wired)
    try:
        consumer.start()
        _wait_for(consumer, lambda s: s["state"] == "streaming", "streaming")
        published = _published(legacy, 2)
        _wait_for(consumer, lambda s: s["pending_count"] == 2, "two pending events")
        consumer.acknowledge_at_safe_point(published[0])
        raw = _checkpoint(cursor)
        port = int(first.base_url.rsplit(":", 1)[1])
        first.close()  # EOF for the live stream, then the port is released

        _wait_for(consumer, lambda s: s["code"] == "connection_retry", "a retry")
        assert [event.revision for event in consumer.peek()] == [published[1]]
        assert _checkpoint(cursor) == raw

        replacement = Coordinator(
            wired.store, session_id=SESSION_ID, owner_id="alice",
            member_credentials={"alice": DAVE, "bob": BOB},
        )
        second = servers(replacement, port=port).start()
        _wait_state(consumer, "access_denied")  # the revoked token is refused again
        assert consumer.status()["code"] == "access_denied"
        assert [event.revision for event in consumer.peek()] == [published[1]]
        assert _checkpoint(cursor) == raw
        assert _observe(consumer, 1.5) == {"access_denied"}  # never an automatic retry
        with pytest.raises(ValidationError):
            consumer.start()

        consumer.close()  # stop-and-replace also preserves one checkpoint writer
        successor = RevisionConsumer(
            second.base_url, credential=DAVE, member_id="alice", checkpoint_path=cursor,
            initial_snapshot=wired.coordinator.snapshot(ALICE),
        )
        try:
            successor.start()
            _wait_for(successor, lambda s: s["pending_count"] == 1, "redelivery")
            assert [event.revision for event in successor.peek()] == [published[1]]
            assert successor.status()["consumed_revision"] == published[0]
            assert _checkpoint(cursor) == raw  # the token is not namespaced into it

            following = _published(legacy, 1, start=published[-1])
            _wait_for(successor, lambda s: s["pending_count"] == 2, "the next event")
            assert [e.revision for e in successor.peek()] == published[1:] + following
            assert _call(second, auth=f"Bearer {DAVE}").status == 200
            assert _call(second, auth=f"Bearer {ALICE}").status == 401
        finally:
            successor.close()
    finally:
        _close_all([consumer])


def test_a_refused_endpoint_retries_with_a_growing_bounded_backoff(draft, monkeypatch):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = int(probe.getsockname()[1])
    probe.close()

    attempts = []
    original = socket.socket.connect

    def counting_connect(self, address):
        if tuple(address) == ("127.0.0.1", dead_port):
            attempts.append(time.monotonic())
        return original(self, address)

    monkeypatch.setattr(socket.socket, "connect", counting_connect)
    consumer = draft(f"http://127.0.0.1:{dead_port}")
    consumer.start()
    _wait_for(consumer, lambda s: s["code"] == "connection_retry", "a retry")
    assert _wait_until(lambda: len(attempts) >= 3), attempts
    gaps = [after - before for before, after in zip(attempts, attempts[1:])]
    assert gaps[0] >= INITIAL_BACKOFF * 0.8, gaps
    assert gaps[1] >= INITIAL_BACKOFF * 1.6, gaps  # 0.5s after the first failure
    assert len(attempts) <= 6  # never a busy loop

    _assert_prompt(consumer)
    settled = len(attempts)
    _observe(consumer, 0.6)
    assert len(attempts) == settled  # close interrupts the backoff


def test_a_401_parks_without_an_automatic_retry_or_echo(
    draft, wire, anchor, cursor, caplog, capsys
):
    poison = f"credential {ALICE} at /tmp/hub.git Traceback"
    peer, consumer = _parked(
        draft, wire, _json_head(b"401 Unauthorized", "access_denied", poison)
    )
    consumer.start()
    _wait_state(consumer, "access_denied")
    assert consumer.status()["code"] == "access_denied"
    assert _observe(consumer, 1.5) == {"access_denied"}
    assert peer.connections == 1
    assert peer.cursors() == [ANCHOR] and peer.paths() == [SUBSCRIBE]
    for raw in peer.requests:
        assert BEARER in raw
    with pytest.raises(ValidationError):
        consumer.start()
    for value in consumer.status().values():
        assert ALICE not in str(value) and "Traceback" not in str(value)
    assert _cursor_revision(cursor) == ANCHOR
    _silent(caplog, capsys)

    consumer.reset_at_safe_point(anchor)
    consumer.start()
    assert _wait_until(lambda: peer.connections == 2), peer.connections
    assert peer.cursors() == [ANCHOR, ANCHOR]


@pytest.mark.parametrize("status,code", [
    (b"401 Unauthorized", "access_denied"),
    (b"410 Gone", "replay_unavailable"),
    (b"503 Service Unavailable", "session_unavailable"),
])
def test_a_truncated_fixed_error_body_is_a_protocol_error(draft, wire, status, code):
    peer, consumer = _parked(
        draft, wire, _json_head(status, code, length=4096)
    )
    consumer.start()
    _wait_for(consumer, lambda s: s["state"] in _TERMINAL_STATES or s["state"] == "retrying",
              "a response outcome")
    assert consumer.status()["state"] == "protocol_error"
    assert consumer.peek() == () and consumer.status()["consumed_revision"] == ANCHOR
    assert _prompt(consumer.close) < 2.5


# --------------------------------------------------------- expiry and reset


def test_window_expiry_parks_and_only_a_same_identity_reset_recovers(
    wired, server, live, cursor, monkeypatch
):
    monkeypatch.setattr(store_module, "REPLAY_WINDOW", 2)
    consumer = live()
    consumer.start()
    _wait_for(consumer, lambda s: s["state"] == "streaming", "streaming")
    legacy = _legacy_store(wired)
    published = _published(legacy, 1)
    _wait_for(consumer, lambda s: s["pending_count"] == 1, "one pending event")
    raw = _checkpoint(cursor)
    _published(legacy, 3, start=published[-1])
    _wait_state(consumer, "resnapshot_required")
    assert consumer.status()["code"] == "resnapshot_required"
    assert [event.revision for event in consumer.peek()] == published
    assert consumer.status()["consumed_revision"] == wired.head
    assert _checkpoint(cursor) == raw
    with pytest.raises(ValidationError):
        consumer.start()
    consumer.close()

    # A fresh instance offers the persisted, now unknown cursor: HTTP 410.
    reopened = live()
    assert reopened.status()["consumed_revision"] == wired.head
    reopened.start()
    _wait_state(reopened, "resnapshot_required")
    assert reopened.status()["pending_count"] == 0
    assert _checkpoint(cursor) == raw
    assert _observe(reopened, 1.5) == {"resnapshot_required"}

    # A different session and a different version are both a foreign identity.
    with pytest.raises(ValidationError):
        reopened.reset_at_safe_point(
            Snapshot(revision=FOREIGN, state=build_initial_state(
                session_id="other-1", target_version="v0.1", base_commit=SHA0)))
    with pytest.raises(ValidationError):
        reopened.reset_at_safe_point(
            Snapshot(revision=FOREIGN, state=build_initial_state(
                session_id=SESSION_ID, target_version="v9.9", base_commit=SHA0)))
    assert reopened.status()["state"] == "resnapshot_required"
    with pytest.raises(ValidationError):
        reopened.start()
    assert _checkpoint(cursor) == raw

    fresh = wired.coordinator.snapshot(ALICE)
    reopened.reset_at_safe_point(fresh)
    status = reopened.status()
    assert status["consumed_revision"] == status["received_revision"] == fresh.revision
    assert status["state"] == "stopped" and reopened.peek() == ()
    assert _cursor_revision(cursor) == fresh.revision

    reopened.start()
    _wait_for(reopened, lambda s: s["state"] == "streaming", "streaming")
    following = _published(legacy, 1, start=fresh.revision)
    _wait_for(reopened, lambda s: s["pending_count"] == 1, "post-reset delivery")
    event = reopened.peek()[0]
    assert (event.revision, event.previous_revision) == (following[0], fresh.revision)
    assert _cursor_revision(cursor) == fresh.revision


# ------------------------------------------------------ close and concurrency


@pytest.mark.parametrize("case", ["blocked-head", "blocked-read", "backoff"])
def test_close_interrupts_a_blocked_read_a_blocked_head_and_a_backoff(
    draft, wire, caplog, capsys, case
):
    payload = {
        "blocked-head": b"",  # nothing at all: blocked inside the status line
        "blocked-read": _head() + b": ready\n\n",  # a live stream that goes quiet
        "backoff": _json_head(b"503 Service Unavailable", "session_unavailable"),
    }[case]
    peer = wire()
    consumer = draft(peer.base_url)
    peer.script(_held(payload))
    consumer.start()
    assert _wait_until(lambda: bool(peer.requests))
    if case == "blocked-read":
        _wait_state(consumer, "streaming")
    elif case == "backoff":
        _wait_state(consumer, "retrying")
    _assert_prompt(consumer, limit=2.5)
    for call in (consumer.start,
                 lambda: consumer.reset_at_safe_point(
                     _snapshot_at(consumer.status()["consumed_revision"])),
                 lambda: consumer.acknowledge_at_safe_point(ANCHOR)):
        with pytest.raises(ValidationError):
            call()
    assert peer.requests and BEARER in peer.requests[0]
    _silent(caplog, capsys)


def test_close_shuts_down_owned_socket_while_getresponse_is_blocked(
    draft, monkeypatch
):
    """The socket remains directly owned while HTTPResponse reads the head.

    In particular, closing HTTPResponse first can wait for its reader lock on
    Windows. Lifecycle shutdown must reach the retained socket before the
    worker's response/connection/socket cleanup runs.
    """
    entered_getresponse = threading.Event()
    interrupted = threading.Event()
    actions = []

    class FakeSocket:
        def __init__(self, name):
            self.name = name
            self.shutdown_called = False

        def settimeout(self, _timeout):
            pass

        def connect(self, _address):
            pass

        def shutdown(self, how):
            assert how == socket.SHUT_RDWR
            self.shutdown_called = True
            actions.append(f"shutdown-{self.name}")
            if self.name == "twin":
                interrupted.set()

        def dup(self):
            twin = FakeSocket("twin")
            actions.append("dup")
            return twin

        def close(self):
            actions.append(f"close-{self.name}")

    fake_socket = FakeSocket("original")

    class FakeConnection:
        def __init__(self, *_args, **_kwargs):
            self.sock = None
            self.response_class = None

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            entered_getresponse.set()
            # Model http.client closing its socket handle after the HTTP/1.0
            # close response headers, while the makefile reader is still live.
            self.sock.close()
            assert interrupted.wait(2.0), "getresponse was not interrupted by shutdown"
            assert self.sock is fake_socket
            assert not fake_socket.shutdown_called
            raise OSError("socket shut down")

        def close(self):
            actions.append("connection-close")

    monkeypatch.setattr(consumer_module.socket, "socket", lambda *_a, **_k: fake_socket)
    monkeypatch.setattr(consumer_module.http.client, "HTTPConnection", FakeConnection)
    consumer = draft("http://127.0.0.1:1")
    consumer.start()
    assert entered_getresponse.wait(2.0), "worker did not enter getresponse"

    assert _prompt(consumer.close, limit=2.5) < 2.5
    assert actions.index("shutdown-twin") < actions.index("close-twin")
    assert actions.index("close-original") < actions.index("shutdown-twin")
    assert not fake_socket.shutdown_called
    assert consumer.status()["state"] == "closed"


def test_interrupt_socket_dup_failure_retries_without_exposing_error(draft, monkeypatch):
    actions = []

    class FakeSocket:
        def settimeout(self, _timeout):
            pass

        def connect(self, _address):
            pass

        def shutdown(self, _how):
            pass

        def dup(self):
            raise OSError("sensitive socket detail")

        def close(self):
            actions.append("socket-close")

    class FakeConnection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            pass

        def close(self):
            actions.append("connection-close")

    monkeypatch.setattr(consumer_module.socket, "socket", lambda *_a, **_k: FakeSocket())
    monkeypatch.setattr(consumer_module.http.client, "HTTPConnection", FakeConnection)
    consumer = draft("http://127.0.0.1:1")
    consumer.start()
    _wait_for(consumer, lambda status: status["state"] == "retrying", "bounded retry")
    assert consumer.status()["code"] == "connection_retry"
    assert "sensitive socket detail" not in repr(consumer.status())
    assert "socket-close" in actions and "connection-close" in actions
    _assert_prompt(consumer)


def test_close_is_idempotent_and_serializes_concurrent_closers(draft, wire):
    peer = wire()
    consumer = draft(peer.base_url)
    peer.script(_held(_head() + b": ready\n\n"))
    consumer.start()
    _wait_state(consumer, "streaming")

    gate = threading.Barrier(6)
    errors = []

    def closer():
        gate.wait()
        try:
            consumer.close()
        except Exception as error:  # noqa: BLE001 - recorded for the assertion
            errors.append(error)

    threads = [threading.Thread(target=closer) for _ in range(5)]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join(5.0)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert consumer.status()["state"] == "closed" and consumer._worker is None
    consumer.close()  # idempotent


def test_close_while_parked_at_a_full_inbox_preserves_pending(wired, server, live, cursor):
    consumer = live()
    published = _published(_legacy_store(wired), MAX_PENDING_EVENTS)
    consumer.start()
    _wait_state(consumer, "inbox_full")
    _assert_prompt(consumer)
    assert [event.revision for event in consumer.peek()] == published
    with pytest.raises(ValidationError):
        consumer.acknowledge_at_safe_point(published[0])
    assert _cursor_revision(cursor) == wired.head


def test_no_lifecycle_or_inbox_lock_is_held_while_the_worker_waits(draft, wire):
    peer = wire()
    consumer = draft(peer.base_url)
    peer.script(_held(_head() + b": ready\n\n"))
    consumer.start()
    _wait_state(consumer, "streaming")
    for lock in (consumer._condition, consumer._lifecycle):
        acquired = lock.acquire(timeout=1.5)
        try:
            assert acquired, lock
        finally:
            if acquired:
                lock.release()
    assert consumer.status()["state"] == "streaming" and consumer.peek() == ()
    _assert_prompt(consumer, limit=2.5)


def test_ack_and_reset_races_keep_the_prefix_and_the_file_invariant(
    wired, server, live, cursor
):
    consumer = live()
    consumer.start()
    _wait_for(consumer, lambda s: s["state"] == "streaming", "streaming")
    legacy = _legacy_store(wired)
    order = [wired.head]
    errors = []

    for _ in range(4):
        gate = threading.Event()
        acked = []

        def acknowledge():
            gate.wait()
            try:
                pending = consumer.peek()
                if pending:
                    acked.append(pending[0].revision)
                    consumer.acknowledge_at_safe_point(pending[0].revision)
            except ValidationError as error:
                errors.append(error)

        thread = threading.Thread(target=acknowledge)
        thread.start()
        order.extend(_published(legacy, 1, start=order[-1]))
        _wait_for(consumer, lambda s: s["received_revision"] == order[-1], "arrival")
        gate.set()
        thread.join(5.0)
        assert not thread.is_alive() and errors == []
        status = consumer.status()
        assert _cursor_revision(cursor) == status["consumed_revision"]
        assert status["consumed_revision"] in order
        remaining = [event.revision for event in consumer.peek()]
        assert all(revision not in remaining for revision in acked)
        _assert_prefix(consumer, order)

    reset_gate = threading.Event()

    def reset():
        reset_gate.wait()
        consumer.reset_at_safe_point(wired.coordinator.snapshot(ALICE))

    thread = threading.Thread(target=reset)
    thread.start()
    order.extend(_published(legacy, 1, start=order[-1]))
    reset_gate.set()
    thread.join(15.0)
    assert not thread.is_alive()
    status = consumer.status()
    assert status["state"] == "stopped" and consumer.peek() == ()
    assert _cursor_revision(cursor) == status["consumed_revision"] == status["received_revision"]
    assert _wait_until(lambda: len(server._subscriptions) == 0), "the stream stopped"

    consumer.start()
    _wait_for(consumer, lambda s: s["state"] == "streaming", "streaming")
    order.extend(_published(legacy, 1, start=order[-1]))
    _wait_for(consumer, lambda s: s["pending_count"] == 1, "post-reset arrival")
    assert consumer.peek()[0].revision == order[-1]
    assert consumer.status()["consumed_revision"] != order[-1]


# --------------------------------------------------- no DNS, proxy, redirect


def test_no_name_resolution_proxy_or_redirect_path_exists(
    wired, server, live, monkeypatch, tmp_path
):
    def forbidden(*args, **kwargs):
        raise AssertionError("no name resolution may happen")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "gethostbyname", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    dead = f"http://127.0.0.1:{tmp_path.stat().st_ino % 60000 + 1}"
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, dead)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)

    # http.client itself reaches the listener through socket.create_connection,
    # so the consumer reaching it at all proves it uses neither that nor DNS.
    consumer = live()
    consumer.start()
    _wait_for(consumer, lambda s: s["state"] == "streaming", "streaming")
    published = _published(_legacy_store(wired), 1)
    _wait_for(consumer, lambda s: s["pending_count"] == 1, "arrival")
    assert consumer.peek()[0].revision == published[0]


def test_a_redirect_is_a_fixed_protocol_error_and_is_never_followed(draft, wire):
    location = b"http://127.0.0.1:9/v1/snapshot"
    body = _error_body("access_denied")
    peer, consumer = _parked(
        draft, wire,
        _head(
            b"302 Found",
            lines=(b"Content-Type: application/json", b"Connection: close",
                   b"Location: " + location, b"Content-Length: %d" % len(body)),
            body=body,
        ),
    )
    consumer.start()
    _wait_state(consumer, "protocol_error")
    assert consumer.status()["code"] == "protocol_error"
    assert _observe(consumer, 1.0) == {"protocol_error"}
    assert peer.connections == 1 and peer.paths() == [SUBSCRIBE]
    assert location not in peer.requests[0]


# --------------------------------------------------------------- the wire


@pytest.mark.parametrize("total,state", [
    (MAX_HEAD_BYTES, "streaming"), (MAX_HEAD_BYTES + 1, "protocol_error"),
])
def test_the_response_head_budget_is_exactly_64_kib(draft, wire, total, state):
    head = _padded_head(total)
    peer = wire()
    consumer = draft(peer.base_url)
    peer.script(_held(head if total > MAX_HEAD_BYTES else head + b": ready\n\n"))
    consumer.start()
    _wait_state(consumer, state)
    # The peer holds the connection open and sends nothing else, so a
    # protocol_error proves the budget was enforced while reading rather than
    # after buffering the whole head.
    assert peer.connections == 1 and consumer.peek() == ()
    _assert_prompt(consumer, limit=2.5)


BAD_STATUS_LINES = [
    b"HTTP/1.1 200 OK\r\n\r\n",
    b"HTTP/1.0 099 x\r\n",
    b"HTTP/1.0 600 x\r\n",
    b"HTTP/1.0 200\r\n",
    b"HTTP/2.0 200 OK\r\n",
    b"\r\n",
    b"garbage\r\n\r\n",
    b"200 OK\r\n\r\n",
    b"HTTP/1.0 200 OK",
    b"HTTP/1.0  20 OK\r\n",
]


@pytest.mark.parametrize("head", BAD_STATUS_LINES, ids=range(len(BAD_STATUS_LINES)))
def test_only_a_bounded_http10_status_line_is_accepted(draft, wire, head):
    peer, consumer = _parked(draft, wire, head)
    consumer.start()
    _wait_state(consumer, "protocol_error")
    _assert_prompt(consumer, limit=2.5)


BAD_HEADS = [
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\n\r\n",              # no close
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\nConnection: keep-alive\r\n\r\n",
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n",
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nConnection: close\r\n\r\n",
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nConnection: close\r\n\r\n",
    b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n",
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n",
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Encoding: gzip\r\n\r\n",
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nTrailer: x\r\n\r\n",
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nUpgrade: h2c\r\n\r\n",
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nNoColonHere\r\n\r\n",
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nX Y: 1\r\n\r\n",
    b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nX-Bad: a\x01b\r\n\r\n",
]


@pytest.mark.parametrize("head", BAD_HEADS, ids=range(len(BAD_HEADS)))
def test_response_headers_must_be_a_bounded_close_delimited_stream(draft, wire, head):
    peer, consumer = _parked(draft, wire, head)
    consumer.start()
    _wait_state(consumer, "protocol_error")
    _assert_prompt(consumer, limit=2.5)


BAD_ERROR_RESPONSES = {
    "text-plain": _head(b"401 Unauthorized", lines=(
        b"Content-Type: text/plain", b"Connection: close", b"Content-Length: 2")) + b"{}",
    "extra-key": _raw_json_head(
        b"401 Unauthorized", _error_body("access_denied").replace(
            b'"code"', b'"hint": "x", "code"')),
    "unknown-code": _json_head(b"401 Unauthorized", "not_a_code"),
    "401-inconsistent-code": _json_head(b"401 Unauthorized", "replay_unavailable"),
    "410-inconsistent-code": _json_head(b"410 Gone", "access_denied"),
    "message-not-a-string": _raw_json_head(
        b"401 Unauthorized", _error_body("access_denied").replace(
            b'"a fixed message"', b"7")),
    "outer-extra": _raw_json_head(
        b"401 Unauthorized", _error_body("access_denied").replace(
            b'{"error"', b'{"request": 1, "error"')),
    "length-oversize": _json_head(b"401 Unauthorized", "access_denied", length_text=b"70000"),
    "length-not-numeric": _json_head(
        b"401 Unauthorized", "access_denied", length_text=b"0x3e"),
    "length-too-many-digits": _json_head(
        b"401 Unauthorized", "access_denied", length_text=b"1000000"),
    "length-negative": _json_head(
        b"401 Unauthorized", "access_denied", length_text=b"-62"),
    "error-not-an-object": _raw_json_head(
        b"410 Gone", b'{"error": ' + _error_body("replay_unavailable")[1:]),
}


@pytest.mark.parametrize("case", sorted(BAD_ERROR_RESPONSES))
def test_a_denial_without_the_fixed_json_shape_is_a_protocol_error(draft, wire, case):
    peer, consumer = _parked(draft, wire, BAD_ERROR_RESPONSES[case])
    consumer.start()
    _wait_state(consumer, "protocol_error")
    _assert_prompt(consumer, limit=2.5)


def test_a_fixed_5xx_error_body_retries_instead_of_parking(draft, wire):
    peer, consumer = _parked(
        draft, wire, _json_head(b"503 Service Unavailable", "session_unavailable")
    )
    consumer.start()
    _wait_for(consumer, lambda s: s["code"] == "connection_retry", "a retry")
    assert _wait_until(lambda: peer.connections >= 2)
    assert _observe(consumer, 0.6) <= {"connecting", "retrying"}
    _assert_prompt(consumer, limit=2.5)


OVERSIZE_FRAMES = [
    b"data: " + b"x" * (MAX_FRAME_BYTES + 10) + b"\n\n",
    b"data: " + b"x" * 30000 + b"\ndata: " + b"y" * 30000 + b"\ndata: "
    + b"z" * 30000 + b"\n\n",
    b": " + b"c" * (MAX_FRAME_BYTES + 10) + b"\n\n",
    b"event: error\ndata: " + b"x" * (MAX_ERROR_BYTES + 10) + b"\n\n",
]


@pytest.mark.parametrize("frames", OVERSIZE_FRAMES, ids=range(len(OVERSIZE_FRAMES)))
def test_an_oversize_frame_or_error_fails_closed(draft, wire, frames):
    peer, consumer = _parked(draft, wire, _head() + frames)
    consumer.start()
    _wait_state(consumer, "protocol_error")
    assert consumer.peek() == ()
    _assert_prompt(consumer, limit=2.5)


BAD_FRAMES = [
    b"event: revision\nid: %s\ndata: {}\ndata: {}\n\n" % NEXT.encode(),      # multiline
    b"event: revision\nid: %s\nid: %s\ndata: {}\n\n" % (NEXT.encode(), AFTER.encode()),
    b"event: revision\nevent: revision\nid: %s\ndata: {}\n\n" % NEXT.encode(),
    b"event: revision\nid: %s\ndata: {}\nretry: 1\n\n" % NEXT.encode(),     # extra field
    b"event: revision\nid: %s\n: a comment\ndata: {}\n\n" % NEXT.encode(),
    b": a comment\nevent: revision\nid: %s\ndata: {}\n\n" % NEXT.encode(),
    b"event: revision\ndata: {}\n\n",                                       # no id
    b"event: revision\nid: %s\ndata: {}\n\n" % ANCHOR.encode(),           # duplicate of head
    b"event: Revision\nid: %s\ndata: {}\n\n" % NEXT.encode(),             # wrong case
    b"event: heartbeat\ndata: {}\n\n",                                     # unknown event
    b"event: error\nid: %s\ndata: {}\n\n" % NEXT.encode(),               # terminal with id
    b"data\n\n",                                                          # no colon
    b"event: revision: value\n\n",
    b"event: revision\nid: %s\r\ndata: {}\n\n" % NEXT.encode(),            # CRLF does not fix the invalid payload
    b"event: revision\nid: %s\ndata: {}\r\n\n" % NEXT.encode(),
    b"event: revision\nid: %s\ndata: {}\n\n" % b"z" * 40,                   # id not a sha
]


@pytest.mark.parametrize("frames", BAD_FRAMES, ids=range(len(BAD_FRAMES)))
def test_a_revision_frame_is_exactly_one_event_id_and_data(draft, wire, frames):
    peer, consumer = _parked(draft, wire, _head() + frames)
    consumer.start()
    _wait_state(consumer, "protocol_error")
    assert consumer.peek() == ()
    assert consumer.status()["consumed_revision"] == consumer.status()["received_revision"]
    _assert_prompt(consumer, limit=2.5)


def _payload_case(case, previous, revision):
    good = _revision_payload(previous, revision)
    if case == "unknown-key":
        return good[:-1] + b', "extra": 1}'
    if case == "missing-key":
        return (
            '{"previous_revision": "%s", "revision": "%s", "context_changed": false}'
            % (previous, revision)
        ).encode("utf-8")
    if case == "context-string":
        return _revision_payload(previous, revision, context=b'"false"')
    if case == "context-int":
        return _revision_payload(previous, revision, context=b"1")
    if case == "ids-not-a-list":
        return good.replace(b'["t-ui"]', b'{"t-ui": true}')
    if case == "ids-unsorted":
        return _revision_payload(previous, revision, changed=(b'"t-ui", "t-api"',))
    if case == "ids-duplicated":
        return _revision_payload(previous, revision, changed=(b'"t-ui", "t-ui"',))
    if case == "ids-too-many":
        many = b", ".join(b'"t-%03d"' % index for index in range(MAX_CHANGED_TASK_IDS + 1))
        return _revision_payload(previous, revision, changed=(many,))
    if case == "id-unsafe":
        return _revision_payload(previous, revision, changed=(b'"t ui"',))
    if case == "id-not-a-string":
        return _revision_payload(previous, revision, changed=(b"7",))
    if case == "revision-mismatch":
        return _revision_payload(previous, AFTER)
    if case == "previous-equals-revision":
        return _revision_payload(revision, revision)
    if case == "previous-mismatch":
        return _revision_payload(AFTER, revision)
    if case == "not-json":
        return b"{"
    if case == "nan":
        return good[:-1] + b', "context_changed": NaN}'
    if case == "duplicate-keys":
        return good[:-1] + b', "revision": "' + revision.encode("ascii") + b'"}'
    if case == "array":
        return b"[]"
    if case == "not-utf8":
        return good[:-1] + b', "note": "\xff"}'
    if case == "carriage-return":
        return good.replace(b"false", b"fa\rse")
    if case == "nul-byte":
        return good[:-1] + b', "note": "a\x00b"}'
    raise AssertionError(case)


PAYLOAD_CASES = [
    "unknown-key", "missing-key", "context-string", "context-int", "ids-not-a-list",
    "ids-unsorted", "ids-duplicated", "ids-too-many", "id-unsafe", "id-not-a-string",
    "revision-mismatch", "previous-equals-revision", "previous-mismatch", "not-json",
    "nan", "duplicate-keys", "array", "not-utf8", "carriage-return", "nul-byte",
]


@pytest.mark.parametrize("case", PAYLOAD_CASES)
def test_revision_payloads_are_parsed_strictly_or_fail_closed(draft, wire, case):
    frame = _event(b"revision", _payload_case(case, ANCHOR, NEXT), identifier=NEXT)
    peer, consumer = _parked(draft, wire, _head() + frame)
    consumer.start()
    _wait_state(consumer, "protocol_error")
    assert consumer.peek() == ()
    status = consumer.status()
    assert status["consumed_revision"] == status["received_revision"] == ANCHOR
    _assert_prompt(consumer, limit=2.5)


def test_a_valid_frame_with_the_bounded_task_id_union_is_accepted(draft, wire):
    many = b", ".join(b'"t-%03d"' % index for index in range(MAX_CHANGED_TASK_IDS))
    frame = _revision_frame(ANCHOR, NEXT, changed=(many,), context=b"true")
    peer = wire()
    consumer = draft(peer.base_url)
    peer.script(_held(_head() + frame))
    consumer.start()
    _wait_for(consumer, lambda s: s["pending_count"] == 1, "one pending event")
    event = consumer.peek()[0]
    assert event.context_changed is True and len(event.changed_task_ids) == 512
    assert consumer.status()["received_revision"] == NEXT
    assert consumer.status()["consumed_revision"] == ANCHOR
    _assert_prompt(consumer, limit=2.5)


def test_heartbeat_and_comment_frames_never_advance_a_cursor(draft, wire):
    comments = b": ready\n\n: heartbeat\n\n" + _revision_frame(ANCHOR, NEXT) + b": h\n\n"
    peer = wire()
    consumer = draft(peer.base_url)
    peer.script(_held(_head() + comments))
    consumer.start()
    _wait_for(consumer, lambda s: s["pending_count"] == 1, "one pending event")
    status = consumer.status()
    assert (status["received_revision"], status["consumed_revision"]) == (NEXT, ANCHOR)
    _assert_prompt(consumer, limit=2.5)


def test_a_partial_frame_at_eof_is_discarded_and_never_acknowledged(draft, wire, cursor):
    partial = b"event: revision\nid: " + NEXT.encode() + b"\ndata: " + (
        _revision_payload(ANCHOR, NEXT)[:20])
    peer = wire()
    consumer = draft(peer.base_url)
    peer.script(_head() + partial, _head() + _revision_frame(ANCHOR, NEXT))
    consumer.start()
    _wait_for(consumer, lambda s: s["pending_count"] == 1, "the complete retry")
    assert consumer.status()["received_revision"] == NEXT
    assert consumer.status()["consumed_revision"] == ANCHOR
    assert _cursor_revision(cursor) == ANCHOR
    assert peer.connections >= 2 and peer.cursors() == [ANCHOR, ANCHOR]
    _assert_prompt(consumer, limit=2.5)


@pytest.mark.parametrize("case,state", [("exact", "ignored"), ("conflicting", "protocol_error")])
def test_only_an_exact_duplicate_of_the_last_event_is_ignored(
    draft, wire, cursor, monkeypatch, case, state
):
    first = _revision_frame(ANCHOR, NEXT)
    # The second copy arrives on a later connection, so it is the redelivered
    # inbox tail - not an in-flight repeat - that must be tolerated or refused.
    second = first if case == "exact" else _revision_frame(AFTER, NEXT, changed=(b'"t-api"',))
    peer = wire()
    consumer = draft(peer.base_url)
    # The tolerated duplicate is held at the moment it reaches _accept: after
    # the real wire parser built the event and the state is already streaming,
    # before the real _accept body runs. No production lock is held here, and
    # the original callback still runs once the gate is released.
    entered, release, accepted = threading.Event(), threading.Event(), threading.Event()
    accepted_calls, redelivered = [], {}
    if state == "ignored":
        original_accept = consumer._accept

        def gated_accept(event):
            accepted_calls.append(event)
            if len(accepted_calls) == 2:
                redelivered["event"] = event
                entered.set()
                release.wait(SETTLE)
            result = original_accept(event)
            if len(accepted_calls) == 2:
                accepted.set()
            return result

        monkeypatch.setattr(consumer, "_accept", gated_accept)
    peer.script(_head() + first, _head() + second)
    consumer.start()
    try:
        _wait_for(consumer, lambda s: s["pending_count"] == 1, "the first event")
        assert _wait_until(lambda: peer.connections >= 2), peer.connections
        if state == "protocol_error":
            _wait_state(consumer, "protocol_error")
        else:
            assert entered.wait(SETTLE), "the duplicate never reached _accept"
            # "streaming" is the honest phase here: the valid head and the whole
            # duplicate frame are parsed and the real _accept has not rejected
            # anything. The tolerated copy must not add a terminal state.
            assert _observe(consumer, 1.5) <= {"connecting", "retrying", "streaming"}
    finally:
        release.set()
    if state == "ignored":
        assert accepted.wait(SETTLE), consumer.status()
        assert redelivered["event"] == accepted_calls[0]
        assert consumer.status()["state"] not in _TERMINAL_STATES | {"stopped", "closed"}
    status = consumer.status()
    assert (status["received_revision"], status["consumed_revision"]) == (NEXT, ANCHOR)
    assert status["pending_count"] == 1
    assert [event.revision for event in consumer.peek()] == [NEXT]
    assert _cursor_revision(cursor) == ANCHOR
    _assert_prompt(consumer, limit=2.5)


TERMINAL_FRAMES = [
    (b"error", b"internal_error", "server_error"),
    (b"error", b"stale_revision", "server_error"),
    (b"error", b"subscriber_limit", "server_error"),
    (b"error", b"invalid_request", "server_error"),
    (b"resnapshot_required", b"replay_unavailable", "resnapshot_required"),
    (b"error", b"replay_unavailable", "protocol_error"),
    (b"resnapshot_required", b"internal_error", "protocol_error"),
    (b"resnapshot_required", b"access_denied", "protocol_error"),
    (b"error", b"not_a_code", "protocol_error"),
    (b"error", b"", "protocol_error"),
]


@pytest.mark.parametrize("name,code,state", TERMINAL_FRAMES, ids=range(len(TERMINAL_FRAMES)))
def test_a_terminal_frame_is_a_fixed_shape_and_a_fixed_state(
    draft, wire, caplog, capsys, name, code, state
):
    poison = f"credential {ALICE} /tmp/hub.git Traceback"
    frame = _event(name, _error_body(code.decode(), poison))
    peer, consumer = _parked(draft, wire, _head() + frame)
    consumer.start()
    _wait_state(consumer, state)
    assert consumer.status()["code"] == state
    assert consumer.peek() == ()
    for value in consumer.status().values():
        assert ALICE not in str(value) and "Traceback" not in str(value)
    if state != "protocol_error":
        with pytest.raises(ValidationError):
            consumer.start()
    else:
        assert _observe(consumer, 1.0) == {"protocol_error"}
    _silent(caplog, capsys)
    _assert_prompt(consumer, limit=2.5)


def test_a_terminal_state_only_recovers_through_an_explicit_reset(draft, wire):
    peer = wire()
    consumer = draft(peer.base_url)
    peer.script(
        _head() + _event(b"error", _error_body("internal_error")),
        _head() + _revision_frame(AFTER, NEXT),
    )
    consumer.start()
    _wait_state(consumer, "server_error")
    with pytest.raises(ValidationError):
        consumer.start()
    assert peer.connections == 1
    consumer.reset_at_safe_point(_snapshot_at(AFTER))
    assert consumer.status() == {
        "state": "stopped", "consumed_revision": AFTER, "received_revision": AFTER,
        "pending_count": 0, "code": None,
    }
    consumer.start()
    _wait_for(consumer, lambda s: s["pending_count"] == 1, "the event after the reset")
    assert consumer.peek()[0].revision == NEXT
    _assert_prompt(consumer, limit=2.5)


# ------------------------------------------------------------- lead invariants


@pytest.mark.parametrize("case", ["foreign-snapshot", "persistence-failure"])
def test_failed_reset_preserves_the_terminal_gate_and_unconsumed_inbox(
    draft, wire, cursor, monkeypatch, case
):
    peer, consumer = _parked(
        draft, wire, _head() + _revision_frame(ANCHOR, NEXT)
        + _event(b"resnapshot_required", _error_body("replay_unavailable")),
    )
    consumer.start()
    _wait_state(consumer, "resnapshot_required")
    before = consumer.status()
    pending, checkpoint = consumer.peek(), _checkpoint(cursor)
    snapshot = _snapshot_at(AFTER)
    if case == "foreign-snapshot":
        snapshot = Snapshot(AFTER, build_initial_state(
            session_id="foreign-1", target_version="v0.1 consumer", base_commit=SHA0,
        ))
    else:
        def fail_replace(*args, **kwargs):
            raise OSError("checkpoint replacement failed")

        monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(ValidationError):
        consumer.reset_at_safe_point(snapshot)
    assert consumer.status() == before
    assert consumer.peek() == pending and _checkpoint(cursor) == checkpoint
    with pytest.raises(ValidationError):
        consumer.start()
    assert peer.connections == 1


def test_wire_arrival_during_ack_persistence_preserves_the_unconsumed_suffix(
    draft, wire, cursor, monkeypatch
):
    send_second = threading.Event()
    writing, release_write, arriving = threading.Event(), threading.Event(), threading.Event()
    peer = wire()
    consumer = draft(peer.base_url)

    def stream(peer, connection, request):
        connection.sendall(_head() + _revision_frame(ANCHOR, NEXT))
        if send_second.wait(timeout=20):
            connection.sendall(_revision_frame(NEXT, AFTER))
            arriving.set()
        peer.release.wait(timeout=20)

    peer.script(stream)
    consumer.start()
    _wait_for(consumer, lambda s: s["pending_count"] == 1, "the initial event")
    persist = consumer._persist_checkpoint

    def parked_persist(revision):
        writing.set()
        assert release_write.wait(timeout=20)
        return persist(revision)

    monkeypatch.setattr(consumer, "_persist_checkpoint", parked_persist)
    outcomes = []

    def ack():
        try:
            outcomes.append(consumer.acknowledge_at_safe_point(NEXT))
        except Exception as error:
            outcomes.append(error)

    thread = threading.Thread(target=ack, daemon=True)
    thread.start()
    try:
        assert writing.wait(timeout=10)
        send_second.set()
        assert arriving.wait(timeout=10)
        assert _cursor_revision(cursor) == ANCHOR  # receipt never commits the cursor
    finally:
        release_write.set()
        send_second.set()
        thread.join(timeout=20)
    assert not thread.is_alive() and outcomes == [None]
    _wait_for(consumer, lambda s: s["received_revision"] == AFTER, "the suffix")
    assert consumer.status()["consumed_revision"] == _cursor_revision(cursor) == NEXT
    assert [event.revision for event in consumer.peek()] == [AFTER]
    assert consumer.peek()[0].previous_revision == NEXT


def test_concurrent_ack_and_reset_cannot_overwrite_the_approved_reset_cursor(
    draft, wire, cursor
):
    peer, consumer = _parked(draft, wire, _held(_head() + _revision_frame(ANCHOR, NEXT)))
    consumer.start()
    _wait_for(consumer, lambda s: s["pending_count"] == 1, "a pending event")
    gate = threading.Barrier(3, timeout=15)
    outcomes = {}

    def run(name, operation):
        try:
            gate.wait()
            outcomes[name] = operation()
        except Exception as error:
            outcomes[name] = error

    threads = [
        threading.Thread(target=run, args=("ack", lambda: consumer.acknowledge_at_safe_point(NEXT)),
                         daemon=True),
        threading.Thread(target=run, args=("reset", lambda: consumer.reset_at_safe_point(_snapshot_at(AFTER))),
                         daemon=True),
    ]
    try:
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        assert outcomes.get("reset") is None and "reset" in outcomes
        assert "ack" in outcomes
        assert outcomes["ack"] is None or isinstance(outcomes["ack"], ValidationError)
        assert consumer.status()["state"] == "stopped"
        assert consumer.status()["consumed_revision"] == consumer.status()["received_revision"] == AFTER
        assert consumer.peek() == () and _cursor_revision(cursor) == AFTER
    finally:
        gate.abort()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=20)
