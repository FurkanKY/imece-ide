"""Bounded tests for the native-client revision subscription v1 slice.

Every subscription here is a real socket to the literal IPv4 address 127.0.0.1
that the listener itself bound: literal request bytes, manual SSE framing, real
sockets, real Git. Only temp bare hub/store metadata is created (base 'a'*40, no
source checkout, no install, API key, stage, commit or cleanup of anything
outside tmp_path). Fixture and helper seeding is reused from
tests/test_collab_transport.py, which shares this hub store and this listener.

Covers the "Native-client revision subscription v1 contract" of
docs/COLLABORATION.md: POST /v1/subscribe with exactly {after_revision}, strict
JSON, per-connection Bearer authentication strictly before any body read, body
parse or Git call (including reconnects), rejection of Last-Event-ID and the
other route guards, first-page validation with 400/410 before any 200, the
close-delimited text/event-stream response with : ready and heartbeat comments
that carry no revision, ordered `event: revision` + `id:` frames holding exactly
the ReplayEvent fields, bounded 32-event pages with no subscriber queue, no-op
cursor progress, legacy GitStore publication observed without an HTTP callback,
two competing CAS writes on an open stream, live window expiry with a terminal
resnapshot_required event and other core failures with a terminal error event
(never a second HTTP response, never an echo), subscriber/worker admission
limits, disconnect slot reclamation, slow-reader lock independence with a
genuinely blocked socket write, and the shutdown/drain races (idle EOF,
concurrent closers, registration race, in-flight core replay, and close() from
an accept or request worker thread).

No sleep drives correctness: races use threading Events, barriers and bounded
polls; every socket and thread is released in a fixture finally block.
"""

from __future__ import annotations

import functools
import http.client
import json
import os
import shutil
import socket
import sys
import threading
import time
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from collab_runtime import store as store_module  # noqa: E402
from collab_runtime import transport  # noqa: E402
from collab_runtime.coordinator import Coordinator, ReplayEvent, ReplayPage  # noqa: E402
from collab_runtime.errors import GitOperationError, ValidationError  # noqa: E402
from collab_runtime.models import canonical_json_bytes  # noqa: E402
from collab_runtime.store import REPLAY_WINDOW, GitStore  # noqa: E402
from test_collab_transport import (  # noqa: E402
    ALICE,
    BOB,
    MALLORY,
    SNAPSHOT,
    TASK_STATUS,
    _call,
    _error,
    _head,
    _json,
    _port,
    _route_body,
    _silent,
    hub,  # noqa: F401 - reused by the wired fixture
    leaks,  # noqa: F401 - reused fixture
    server,  # noqa: F401 - reused fixture
    servers,
    wired,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadı")

SUBSCRIBE = "/v1/subscribe"
REPLAY = "/v1/replay"
# The bounded constants of the contract, read from the module under test.
MAX_WORKERS = transport._MAX_WORKERS  # at most 12 active request workers
MAX_SUBSCRIPTIONS = transport._MAX_SUBSCRIPTIONS  # at most 8 subscriptions
PAGE_LIMIT = transport._REPLAY_LIMIT  # at most 32 events per replay page
INTERVAL = transport._OBSERVATION_INTERVAL  # one observation per second
WINDOW = REPLAY_WINDOW + 1  # 65 revisions, 64 transitions; older cursors are gone
EVENT_FIELDS = {"previous_revision", "revision", "context_changed", "changed_task_ids"}
# A live event is observed about once per second; the multiplier only widens a
# bounded wait for a deterministic, already-raced event. Nothing sleeps on it.
LIVE = 4 * INTERVAL + 10


# ------------------------------------------------------------ sse plumbing


def _deadline(timeout):
    return time.monotonic() + timeout


def _wait_until(predicate, timeout=20.0, interval=0.02):
    """Bounded poll of an already-reached state, never a correctness sleep."""
    deadline = _deadline(timeout)
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _take_frame(buffer):
    """Split one complete SSE frame (comment lines, fields, blank line)."""
    index = buffer.find(b"\n\n")
    if index < 0:
        return None, buffer
    raw, rest = buffer[: index + 2], buffer[index + 2 :]
    name = identifier = None
    data = []
    comments = []
    for line in raw.split(b"\n"):
        line = line.rstrip(b"\r")
        if line.startswith(b":"):
            comments.append(line[1:].strip())
            continue
        field, _, value = line.partition(b":")
        value = value.strip()
        if field == b"event":
            name = value.decode("latin-1")
        elif field == b"id":
            identifier = value.decode("latin-1")
        elif field == b"data":
            data.append(value)
    return (
        SimpleNamespace(name=name, id=identifier, data=b"".join(data),
                        comment=comments, raw=raw),
        rest,
    )


class _Subscriber:
    """One real subscribe connection: literal request bytes, manual SSE reads.

    Nothing is read unless a test asks for it, so a test can deliberately stay a
    non-reading slow reader. `raw` keeps every byte the listener sent on this
    socket, which is how "one HTTP response only" and partial delivery are
    asserted.
    """

    def __init__(self, server, *, after_revision, auth=f"Bearer {ALICE}", body=None,
                 recvbuf=None, extra=(("Content-Type", "application/json"),)):
        self.sock = socket.create_connection(("127.0.0.1", _port(server)), timeout=5.0)
        if recvbuf is not None:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, recvbuf)
        payload = body if body is not None else _sub_body(after_revision)
        head = _head(server, "POST", SUBSCRIBE, auth=auth, extra=extra,
                     content_length=len(payload))
        self.sock.sendall(head + payload)
        self.raw = b""
        self.buffer = b""
        self.eof = False
        self.seen = []
        self.status = None
        self.version = None
        self.headers = {}

    def _fill(self, deadline):
        """Read more bytes until data, EOF or the caller's deadline.

        The per-recv cap only bounds one syscall; it never ends the wait.
        """
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            self.sock.settimeout(max(0.01, min(remaining, 0.25)))
            try:
                chunk = self.sock.recv(65536)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                self.eof = True
                return "eof"
            if not chunk:
                self.eof = True
                return "eof"
            self.raw += chunk
            self.buffer += chunk
            return "data"

    def read_head(self, timeout=20.0):
        deadline = _deadline(timeout)
        while b"\r\n\r\n" not in self.buffer:
            if self._fill(deadline) != "data":
                raise AssertionError(f"no response head, raw={self.raw!r}")
        head, _, rest = self.buffer.partition(b"\r\n\r\n")
        self.buffer = rest
        lines = head.decode("latin-1").split("\r\n")
        version, _, code = lines[0].partition(" ")
        self.version = version
        self.status = int(code.split(" ")[0])
        self.headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            self.headers[name.strip().lower()] = value.strip()
        return self

    def read_frame(self, timeout=20.0):
        """Return the next frame, or None only on a real EOF.

        A bounded wait that runs out raises TimeoutError, so a caller can never
        mistake a slow stream for a closed one.
        """
        deadline = _deadline(timeout)
        while True:
            frame, rest = _take_frame(self.buffer)
            if frame is not None:
                self.buffer = rest
                self.seen.append(frame)
                return frame
            state = self._fill(deadline)
            if state == "data":
                continue
            frame, rest = _take_frame(self.buffer)  # a frame that arrived with the EOF
            self.buffer = rest
            if frame is not None:
                self.seen.append(frame)
                return frame
            if state == "timeout":
                raise TimeoutError(f"no SSE frame within {timeout}s")
            return None

    def read_event(self, timeout=20.0):
        """Read frames until a named event arrives; comment-only frames (the
        heartbeats that never advance a cursor) are skipped."""
        deadline = _deadline(timeout)
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"the stream never sent an event, raw={self.raw!r}"
            frame = self.read_frame(timeout=remaining)
            assert frame is not None, f"the stream ended before the event, raw={self.raw!r}"
            if frame.name is not None:
                return frame

    def read_events(self, count, timeout=20.0):
        deadline = _deadline(timeout)
        return [self.read_event(timeout=deadline - time.monotonic())
                for _ in range(count)]

    def read_comment(self, timeout=20.0):
        """The next comment-only frame (`: heartbeat`), which carries no id."""
        deadline = _deadline(timeout)
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"no SSE comment within {timeout}s, raw={self.raw!r}"
            frame = self.read_frame(timeout=remaining)
            assert frame is not None, f"the stream ended, raw={self.raw!r}"
            if frame.name is None:
                return frame

    def read_ready(self, timeout=20.0):
        try:
            frame = self.read_frame(timeout=timeout)
        except TimeoutError:
            frame = None
        assert frame is not None, f"no : ready comment, raw={self.raw!r}"
        assert frame.name is None and frame.id is None and not frame.data, frame.raw
        assert frame.comment == [b"ready"], frame.raw
        return frame

    def drain_to_eof(self, timeout=60.0):
        deadline = _deadline(timeout)
        while not self.eof and time.monotonic() < deadline:
            try:
                if self.read_frame(timeout=0.25) is None:
                    break
            except TimeoutError:
                continue
        return self.eof

    def revisions(self):
        return [frame for frame in self.seen if frame.name == "revision"]

    def terminal(self):
        return [frame for frame in self.seen
                if frame.name in ("error", "resnapshot_required")]

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _sub_body(revision):
    return b'{"after_revision": "' + revision.encode("ascii") + b'"}'


def _subscriber(server, opened, **kwargs):
    client = _Subscriber(server, **kwargs)
    opened.append(client)
    return client


def _caught_up(server, opened, revision, **kwargs):
    """Open one admitted, caught-up subscription and consume its : ready."""
    client = _subscriber(server, opened, after_revision=revision, **kwargs)
    client.read_head()
    assert client.status == 200, client.raw
    client.read_ready()
    return client


def _subscription_count(server):
    with server._tracking:
        return len(server._subscriptions)


def _worker_count(server):
    with server._tracking:
        return len(server._workers)


def _stopping(server):
    with server._tracking:
        return server._stopping


def _publish_legacy(legacy, count, *, start=None):
    """Publish `count` commits through a second GitStore client of the hub.

    This is the legacy CLI / private proposal publication path: no HTTP
    callback, no in-process notification and no shared coordinator instance.
    """
    revision = start or legacy.remote_head()
    for _ in range(count):
        revision, state = legacy.fetch_state()
        revision = legacy.publish(state, expected_revision=revision)
    return revision


def _legacy_store(wired, name="legacy.git"):
    path = GitStore.create_bare(wired.store.store_path.parent / name, what="store")
    return GitStore(store=path, remote=str(wired.hub))


@pytest.fixture
def opened():
    """Every subscription socket a test builds is closed in one finally block."""
    clients = []
    yield clients
    for client in reversed(clients):
        client.close()


@pytest.fixture
def legacy(wired):
    return _legacy_store(wired)


@pytest.fixture
def aged(wired, legacy):
    """A hub whose early revisions have already fallen out of the replay
    window: WINDOW further commits pushed them behind the bounded enumeration."""
    _publish_legacy(legacy, WINDOW, start=wired.head)
    return SimpleNamespace(legacy=legacy, old=wired.revisions[1])


def _no_echo(reply, leaks):
    """A rejection is ordinary fixed JSON: no stream, no echo, no CORS."""
    text = reply.body.decode("utf-8", "replace")
    for needle in leaks:
        assert needle not in text, needle
    assert reply.headers.get("Content-Type") == "application/json"
    assert b"event:" not in reply.body
    for banned in ("Set-Cookie", "Location", "Access-Control-Allow-Origin"):
        assert reply.headers.get(banned) is None, banned


def _raw_subscribe(server, revision, *, auth=f"Bearer {ALICE}", body=None, extra=(),
                   method="POST", content_type=b"application/json", path=SUBSCRIBE,
                   content_length=None, timeout=30.0):
    """One literal subscribe request (or its refusal) over a real socket.

    Returns a status of None when the listener answered nothing at all, which
    is the legitimate outcome for a dropped connection or worker exhaustion.
    """
    payload = body if body is not None else _sub_body(revision)
    lines = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{_port(server)}"]
    if auth is not None:
        if isinstance(auth, (list, tuple)):
            lines.extend(f"Authorization: {value}" for value in auth)
        else:
            lines.append(f"Authorization: {auth}")
    if content_type is not None:
        if isinstance(content_type, bytes):
            content_type = content_type.decode("latin-1")
        lines.append(f"Content-Type: {content_type}")
    for name, value in extra:
        lines.append(f"{name}: {value}")
    lines.append(f"Content-Length: {content_length if content_length is not None else len(payload)}")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    sock = socket.create_connection(("127.0.0.1", _port(server)), timeout=5.0)
    reply = SimpleNamespace(status=None, headers=None, body=b"", version=None)
    try:
        try:
            sock.sendall(head + payload)
        except (TimeoutError, socket.timeout, OSError):
            return reply  # the listener may answer and close before the write lands
        sock.settimeout(timeout)
        response = http.client.HTTPResponse(sock)
        try:
            response.begin()
        except (TimeoutError, socket.timeout, OSError, http.client.HTTPException):
            return reply
        reply.status = response.status
        reply.headers = response.headers
        reply.body = response.read()
        reply.version = response.version
        return reply
    finally:
        sock.close()


def _forbid_reads(monkeypatch, *, git=True):
    """Fail loudly (and record) on any body read, body parse or git call."""
    seen = []

    def forbidden_read(stream, length):
        seen.append(f"read-body:{length}")
        raise AssertionError("the request body must not be read")

    def forbidden_parse(raw, *, what):
        seen.append(f"parse-body:{len(raw)}")
        raise AssertionError("the request body must not be parsed")

    def forbidden_git(args, **kwargs):
        seen.append(f"git:{list(args)[:1]}")
        raise AssertionError("git must not run")

    monkeypatch.setattr(transport, "_read_body_bytes", forbidden_read)
    monkeypatch.setattr(transport, "parse_json_bytes", forbidden_parse)
    if git:
        monkeypatch.setattr(store_module, "_run_git", forbidden_git)
    return seen


# ------------------------------------------------- constants and response shape


def test_the_admission_and_page_constants_match_the_contract():
    assert (MAX_WORKERS, MAX_SUBSCRIPTIONS, PAGE_LIMIT) == (12, 8, 32)
    assert INTERVAL == 1.0 and WINDOW == 65
    assert SUBSCRIBE in transport._ROUTES


def test_a_caught_up_subscription_streams_ready_then_heartbeat_comments(
    server, opened, leaks
):
    head = _json(_call(server))["revision"]
    client = _caught_up(server, opened, head)
    assert client.version == "HTTP/1.0"
    assert client.headers["content-type"] == "text/event-stream"
    assert "content-length" not in client.headers  # close-delimited body
    assert client.headers["cache-control"] == "no-store"
    assert client.headers["x-content-type-options"] == "nosniff"
    assert client.headers["connection"] == "close"
    assert client.raw.split(b"\r\n\r\n", 1)[1].startswith(b": ready\n\n")
    # A heartbeat arrives within a couple of observation intervals and carries
    # no revision, so it can never advance a cursor.
    frame = client.read_comment(timeout=3 * INTERVAL + 5)
    assert frame is not None and frame.comment == [b"heartbeat"], frame
    assert frame.name is None and frame.id is None and not frame.data
    assert client.raw.count(b"HTTP/1.") == 1  # never a second HTTP response
    assert not client.revisions() and not client.terminal()
    for needle in leaks:
        assert needle.encode() not in client.raw


def test_a_caught_up_subscriber_keeps_streaming_bounded_heartbeats(
    wired, server, opened
):
    client = _caught_up(server, opened, wired.head)
    for _ in range(3):
        frame = client.read_comment(timeout=3 * INTERVAL + 5)
        assert frame is not None and frame.comment == [b"heartbeat"], frame
        assert frame.name is None and frame.id is None and not frame.data
    assert not client.revisions() and not client.terminal()
    assert _subscription_count(server) == 1  # idle observations retain the slot


def test_reconnect_may_redeliver_from_the_client_cursor_and_loses_no_event(
    wired, servers, opened, legacy
):
    """Delivery is replayable, never exactly once: reconnecting with the same
    cursor redelivers, reconnecting with the consumed cursor delivers nothing,
    and no committed revision is ever skipped."""
    server = servers(wired.coordinator).start()
    first = _caught_up(server, opened, wired.head)
    published = _publish_legacy(legacy, 3, start=wired.head)
    delivered = [json.loads(frame.data)["revision"]
                 for frame in first.read_events(3, timeout=15)]
    expected = _finite_replay(server, wired.head)
    assert delivered == expected and published == expected[-1]
    for previous, event in zip(first.revisions(), first.revisions()[1:]):
        assert json.loads(event.data)["previous_revision"] == previous.id
    first.close()

    redelivered = _caught_up(server, opened, wired.head)
    again = [json.loads(frame.data)["revision"]
             for frame in redelivered.read_events(3, timeout=15)]
    assert again == expected  # redelivered, not silently acknowledged
    redelivered.close()

    caught_up = _caught_up(server, opened, published)
    heartbeat = caught_up.read_comment(timeout=3 * INTERVAL + 5)
    assert heartbeat is not None and heartbeat.comment == [b"heartbeat"], heartbeat
    assert not caught_up.revisions() and not caught_up.terminal()


def _finite_replay(server, cursor):
    """Every revision after `cursor`, read back through the finite route."""
    revisions = []
    after = cursor
    while True:
        page = _json(_call(server, REPLAY, _sub_body(after)))
        revisions.extend(event["revision"] for event in page["events"])
        after = page["next_revision"]
        if not page["has_more"]:
            return revisions


# ------------------------------------------------------ authentication first


@pytest.mark.parametrize("auth", [
    None,                       # no Authorization header at all
    "",                         # empty value
    f"Bearer {MALLORY}",        # unknown credential
    "Basic dXNlcjpwYXNz",       # wrong scheme
    "Bearer",                   # scheme without a credential
    (f"Bearer {ALICE}", f"Bearer {BOB}"),  # duplicate header
], ids=["missing", "empty", "invalid", "wrong-scheme", "no-credential", "duplicate"])
def test_subscribe_authentication_precedes_body_parse_replay_and_git(
    wired, servers, monkeypatch, auth, leaks
):
    server = servers(wired.coordinator).start()
    seen = _forbid_reads(monkeypatch)
    real_replay = wired.coordinator.replay

    def counted(credential, *, after_revision, limit=PAGE_LIMIT):
        seen.append(f"replay:{after_revision}")
        return real_replay(credential, after_revision=after_revision, limit=limit)

    monkeypatch.setattr(wired.coordinator, "replay", counted)
    reply = _raw_subscribe(server, wired.head, auth=auth)
    _error(reply, code="access_denied", status=401)
    assert reply.headers.get("WWW-Authenticate") == "Bearer"
    _no_echo(reply, leaks)
    # No body read, no body parse, no replay and no git call of any kind.
    assert seen == [], seen
    assert _subscription_count(server) == 0


@pytest.mark.parametrize("auth", [None, (f"Bearer {ALICE}", f"Bearer {BOB}")],
                         ids=["missing", "duplicate"])
def test_every_reconnect_requires_its_own_bearer_header(
    wired, server, monkeypatch, auth, opened, leaks
):
    live = _caught_up(server, opened, wired.head)
    seen = _forbid_reads(monkeypatch, git=False)
    reply = _raw_subscribe(server, wired.head, auth=auth)
    _error(reply, code="access_denied", status=401)
    _no_echo(reply, leaks)
    assert seen == [], seen
    # The already authenticated stream is untouched by the refused reconnect.
    assert _subscription_count(server) == 1
    frame = live.read_comment(timeout=3 * INTERVAL + 5)
    assert frame is not None and frame.comment == [b"heartbeat"], frame


# ------------------------------------------------------ body and route guards


@pytest.mark.parametrize("body", [
    b"{}",                                              # missing cursor
    b'{"after_revision": "__REV__", "limit": 32}',      # finite replay key
    b'{"after_revision": "__REV__", "cursor": "__REV__"}',  # alias guess
    b'{"after_revision": 5}',                           # not a revision string
    b'{"after_revision": null}',                        # null cursor
    b'{"after_revision": "not-a-revision"}',            # malformed cursor
    b'["__REV__"]',                                     # not a JSON object
], ids=["missing", "limit-key", "alias", "integer", "null", "malformed", "array"])
def test_subscribe_bodies_must_be_exactly_after_revision(
    wired, servers, body, leaks
):
    server = servers(wired.coordinator).start()
    reply = _raw_subscribe(server, wired.head,
                           body=body.replace(b"__REV__", wired.head.encode()))
    _error(reply, code="invalid_request", status=400)
    _no_echo(reply, leaks)
    assert _subscription_count(server) == 0


def test_last_event_id_is_rejected_and_only_after_authentication(wired, servers, leaks):
    server = servers(wired.coordinator).start()
    refused = _raw_subscribe(server, wired.head, auth=None,
                             extra=(("Last-Event-ID", wired.head),))
    _error(refused, code="access_denied", status=401)  # auth still comes first
    rejected = _raw_subscribe(server, wired.head, extra=(("Last-Event-ID", wired.head),))
    _error(rejected, code="invalid_request", status=400)
    _no_echo(rejected, leaks)
    assert wired.head.encode() not in rejected.body  # no cursor is echoed back
    assert _subscription_count(server) == 0


@pytest.mark.parametrize("case", ["method", "unknown", "query", "media", "length"])
def test_subscribe_route_and_framing_guards(wired, servers, case, leaks):
    server = servers(wired.coordinator).start()
    if case == "method":
        reply = _raw_subscribe(server, wired.head, method="GET")
        _error(reply, status=405)
        assert reply.headers.get("Allow") == "POST"
    elif case == "unknown":
        reply = _raw_subscribe(server, wired.head, path="/v1/stream")
        _error(reply, status=404)
    elif case == "query":
        reply = _raw_subscribe(server, wired.head, path=f"{SUBSCRIBE}?after_revision=x")
        _error(reply, status=400)
    elif case == "media":
        reply = _raw_subscribe(server, wired.head, content_type=b"text/plain")
        _error(reply, status=415)
    else:
        reply = _raw_subscribe(server, wired.head, extra=(("Content-Length", "1"),))
        _error(reply, status=400)
    _no_echo(reply, leaks)
    assert _subscription_count(server) == 0


def test_an_unknown_initial_cursor_is_gone_before_the_stream(wired, servers, leaks):
    server = servers(wired.coordinator).start()
    reply = _raw_subscribe(server, "f" * 40)
    _error(reply, code="replay_unavailable", status=410)
    _no_echo(reply, leaks)
    assert _subscription_count(server) == 0  # the reserved slot was released


def test_an_expired_initial_cursor_is_gone(wired, servers, aged, leaks):
    server = servers(wired.coordinator).start()
    assert aged.legacy.remote_head() != aged.old  # the old cursor left the window
    reply = _raw_subscribe(server, aged.old)
    _error(reply, code="replay_unavailable", status=410)
    _no_echo(reply, leaks)
    assert _subscription_count(server) == 0


# ----------------------------------------------- replay paging and publication


def test_a_large_backlog_is_replayed_as_ordered_pages_of_32(
    wired, servers, legacy, opened, monkeypatch
):
    """40 legacy-published commits arrive in strict revision order, every page
    is bounded to 32 events, and each frame carries the existing ReplayEvent
    fields with the revision as its id."""
    cursor = wired.head
    published = _publish_legacy(legacy, 40, start=cursor)
    server = servers(wired.coordinator).start()
    expected = _finite_replay(server, cursor)
    assert len(expected) == 40 and expected[-1] == published

    pages = []
    real_replay = wired.coordinator.replay

    def tracked(credential, *, after_revision, limit=PAGE_LIMIT):
        page = real_replay(credential, after_revision=after_revision, limit=limit)
        pages.append((limit, len(page.events)))
        return page

    monkeypatch.setattr(wired.coordinator, "replay", tracked)
    client = _caught_up(server, opened, cursor)
    frames = client.read_events(40, timeout=30)
    events = [json.loads(frame.data) for frame in frames]
    assert [event["revision"] for event in events] == expected
    for frame, event in zip(frames, events):
        assert frame.name == "revision"
        assert frame.id == event["revision"]  # the id is the revision
        assert set(event) == EVENT_FIELDS, event
        assert len(frame.id) == 40
        assert all(char in "0123456789abcdef" for char in frame.id)
    assert events[0]["previous_revision"] == cursor
    for previous, event in zip(events, events[1:]):
        assert event["previous_revision"] == previous["revision"]
    assert all(limit == PAGE_LIMIT and size <= PAGE_LIMIT for limit, size in pages)
    assert [size for _, size in pages[:2]] == [32, 8]
    assert sum(size for _, size in pages) == 40  # caught-up polls may be empty
    assert not client.terminal()


def test_a_no_op_commit_still_advances_the_cursor(wired, server, legacy, opened):
    client = _caught_up(server, opened, wired.head)
    head, state = legacy.fetch_state()
    noop = legacy.publish(state, expected_revision=head)  # semantically unchanged
    frame = client.read_event(timeout=LIVE)
    event = json.loads(frame.data)
    assert frame.name == "revision" and frame.id == event["revision"] == noop
    assert event["previous_revision"] == head
    assert event["context_changed"] is False and event["changed_task_ids"] == []
    following = _publish_legacy(legacy, 1, start=noop)
    second = client.read_event(timeout=LIVE)
    payload = json.loads(second.data)
    assert second.id == following
    assert payload["previous_revision"] == noop  # the no-op revision was persisted


def test_legacy_git_publication_is_observed_without_an_http_callback(
    wired, server, legacy, opened
):
    client = _caught_up(server, opened, wired.head)
    assert legacy.remote_head() == wired.head
    published = _publish_legacy(legacy, 1, start=wired.head)  # only git ran
    frame = client.read_event(timeout=LIVE)
    event = json.loads(frame.data)
    assert frame.id == event["revision"] == published == legacy.remote_head()
    assert event["previous_revision"] == wired.head
    assert set(event) == EVENT_FIELDS
    assert not client.terminal()


def test_sequential_cas_writes_stream_in_order_and_the_stale_one_is_409(
    wired, server, opened, leaks
):
    client = _caught_up(server, opened, wired.head)
    first = _call(server, TASK_STATUS,
                  _route_body(TASK_STATUS, wired.head).replace(b"running", b"done"))
    assert first.status == 200, first.body
    revision = _json(first)["revision"]
    # A competing write on the stale expected_revision stays an ordinary finite
    # JSON reply and must not disturb the open stream.
    stale = _call(server, TASK_STATUS, _route_body(TASK_STATUS, wired.head))
    _error(stale, code="stale_revision", status=409)
    _no_echo(stale, leaks)
    second = _call(server, TASK_STATUS,
                   _route_body(TASK_STATUS, revision).replace(b"done", b"running"))
    assert second.status == 200, second.body
    published = _json(second)["revision"]

    frames = client.read_events(2, timeout=LIVE)
    assert len(frames) == 2, [frame.raw for frame in frames]
    events = [json.loads(frame.data) for frame in frames]
    assert [frame.id for frame in frames] == [event["revision"] for event in events]
    assert [event["revision"] for event in events] == [revision, published]
    assert events[0]["previous_revision"] == wired.head
    assert events[1]["previous_revision"] == events[0]["revision"]
    assert events[0]["changed_task_ids"] == ["t-ui"]
    assert events[0]["context_changed"] is False and events[1]["context_changed"] is False
    assert not client.terminal()


# ---------------------------------------------------------- terminal failures


def test_a_core_failure_midstream_is_one_terminal_event_without_secrets(
    wired, server, opened, monkeypatch, caplog, capsys, leaks
):
    client = _caught_up(server, opened, wired.head)
    calls = []
    real_replay = wired.coordinator.replay

    def failing(credential, *, after_revision, limit=PAGE_LIMIT):
        calls.append(after_revision)
        if len(calls) == 1:
            return real_replay(credential, after_revision=after_revision, limit=limit)
        raise GitOperationError(f"git push refused in {wired.store.store_path} with {ALICE}")

    monkeypatch.setattr(wired.coordinator, "replay", failing)
    frame = client.read_event(timeout=LIVE)
    assert frame.name == "error" and frame.id is None, frame.raw
    assert json.loads(frame.data) == {"error": {"code": "session_unavailable",
                                                "message": transport._SESSION_UNAVAILABLE}}
    assert client.drain_to_eof(timeout=20)
    assert [other.name for other in client.seen[1:] if other.name] == ["error"]
    assert client.raw.count(b"HTTP/1.") == 1  # never a second HTTP response
    for needle in leaks:
        assert needle.encode() not in client.raw, needle
    _silent(caplog, capsys)


def test_an_unexpected_core_failure_is_a_fixed_internal_error(
    wired, server, opened, monkeypatch
):
    client = _caught_up(server, opened, wired.head)
    calls = []
    real_replay = wired.coordinator.replay

    def failing(credential, *, after_revision, limit=PAGE_LIMIT):
        calls.append(after_revision)
        if len(calls) == 1:
            return real_replay(credential, after_revision=after_revision, limit=limit)
        raise RuntimeError("secret diagnostic detail")

    monkeypatch.setattr(wired.coordinator, "replay", failing)
    frame = client.read_event(timeout=LIVE)
    assert frame.name == "error" and frame.id is None
    assert json.loads(frame.data) == {"error": {"code": "internal_error",
                                                "message": transport._INTERNAL_ERROR}}
    assert b"secret diagnostic detail" not in client.raw


def test_an_overtaken_cursor_terminates_with_resnapshot_required(
    wired, server, legacy, opened, monkeypatch, leaks
):
    """A stream whose cursor is overtaken while its own core call is in flight
    gets the fixed terminal resnapshot_required event, with no id and then EOF;
    it is never silently skipped forward."""
    client = _caught_up(server, opened, wired.head)
    calls = []
    parked, released = threading.Event(), threading.Event()
    real_replay = wired.coordinator.replay

    def parked_streaming(credential, *, after_revision, limit=PAGE_LIMIT):
        calls.append(after_revision)
        if len(calls) == 1:
            return real_replay(credential, after_revision=after_revision, limit=limit)
        parked.set()
        assert released.wait(timeout=120), "the in-flight replay was never released"
        return real_replay(credential, after_revision=after_revision, limit=limit)

    monkeypatch.setattr(wired.coordinator, "replay", parked_streaming)
    try:
        assert parked.wait(timeout=LIVE), "the stream never reached its core replay"
        _publish_legacy(legacy, WINDOW, start=wired.head)  # overtaken while parked
    finally:
        released.set()
    frame = client.read_event(timeout=30)
    assert frame.name == "resnapshot_required" and frame.id is None, frame.raw
    assert json.loads(frame.data) == {"error": {"code": "replay_unavailable",
                                                "message": transport._REPLAY_UNAVAILABLE}}
    assert client.drain_to_eof(timeout=10)
    assert not client.revisions()  # nothing was silently skipped forward
    assert [item.name for item in client.seen if item.name] == ["resnapshot_required"]
    assert client.raw.count(b"HTTP/1.") == 1
    for needle in leaks:
        assert needle.encode() not in client.raw


# --------------------------------------------------------- admission limits


def test_the_ninth_subscription_is_refused_before_replay(
    wired, server, opened, monkeypatch, leaks
):
    for _ in range(MAX_SUBSCRIPTIONS):
        _caught_up(server, opened, wired.head)
    assert _wait_until(lambda: _subscription_count(server) == MAX_SUBSCRIPTIONS)
    calls = []
    real_replay = wired.coordinator.replay

    def counted(credential, *, after_revision, limit=PAGE_LIMIT):
        # Existing streams poll independently: count only the refused request's
        # principal so a heartbeat crossing this assertion cannot make it flaky.
        if credential == BOB:
            calls.append(after_revision)
        return real_replay(credential, after_revision=after_revision, limit=limit)

    monkeypatch.setattr(wired.coordinator, "replay", counted)
    reply = _raw_subscribe(server, wired.head, auth=f"Bearer {BOB}")
    _error(reply, code="subscriber_limit", status=503)
    _no_echo(reply, leaks)
    assert calls == []  # the limit answers before any replay or stream header
    # Streams alone cannot occupy every worker: a finite command still works.
    assert _call(server, SNAPSHOT, b"{}", timeout=20).status == 200


def test_worker_exhaustion_closes_the_socket_without_parsing_or_authenticating(
    wired, server, opened, monkeypatch
):
    for _ in range(MAX_SUBSCRIPTIONS):
        _caught_up(server, opened, wired.head)
    assert _wait_until(lambda: _worker_count(server) == MAX_SUBSCRIPTIONS)
    finite = MAX_WORKERS - MAX_SUBSCRIPTIONS
    released, all_parked = threading.Event(), threading.Event()
    state = SimpleNamespace(count=0, snapshot_calls=0)
    guard = threading.Lock()
    real_snapshot = wired.coordinator.snapshot
    parses = []
    real_parse = transport.parse_json_bytes

    def parked_snapshot(credential):
        with guard:
            state.snapshot_calls += 1
            state.count += 1
            if state.count == finite:
                all_parked.set()
        assert released.wait(timeout=90), "the parked request was never released"
        return real_snapshot(credential)

    def counted_parse(raw, *, what):
        parses.append(len(raw))
        return real_parse(raw, what=what)

    monkeypatch.setattr(wired.coordinator, "snapshot", parked_snapshot)
    monkeypatch.setattr(transport, "parse_json_bytes", counted_parse)
    threads = []
    try:
        for index in range(finite):
            thread = threading.Thread(
                target=functools.partial(_call, server, SNAPSHOT, b"{}", timeout=90),
                daemon=True, name=f"parked-{index}",
            )
            threads.append(thread)
            thread.start()
        assert all_parked.wait(timeout=30), "the finite requests never parked"
        assert _wait_until(lambda: _worker_count(server) == MAX_WORKERS)
        baseline = (len(parses), state.snapshot_calls)
        sock = socket.create_connection(("127.0.0.1", _port(server)), timeout=5.0)
        try:
            sock.sendall(_head(server, "POST", SNAPSHOT) + b"{}")
            sock.settimeout(10.0)
            data = b""
            closed = False
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        closed = True
                        break
                    data += chunk
            except ConnectionError:
                closed = True  # a TCP reset is also a real admission refusal
        finally:
            sock.close()
        assert closed, "worker exhaustion must close, not leave the socket idle"
        assert data == b"", data  # closed without parsing, auth or metadata
        assert (len(parses), state.snapshot_calls) == baseline
    finally:
        released.set()
        for thread in threads:
            thread.join(timeout=60)
    assert all(not thread.is_alive() for thread in threads)


def test_a_disconnected_subscriber_frees_its_slot(wired, server, opened):
    streams = [_caught_up(server, opened, wired.head)
               for _ in range(MAX_SUBSCRIPTIONS)]
    assert _wait_until(lambda: _subscription_count(server) == MAX_SUBSCRIPTIONS)
    streams[0].sock.shutdown(socket.SHUT_RDWR)
    streams[0].close()
    assert _wait_until(lambda: _subscription_count(server) == MAX_SUBSCRIPTIONS - 1), (
        "the disconnected subscriber never released its slot"
    )
    reused = _caught_up(server, opened, wired.head)  # the freed slot is reusable
    assert reused.status == 200
    assert _subscription_count(server) == MAX_SUBSCRIPTIONS


# ----------------------------------------------------------------- shutdown


def test_shutdown_eofs_idle_streams_and_close_returns_promptly(wired, servers, opened):
    server = servers(wired.coordinator).start()
    streams = [_caught_up(server, opened, wired.head) for _ in range(3)]
    started = time.monotonic()
    server.close()
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, elapsed  # idle streams are woken, not waited out
    for stream in streams:
        assert stream.drain_to_eof(timeout=10)
        assert stream.raw.count(b"HTTP/1.") == 1
        assert not stream.terminal()  # EOF, no terminal event for a local wake-up
    with server._tracking:
        assert server._worker_threads == set()  # every worker was joined
    with socket.socket() as probe:
        probe.settimeout(1.0)
        with pytest.raises((ConnectionError, TimeoutError, OSError)):
            probe.connect(("127.0.0.1", _port(server)))


def test_concurrent_closers_share_the_drain_of_a_parked_finite_request(
    wired, server, monkeypatch
):
    parked, released = threading.Event(), threading.Event()
    real_snapshot = wired.coordinator.snapshot
    outcome: dict = {}

    def parked_snapshot(credential):
        parked.set()
        assert released.wait(timeout=90), "the parked request was never released"
        return real_snapshot(credential)

    monkeypatch.setattr(wired.coordinator, "snapshot", parked_snapshot)

    def request():
        try:
            outcome["reply"] = _call(server, SNAPSHOT, b"{}", timeout=90)
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcome["request-error"] = exc

    def close(name):
        try:
            outcome[name] = server.close()
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcome["close-error"] = exc

    worker = threading.Thread(target=request, daemon=True, name="drained-request")
    closers = [threading.Thread(target=close, args=(f"close-{index}",), daemon=True,
                                name=f"closer-{index}") for index in (1, 2)]
    worker.start()
    try:
        assert parked.wait(timeout=30)
        for thread in closers:
            thread.start()
        for thread in closers:
            thread.join(timeout=0.5)
        assert [thread.is_alive() for thread in closers] == [True, True]
        assert "close-error" not in outcome and "reply" not in outcome, outcome
    finally:
        released.set()
        worker.join(timeout=60)
        for thread in closers:
            thread.join(timeout=60)
    assert all(not thread.is_alive() for thread in [worker, *closers])
    assert "request-error" not in outcome and "close-error" not in outcome, outcome
    assert outcome["reply"].status == 200
    assert outcome["close-1"] is None and outcome["close-2"] is None


def test_a_subscription_racing_close_never_writes_stream_headers(
    wired, server, monkeypatch
):
    parked, released = threading.Event(), threading.Event()
    real_replay = wired.coordinator.replay

    def parked_initial(credential, *, after_revision, limit=PAGE_LIMIT):
        parked.set()
        assert released.wait(timeout=90), "the parked validation was never released"
        return real_replay(credential, after_revision=after_revision, limit=limit)

    monkeypatch.setattr(wired.coordinator, "replay", parked_initial)
    outcome: dict = {}

    def subscribe():
        try:
            outcome["reply"] = _raw_subscribe(server, wired.head, timeout=60)
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcome["subscribe-error"] = exc

    def close():
        try:
            outcome["close"] = server.close()
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcome["close-error"] = exc

    subscriber = threading.Thread(target=subscribe, daemon=True, name="racing-subscribe")
    closer = threading.Thread(target=close, daemon=True, name="racing-close")
    subscriber.start()
    try:
        assert parked.wait(timeout=30), "the subscription never validated its page"
        closer.start()
        assert _wait_until(lambda: _stopping(server), timeout=15)
        assert closer.is_alive()  # the drain waits for the validating worker
    finally:
        released.set()
        subscriber.join(timeout=60)
        closer.join(timeout=60)
    assert not closer.is_alive() and not subscriber.is_alive()
    assert "close-error" not in outcome and "subscribe-error" not in outcome, outcome
    # Shutdown stops admission before the first page, so no 200 and no frame.
    assert outcome["reply"].status is None, outcome["reply"].body
    assert _subscription_count(server) == 0


def test_an_in_flight_stream_replay_is_drained_before_close_completes(
    wired, server, opened, monkeypatch
):
    client = _caught_up(server, opened, wired.head)
    parked, released = threading.Event(), threading.Event()
    real_replay = wired.coordinator.replay
    calls = []

    def parked_streaming(credential, *, after_revision, limit=PAGE_LIMIT):
        calls.append(after_revision)
        if len(calls) == 1:
            return real_replay(credential, after_revision=after_revision, limit=limit)
        parked.set()
        assert released.wait(timeout=90), "the in-flight replay was never released"
        return real_replay(credential, after_revision=after_revision, limit=limit)

    monkeypatch.setattr(wired.coordinator, "replay", parked_streaming)
    outcome: dict = {}

    def close():
        try:
            outcome["close"] = server.close()
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcome["close-error"] = exc

    closer = threading.Thread(target=close, daemon=True, name="drain-close")
    try:
        assert parked.wait(timeout=LIVE), "the stream never reached its core replay"
        closer.start()
        closer.join(timeout=0.5)
        assert closer.is_alive()  # close() drains the in-flight core call
    finally:
        released.set()
        closer.join(timeout=60)
    assert not closer.is_alive()
    assert "close-error" not in outcome, outcome
    assert client.drain_to_eof(timeout=10)
    assert not client.terminal()


def test_close_from_a_request_worker_fails_instead_of_deadlocking(
    wired, server, monkeypatch
):
    recorded = []
    real_snapshot = wired.coordinator.snapshot

    def snapshot_from_worker(credential):
        try:
            server.close()
        except ValidationError as error:
            recorded.append(str(error))
        else:
            recorded.append("close() unexpectedly succeeded")
        return real_snapshot(credential)

    monkeypatch.setattr(wired.coordinator, "snapshot", snapshot_from_worker)
    reply = _call(server, SNAPSHOT, b"{}", timeout=20)
    assert reply.status == 200, reply.body
    assert len(recorded) == 1 and "worker" in recorded[0], recorded
    # The refused close left the listener and the finite routes usable.
    assert _call(server, SNAPSHOT, b"{}", timeout=20).status == 200


def test_close_from_the_accept_thread_fails_instead_of_deadlocking(
    wired, servers, monkeypatch
):
    server = servers(wired.coordinator)
    recorded = []
    real_actions = server._server.service_actions

    def actions():
        if not recorded:
            try:
                server.close()
            except ValidationError as error:
                recorded.append(str(error))
            else:
                recorded.append("close() unexpectedly succeeded")
        else:
            real_actions()

    monkeypatch.setattr(server._server, "service_actions", actions, raising=False)
    server.start()
    assert _wait_until(lambda: bool(recorded), timeout=20), "the accept thread never ran"
    assert len(recorded) == 1 and "accept or request worker" in recorded[0], recorded
    assert _call(server, SNAPSHOT, b"{}", timeout=20).status == 200


# -------------------------------------------------------- slow subscribers


def _tiny_send_buffer(monkeypatch, server, *, sndbuf=512):
    """Force a tiny server SO_SNDBUF so a non-reading client blocks a write."""
    real_get_request = server._server.get_request

    def get_request():
        connection, address = real_get_request()
        try:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
        except OSError:
            pass
        return connection, address

    monkeypatch.setattr(server._server, "get_request", get_request, raising=False)


def _synthetic_pages(count_pages, *, events=PAGE_LIMIT):
    """Bounded synthetic pages: enough frame bytes to fill a tiny socket."""
    revisions = [f"{index + 1:040x}" for index in range(count_pages * events)]
    previous = "f" * 40
    pages = []
    for number in range(count_pages):
        chunk = revisions[number * events : (number + 1) * events]
        page = tuple(ReplayEvent(previous, revision, False, ()) for revision in chunk)
        previous = chunk[-1]
        pages.append(ReplayPage(revisions[-1], previous, page, number + 1 < count_pages))
    return pages


def _blocked_stream(wired, server, opened, monkeypatch, *, pages=6):
    """One admitted subscription whose socket write is blocked by a
    non-reading client on a tiny send buffer.

    Returns the client plus the write ledger: `entered`/`exited` bracket each
    frame the stream attempted, and `written` is the byte length it tried to
    hand to the socket.
    """
    if os.name == "nt":
        pytest.skip("tiny TCP send-buffer backpressure is OS-dependent on Windows")
    _tiny_send_buffer(monkeypatch, server)
    payload = _synthetic_pages(pages)
    ledger = SimpleNamespace(index=0, entered=[], exited=[], written=0)

    def synthetic(credential, *, after_revision, limit=PAGE_LIMIT):
        if ledger.index < len(payload):
            page = payload[ledger.index]
            ledger.index += 1
            return page
        return ReplayPage(payload[-1].next_revision, payload[-1].next_revision, (), False)

    real_write = transport.LoopbackServer._write_event

    def tracked(handler, name, event_payload, *, revision=None):
        ledger.entered.append(name)
        ledger.written += _frame_bytes(name, event_payload, revision)
        try:
            return real_write(handler, name, event_payload, revision=revision)
        finally:
            ledger.exited.append(name)

    def blocked():
        return (len(ledger.entered) >= 5
                and len(ledger.exited) < len(ledger.entered))

    monkeypatch.setattr(wired.coordinator, "replay", synthetic)
    monkeypatch.setattr(transport.LoopbackServer, "_write_event", staticmethod(tracked))
    client = _Subscriber(server, after_revision="f" * 40, recvbuf=512)
    opened.append(client)
    client.sock.settimeout(30.0)
    assert _wait_until(blocked, timeout=30), (
        "the stream never blocked a write on the tiny send buffer"
    )
    return client, ledger


def _frame_bytes(name, event_payload, revision):
    size = len(f"event: {name}\n".encode("ascii")) + len(b"data: ")
    size += len(canonical_json_bytes(event_payload)) + 2
    if revision is not None:
        size += len(f"id: {revision}\n".encode("ascii"))
    return size


def test_a_slow_subscriber_never_holds_the_coordinator_lock(
    wired, server, opened, monkeypatch
):
    client, ledger = _blocked_stream(wired, server, opened, monkeypatch)
    assert len(ledger.entered) > len(ledger.exited)
    # A frame write is pending on the socket while the coordinator lock is free
    # and a finite command still completes on the very same core.
    assert wired.coordinator._lock.acquire(timeout=5)
    wired.coordinator._lock.release()
    reply = _call(server, SNAPSHOT, b"{}", timeout=20)
    assert reply.status == 200, reply.body
    # The still-blocking stream gives up on its socket timeout, which is EOF with
    # no terminal event; the reader only looks at the bytes after that.
    assert _wait_until(lambda: len(ledger.entered) == len(ledger.exited), timeout=25), (
        "the slow reader never ended the blocked stream write"
    )
    assert client.drain_to_eof(timeout=20)
    # The write really was blocked: fewer bytes reached the client than the
    # stream handed to the socket.
    assert 0 < len(client.raw) < ledger.written, (len(client.raw), ledger.written)
    assert not client.terminal()
    assert client.raw.count(b"HTTP/1.") == 1


def test_shutdown_interrupts_a_blocked_stream_write(
    wired, server, opened, monkeypatch
):
    client, ledger = _blocked_stream(wired, server, opened, monkeypatch)
    assert len(ledger.exited) < len(ledger.entered)
    started = time.monotonic()
    server.close()
    elapsed = time.monotonic() - started
    # The socket timeout alone would take five seconds, so a prompt close proves
    # the shutdown really interrupted the blocked write and then drained it.
    assert elapsed < 2.0, elapsed
    assert client.drain_to_eof(timeout=10)
    assert len(ledger.entered) == len(ledger.exited)
    assert len(client.raw) < ledger.written
    with server._tracking:
        assert server._worker_threads == set()


# -------------------------------------------------------- lead race regressions


def test_initial_replay_page_is_released_before_fetching_the_next_page(
    wired, server, opened, monkeypatch
):
    observed = threading.Event()
    initial = None
    retained = []
    real_replay = wired.coordinator.replay

    def tracked(credential, *, after_revision, limit=PAGE_LIMIT):
        nonlocal initial
        if initial is not None:
            retained.append(initial() is not None)
            observed.set()
        page = real_replay(credential, after_revision=after_revision, limit=limit)
        if initial is None:
            initial = weakref.ref(page)
        return page

    monkeypatch.setattr(wired.coordinator, "replay", tracked)
    _caught_up(server, opened, wired.head)
    assert observed.wait(timeout=LIVE)
    assert retained and not any(retained), "the parent call retained the initial replay page"


def test_concurrent_cas_writes_with_a_live_stream_publish_only_the_winner(
    wired, server, opened
):
    stream = _caught_up(server, opened, wired.head)
    barrier = threading.Barrier(2, timeout=20)
    outcomes = {}

    def write(status):
        try:
            barrier.wait()
            outcomes[status] = _call(
                server, TASK_STATUS,
                _route_body(TASK_STATUS, wired.head).replace(b"running", status.encode()),
                timeout=30,
            )
        except Exception as error:
            outcomes[status] = error

    workers = [threading.Thread(target=write, args=(status,), daemon=True)
               for status in ("running", "done")]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=40)
        assert all(not worker.is_alive() for worker in workers)
        assert all(not isinstance(reply, Exception) for reply in outcomes.values()), outcomes
        assert sorted(reply.status for reply in outcomes.values()) == [200, 409]
        winner = _json(next(reply for reply in outcomes.values() if reply.status == 200))["revision"]
        event = stream.read_event(timeout=LIVE)
        assert event.id == winner
        assert json.loads(event.data)["previous_revision"] == wired.head
        assert _finite_replay(server, wired.head) == [winner]
    finally:
        barrier.abort()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=40)


def test_hub_advancing_after_initial_replay_is_not_lost_before_stream_start(
    wired, server, legacy, opened, monkeypatch
):
    parked, released = threading.Event(), threading.Event()
    real_replay = wired.coordinator.replay
    first = True

    def pinned(credential, *, after_revision, limit=PAGE_LIMIT):
        nonlocal first
        page = real_replay(credential, after_revision=after_revision, limit=limit)
        if first:
            first = False
            parked.set()
            assert released.wait(timeout=30)
        return page

    monkeypatch.setattr(wired.coordinator, "replay", pinned)
    client = _subscriber(server, opened, after_revision=wired.head)
    try:
        assert parked.wait(timeout=20)
        published = _publish_legacy(legacy, 1)
    finally:
        released.set()
    client.read_head()
    assert client.status == 200
    client.read_ready()
    event = client.read_event(timeout=LIVE)
    assert event.id == published
    assert json.loads(event.data)["previous_revision"] == wired.head


def test_shutdown_before_subscription_registration_refuses_the_stream(
    wired, server, monkeypatch
):
    parked, released = threading.Event(), threading.Event()
    real_read = transport._read_body_bytes
    outcomes = {}

    def pending_body(reader, length):
        body = real_read(reader, length)
        parked.set()
        assert released.wait(timeout=30)
        return body

    monkeypatch.setattr(transport, "_read_body_bytes", pending_body)

    def subscribe():
        outcomes["reply"] = _raw_subscribe(server, wired.head)

    def close():
        outcomes["close"] = server.close()

    requester = threading.Thread(target=subscribe, daemon=True)
    closer = threading.Thread(target=close, daemon=True)
    requester.start()
    try:
        assert parked.wait(timeout=20)
        assert _subscription_count(server) == 0
        closer.start()
        assert _wait_until(lambda: _stopping(server))
        assert closer.is_alive()
    finally:
        released.set()
        requester.join(timeout=40)
        if closer.ident is not None:
            closer.join(timeout=40)
    assert not requester.is_alive() and not closer.is_alive()
    _error(outcomes["reply"], code="session_unavailable", status=503)
    assert outcomes["close"] is None
    assert _subscription_count(server) == 0


def test_credential_replacement_drains_old_streams_and_reauthenticates_reconnect(
    wired, servers, opened
):
    first = servers(wired.coordinator).start()
    stream = _caught_up(first, opened, wired.head)
    first.close()
    assert stream.drain_to_eof(timeout=10)
    with first._tracking:
        assert not first._worker_threads and not first._subscriptions

    replacement_token = "R" * 40
    replacement = Coordinator(
        wired.store, session_id="demo-1", owner_id="alice",
        member_credentials={"alice": replacement_token, "bob": BOB},
    )
    second = servers(replacement).start()
    refused = _raw_subscribe(second, wired.head, auth=f"Bearer {ALICE}")
    _error(refused, code="access_denied", status=401)
    revision = replacement.update_task_status(
        BOB, task_id="t-ui", status="done", expected_revision=wired.head,
    )
    resumed = _caught_up(second, opened, wired.head, auth=f"Bearer {replacement_token}")
    assert resumed.read_event(timeout=LIVE).id == revision
