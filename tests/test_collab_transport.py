"""Bounded tests for collab_runtime.transport (the loopback HTTP/JSON v1 slice).

Every request here is a real socket or native HTTPConnection to the literal
IPv4 address 127.0.0.1 that the listener itself bound: no external service, no
environment proxy, no hostname resolution and no `requests`. Only temp bare
hub/store metadata is created (base 'a'*40, no source checkout, no install,
API key, stage, commit or cleanup of anything outside tmp_path).

Covers the "Loopback HTTP/JSON v1 contract" of docs/COLLABORATION.md:
address/bind/port/lifecycle rules, the four authenticated POST routes over
HTTP, authentication strictly before any body read, body parse or git call,
authority/origin/query/framing guards, the separate 64 KiB head budget and
64 KiB JSON envelope budget, fixed safe error shapes with no echo of URLs,
paths, bodies, credentials, context, git diagnostics or tracebacks, the
stop-and-replace credential lifecycle, and three deterministic races driven by
threading events (never sleeps).
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import shutil
import socket
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collab_runtime import store as store_module  # noqa: E402
from collab_runtime import transport  # noqa: E402
from collab_runtime.coordinator import Coordinator  # noqa: E402
from collab_runtime.errors import (  # noqa: E402
    AccessDeniedError,
    CollabError,
    GitOperationError,
    ReplayUnavailableError,
    SessionNotFoundError,
    StaleRevisionError,
    ValidationError,
)
from collab_runtime.models import (  # noqa: E402
    build_context,
    build_initial_state,
    build_task,
)
from collab_runtime.store import GitStore  # noqa: E402
from collab_runtime.transport import LoopbackServer  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadı")

SHA0 = "a" * 40
SESSION_ID = "demo-1"
MAX_HEAD = 65536  # the request head budget of the v1 contract, in bytes
MAX_ENVELOPE = 65536  # the JSON envelope budget of the v1 contract, in bytes

ALICE = "A" * 40  # configured owner
BOB = "B" * 40  # assignee of t-ui
OTHER = "C" * 40  # assignee of t-api
MALLORY = "M" * 40  # never configured anywhere
DAVE = "D" * 40  # only in the replacement mapping
MEMBERS = {"alice": ALICE, "bob": BOB, "othermember": OTHER}
REPLACED = {"bob": BOB, "othermember": OTHER, "newcomer": DAVE}

SNAPSHOT, CONTEXT, TASK_STATUS, REPLAY = (
    "/v1/snapshot", "/v1/context", "/v1/task-status", "/v1/replay",
)
ROUTES = (SNAPSHOT, CONTEXT, TASK_STATUS, REPLAY)
# One valid body per route; __REV__ is filled with a current revision.
ROUTE_BODY = {
    SNAPSHOT: b"{}",
    CONTEXT: b'{"expected_revision": "__REV__", "context": '
             b'{"goal": "g", "decisions": [], "interfaces": {}}}',
    TASK_STATUS: b'{"expected_revision": "__REV__", "task_id": "t-ui", "status": "running"}',
    REPLAY: b'{"after_revision": "__REV__"}',
}
EMPTY = b"{}"
JSON_CT = (("Content-Type", b"application/json"),)

Reply = namedtuple("Reply", "status headers body version")
NO_REPLY = Reply(None, None, b"", None)

# Core failure classes and the fixed status/code the contract promises.
CORE_FAILURES = [
    (AccessDeniedError, 403, "access_denied"),
    (StaleRevisionError, 409, "stale_revision"),
    (ReplayUnavailableError, 410, "replay_unavailable"),
    (ValidationError, 400, "invalid_request"),
    (SessionNotFoundError, 503, "session_unavailable"),
    (GitOperationError, 503, "session_unavailable"),
    (CollabError, 500, "internal_error"),
    (RuntimeError, 500, "internal_error"),
]
CORE_FAILURE_IDS = [error.__name__ for error, _, _ in CORE_FAILURES]

# Paths that would mean a credential-management, invite, pairing or code
# channel exists. The v1 slice must answer 404 instead of implementing them.
UNSUPPORTED_PATHS = (
    "/v1/credentials", "/v1/rotate", "/v1/revoke", "/v1/invite", "/v1/pair",
    "/v1/proposal", "/v1/patch", "/v1/code", "/v1/candidates", "/v1/events",
    "/", "/index.html", "/static/app.js",
)


# ------------------------------------------------------------------ seeding


def _seed(hub, tmp_path, name="transport.store.git"):
    """Temp bare hub + one client store seeded from base 'a'*40 (no source
    checkout): initial state, one context publish, then two trusted tasks."""
    path = GitStore.create_bare(tmp_path / name, what="store")
    store = GitStore(store=path, remote=str(hub))
    revision = store.init_session(
        build_initial_state(
            session_id=SESSION_ID, target_version="v0.1 transport", base_commit=SHA0
        )
    )
    revisions = [revision]
    revision = store.publish(
        store.fetch_state()[1].with_context(
            build_context(goal="shared goal", decisions=["keep it small"],
                          interfaces={"api": "REST"})
        ),
        expected_revision=revision,
    )
    revisions.append(revision)
    for task_id, owner in (("t-ui", "bob"), ("t-api", "othermember")):
        revision = store.upsert_task(
            build_task(task_id=task_id, owner=owner, goal=f"goal for {task_id}",
                       scopes=[f"src/{task_id}/"], status="queued",
                       context_revision=revision),
            expected_revision=revision,
        )
        revisions.append(revision)
    return store, revisions


@pytest.fixture
def hub(tmp_path):
    return GitStore.create_bare(tmp_path / "hub.git", what="hub")


@pytest.fixture
def wired(hub, tmp_path):
    store, revisions = _seed(hub, tmp_path)
    coordinator = Coordinator(
        store, session_id=SESSION_ID, owner_id="alice", member_credentials=MEMBERS
    )
    return SimpleNamespace(
        hub=hub, store=store, coordinator=coordinator,
        revisions=revisions, head=revisions[-1],
    )


@pytest.fixture
def servers():
    """Every server a test builds is registered here and closed in one finally
    block, so a failing assertion can never leak a listener or a thread."""
    built = []

    def factory(coordinator, **kwargs):
        server = LoopbackServer(coordinator, **kwargs)
        built.append(server)
        return server

    try:
        yield factory
    finally:
        for server in reversed(built):
            try:
                server.close()
            except Exception:  # noqa: BLE001 - cleanup must not mask the failure
                pass


@pytest.fixture
def server(wired, servers):
    return servers(wired.coordinator).start()


@pytest.fixture
def leaks(wired):
    """Substrings that must never appear in a rejection body: credentials, the
    shared context, the temp paths and raw git/traceback vocabulary."""
    return (ALICE, BOB, OTHER, MALLORY, "keep it small", "shared goal",
            str(wired.hub), str(wired.store.store_path), "Traceback", "git")


# -------------------------------------------------------------- http helpers


def _port(server):
    return int(server.base_url.rsplit(":", 1)[1])


def _authority(server):
    return f"127.0.0.1:{_port(server)}"


def _route_body(route, revision):
    return ROUTE_BODY[route].replace(b"__REV__", revision.encode())


_DEFAULT_AUTH = object()


def _head(server, method, path, *, auth=_DEFAULT_AUTH, extra=(), host=None,
          omit_host=False, content_length=0, version="HTTP/1.1"):
    """Build one literal request head. `auth` may be None, a single raw
    Authorization value, or a tuple of values to preserve a duplicate header."""
    if auth is _DEFAULT_AUTH:
        auth = f"Bearer {ALICE}"
    lines = [f"{method} {path} {version}"]
    if not omit_host:
        lines.append(f"Host: {_authority(server) if host is None else host}")
    if isinstance(auth, (list, tuple)):
        lines.extend(f"Authorization: {value}" for value in auth)
    elif auth is not None:
        lines.append(f"Authorization: {auth}")
    for name, value in extra:
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        lines.append(f"{name}: {value}")
    if content_length is not None:
        if isinstance(content_length, bytes):
            content_length = content_length.decode("latin-1")
        lines.append(f"Content-Length: {content_length}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def _exchange(server, payload, *, close_write=False, timeout=15.0,
              read_timeout=None, drain_send=False):
    """Send literal bytes to the bound loopback port and parse the reply.

    Returns Reply(None, None, b"", None) when the listener answered nothing at
    all, which is a legitimate outcome for a dropped or idle connection.
    """
    sock = socket.create_connection(("127.0.0.1", _port(server)), timeout=timeout)
    response = None
    try:
        if drain_send:
            view = memoryview(payload)
            sock.settimeout(0.5)
            while view:
                try:
                    sent = sock.send(view)
                except (TimeoutError, ConnectionError, OSError):
                    break
                if sent <= 0:
                    break
                view = view[sent:]
        else:
            try:
                sock.sendall(payload)
            except (TimeoutError, ConnectionError, OSError):
                pass  # the listener may answer and close before the write lands
        if close_write:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        sock.settimeout(read_timeout or timeout)
        response = http.client.HTTPResponse(sock)
        try:
            response.begin()
        except (TimeoutError, ConnectionError, OSError, http.client.HTTPException):
            return NO_REPLY
        try:
            body = response.read()
        except http.client.IncompleteRead as exc:  # e.g. a HEAD reply, no body
            body = exc.partial
        return Reply(response.status, response.headers, body, response.version)
    finally:
        if response is not None:
            try:
                response.close()
            except OSError:
                pass
        sock.close()


_EXCHANGE_KEYS = ("close_write", "timeout", "read_timeout", "drain_send")


def _raw(server, path=SNAPSHOT, body=EMPTY, *, method="POST", **kwargs):
    """One request built as literal bytes (full control over duplicates, odd
    Content-Length values and malformed framing)."""
    options = {key: kwargs.pop(key) for key in _EXCHANGE_KEYS if key in kwargs}
    kwargs.setdefault("content_length", len(body))
    return _exchange(server, _head(server, method, path, **kwargs) + body, **options)


def _raw_bytes(server, payload, *, timeout=15.0, read_timeout=5.0):
    """Send literal bytes and return everything the listener sends back."""
    sock = socket.create_connection(("127.0.0.1", _port(server)), timeout=timeout)
    try:
        try:
            sock.sendall(payload)
        except (TimeoutError, ConnectionError, OSError):
            pass
        sock.settimeout(read_timeout)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except (TimeoutError, ConnectionError, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        sock.close()


def _call(server, path=SNAPSHOT, body=EMPTY, *, method="POST", auth=_DEFAULT_AUTH,
          content_type=b"application/json", extra=(), host=None, omit_host=False,
          timeout=15.0):
    """One real request over a native HTTPConnection to the literal address.

    A real client announces its media type, so `application/json` is sent unless
    `content_type` is None or `extra` overrides it.
    """
    if auth is _DEFAULT_AUTH:
        auth = f"Bearer {ALICE}"
    conn = http.client.HTTPConnection("127.0.0.1", _port(server), timeout=timeout)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if not omit_host:
            conn.putheader("Host", _authority(server) if host is None else host)
        if isinstance(auth, (list, tuple)):
            for value in auth:
                conn.putheader("Authorization", value)
        elif auth is not None:
            conn.putheader("Authorization", auth)
        if content_type is not None:
            conn.putheader("Content-Type", content_type)
        for name, value in extra:
            conn.putheader(name, value)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body if body else None)
        response = conn.getresponse()
        return Reply(response.status, response.headers, response.read(),
                     response.version)
    finally:
        conn.close()


def _padded_head(server, total, *, path=SNAPSHOT, body=EMPTY, version="HTTP/1.1"):
    """A complete, parseable request head of exactly `total` bytes."""
    lines = [f"POST {path} {version}", f"Host: {_authority(server)}",
             f"Authorization: Bearer {ALICE}", "Content-Type: application/json",
             f"Content-Length: {len(body)}"]
    fixed = "".join(f"{line}\r\n" for line in lines) + "\r\n"
    padding = total - len(fixed) - len("X-Pad: \r\n")
    assert padding >= 1
    return (fixed[:-2] + f"X-Pad: {'p' * padding}\r\n\r\n").encode("ascii")


# ------------------------------------------------------------ reply asserts


def _json(reply):
    return json.loads(reply.body.decode("utf-8"))


def _error(reply, code=None, status=None):
    """Assert the fixed {error: {code, message}} rejection shape."""
    assert reply.status is not None, "the listener answered nothing"
    assert reply.status >= 400, reply.status
    if status is not None:
        assert reply.status == status, (reply.status, reply.body)
    assert reply.headers.get("Content-Type") == "application/json"
    payload = _json(reply)
    assert set(payload) == {"error"}, payload
    assert set(payload["error"]) == {"code", "message"}, payload
    assert isinstance(payload["error"]["message"], str) and payload["error"]["message"]
    if code is not None:
        assert payload["error"]["code"] == code, payload
    return payload["error"]


def _no_leak(reply, leaks):
    """No credential, context, path, git diagnostic or traceback in a reply, and
    no cookie, redirect or CORS surface in its headers."""
    text = reply.body.decode("utf-8", "replace")
    for needle in leaks:
        assert needle not in text, needle
    headers = reply.headers
    if headers is not None:
        for banned in ("Set-Cookie", "Location", "Access-Control-Allow-Origin",
                       "Access-Control-Allow-Headers", "Set-Cookie2"):
            assert headers.get(banned) is None, banned
        assert headers.get("Cache-Control") == "no-store"
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("Connection") == "close"


def _no_secret(reply):
    """A successful metadata reply may carry the shared state, but never a
    credential, a temp path or a traceback."""
    text = reply.body.decode("utf-8", "replace")
    for credential in (ALICE, BOB, OTHER, MALLORY, DAVE):
        assert credential not in text, credential
    assert "Traceback" not in text


def _silent(caplog, capsys):
    assert caplog.records == [], [record.getMessage() for record in caplog.records]
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def _forbid_after_construction(monkeypatch):
    """Once the listener exists, make any body read, body parse or git call
    fail loudly, so "authentication happens first" is provable, not assumed."""
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
    monkeypatch.setattr(store_module, "_run_git", forbidden_git)
    return seen


# --------------------------------------------- construction, binding, lifecycle


def test_construction_binds_only_literal_ipv4_and_exposes_the_authority(
    wired, servers
):
    first = servers(wired.coordinator)
    second = servers(wired.coordinator)
    for server in (first, second):
        assert server._server.server_address[0] == "127.0.0.1"
        assert server._server.address_family == socket.AF_INET
        assert 1 <= _port(server) <= 65535
        assert server.base_url == f"http://127.0.0.1:{_port(server)}"
    assert _port(first) != _port(second)  # port 0 picked two fresh ephemeral ports


def test_there_is_no_host_argument(wired):
    for kwargs in ({"host": "127.0.0.1"}, {"address": "127.0.0.1"},
                   {"hostname": "localhost"}, {"port": 0, "host": "0.0.0.0"}):
        with pytest.raises(TypeError):
            LoopbackServer(wired.coordinator, **kwargs)


@pytest.mark.parametrize("port", [True, False, -1, 65536, "0", 1.0, None, [0]])
def test_invalid_port_is_rejected_before_binding(wired, port):
    with pytest.raises(ValidationError):
        LoopbackServer(wired.coordinator, port=port)


@pytest.mark.parametrize("core", [None, "coordinator", object(), 42])
def test_only_a_configured_coordinator_is_accepted(core):
    with pytest.raises(ValidationError):
        LoopbackServer(core)


def test_a_wrong_core_fails_before_any_bind():
    """A busy port plus a wrong core must fail on the core, never on the bind."""
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        busy = holder.getsockname()[1]
        with pytest.raises(ValidationError) as excinfo:
            LoopbackServer("not a coordinator", port=busy)
        assert "coordinator" in str(excinfo.value)


def test_busy_port_raises_validation_error_without_system_exit_or_noise(
    wired, caplog, capsys
):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        busy = holder.getsockname()[1]
        with pytest.raises(ValidationError) as excinfo:
            LoopbackServer(wired.coordinator, port=busy)
    assert "bound" in str(excinfo.value).lower()
    assert not isinstance(excinfo.value, SystemExit)
    with caplog.at_level(logging.DEBUG):
        _silent(caplog, capsys)
    with socket.socket() as probe:  # the refused port was left free again
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", busy))
        probe.close()


def test_close_before_start_start_idempotence_and_the_restart_guard(wired, servers):
    unstarted = servers(wired.coordinator)
    assert unstarted.close() is None  # close before start is a no-op
    with pytest.raises(ValidationError):
        unstarted.start()  # a server closed before start is closed for good

    server = servers(wired.coordinator)
    assert server.start() is server
    assert server.start() is server  # start is idempotent
    assert _call(server)[0] == 200
    assert server.close() is None
    assert server.close() is None  # close is idempotent
    with pytest.raises(ValidationError):
        server.start()
    with pytest.raises(ValidationError):
        server.start()
    with socket.socket() as probe:
        probe.settimeout(1.0)
        with pytest.raises((ConnectionError, TimeoutError, OSError)):
            probe.connect(("127.0.0.1", _port(server)))  # the listener is closed


def test_close_after_worker_start_failure_does_not_wait_for_an_unstarted_worker(wired, monkeypatch):
    server = LoopbackServer(wired.coordinator)

    def fail_start(_thread):
        raise RuntimeError("test worker creation failure")

    def forbid_shutdown():
        raise AssertionError("an unstarted worker cannot be shut down")

    monkeypatch.setattr(transport.Thread, "start", fail_start)
    monkeypatch.setattr(server._server, "shutdown", forbid_shutdown)
    try:
        with pytest.raises(RuntimeError):
            server.start()
        assert server._thread is None
        server.close()
        with pytest.raises(ValidationError):
            server.start()
    finally:
        server._server.server_close()


def test_close_failure_is_reported_and_can_be_retried(wired, servers, monkeypatch):
    server = servers(wired.coordinator)
    real_close = server._server.server_close
    attempts = []

    def fail_once():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("private close diagnostic")
        real_close()

    monkeypatch.setattr(server._server, "server_close", fail_once)
    with pytest.raises(ValidationError) as failure:
        server.close()
    assert "private close diagnostic" not in str(failure.value)
    assert server._closed is False
    server.close()
    assert len(attempts) == 2
    assert server._closed is True


def test_context_manager_starts_and_closes(wired):
    server = LoopbackServer(wired.coordinator)
    with server as entered:
        assert entered is server
        assert _call(entered)[0] == 200
    with socket.socket() as probe:
        probe.settimeout(1.0)
        with pytest.raises((ConnectionError, TimeoutError, OSError)):
            probe.connect(("127.0.0.1", _port(server)))


def test_an_explicit_port_is_honoured(wired, servers):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        wanted = holder.getsockname()[1]
    server = servers(wired.coordinator, port=wanted)
    assert _port(server) == wanted
    server.start()
    assert _call(server)[0] == 200


# ----------------------------------------------------------- check_access seam


def test_check_access_returns_nothing_and_never_touches_git(wired, monkeypatch):
    seen = []
    monkeypatch.setattr(store_module, "_run_git",
                        lambda args, **kwargs: seen.append(list(args)))
    for credential in (ALICE, BOB, OTHER):
        assert wired.coordinator.check_access(credential) is None
    for bad in (None, 42, 3.5, True, b"A" * 40, ["A" * 40], {"token": ALICE},
                "", "short", "A" * 31, "A" * 257, ALICE + " ", "é" * 40,
                ALICE + "\n", ALICE.lower(), MALLORY):
        with pytest.raises(AccessDeniedError):
            wired.coordinator.check_access(bad)
    assert seen == []


# ------------------------------------------------------------ the four v1 routes


def test_snapshot_returns_revision_and_state_to_every_member(server):
    for credential in (ALICE, BOB, OTHER):
        reply = _call(server, auth=f"Bearer {credential}")
        assert reply.status == 200, reply.body
        payload = _json(reply)
        assert set(payload) == {"revision", "state"}
        state = payload["state"]
        assert set(state) == {"schema", "session_id", "target_version",
                              "base_commit", "context", "tasks"}
        assert state["session_id"] == SESSION_ID
        assert state["base_commit"] == SHA0
        assert set(state["tasks"]) == {"t-ui", "t-api"}
        assert set(state["context"]) == {"goal", "decisions", "interfaces"}


def test_context_write_is_owner_only_and_moves_the_head(wired, server, leaks):
    body = _route_body(CONTEXT, wired.head).replace(b'"goal": "g"',
                                                    b'"goal": "second goal"')
    for credential in (BOB, OTHER):
        reply = _call(server, CONTEXT, body, auth=f"Bearer {credential}")
        _error(reply, code="access_denied", status=403)
        _no_leak(reply, leaks)
    assert wired.store.remote_head() == wired.head  # a denied write moves nothing

    reply = _call(server, CONTEXT, body)
    assert reply.status == 200, reply.body
    assert set(_json(reply)) == {"revision"}
    revision = _json(reply)["revision"]
    assert wired.store.remote_head() == revision
    state = _json(_call(server))["state"]
    assert state["context"]["goal"] == "second goal"
    assert set(state["tasks"]) == {"t-ui", "t-api"}  # the context write touched no task
    assert _json(_call(server))["revision"] == revision


def test_task_status_separates_own_task_from_other_task_and_keeps_provenance(
    wired, server, leaks
):
    before = _json(_call(server))["state"]["tasks"]["t-ui"]
    own = _call(server, TASK_STATUS, _route_body(TASK_STATUS, wired.head),
                auth=f"Bearer {BOB}")
    assert own.status == 200, own.body
    assert set(_json(own)) == {"revision"}

    foreign = _call(server, TASK_STATUS,
                    _route_body(TASK_STATUS, wired.head).replace(b"t-ui", b"t-api"),
                    auth=f"Bearer {BOB}")
    _error(foreign, code="access_denied", status=403)
    _no_leak(foreign, leaks)

    head = _json(own)["revision"]
    owner_write = _call(server, TASK_STATUS,
                        _route_body(TASK_STATUS, head).replace(b"t-ui", b"t-api")
                        .replace(b"running", b"waiting"))
    assert owner_write.status == 200, owner_write.body  # the owner drives any task

    after = _json(_call(server))["state"]["tasks"]["t-ui"]
    assert after["status"] == "running"
    for field in ("owner", "goal", "scopes", "context_revision"):
        assert after[field] == before[field], field
    assert set(after) == set(before) == {"owner", "goal", "scopes", "status",
                                        "context_revision"}
    assert after["context_revision"] == wired.revisions[1]

    invalid = _call(server, TASK_STATUS,
                    _route_body(TASK_STATUS, head).replace(b'"running"', b'"nope"'))
    _error(invalid, code="invalid_request", status=400)
    missing = _call(server, TASK_STATUS,
                    _route_body(TASK_STATUS, head).replace(b"t-ui", b"t-none"))
    _error(missing, code="invalid_request", status=400)


def test_stale_write_is_conflict_and_never_moves_the_hub(wired, server, leaks):
    stale = wired.revisions[0]
    reply = _call(server, CONTEXT, _route_body(CONTEXT, stale))
    _error(reply, code="stale_revision", status=409)
    _no_leak(reply, leaks)
    assert wired.store.remote_head() == wired.head

    write = _call(server, TASK_STATUS, _route_body(TASK_STATUS, wired.head))
    assert write.status == 200, write.body
    winner = _json(write)["revision"]
    assert wired.store.remote_head() == winner
    again = _call(server, CONTEXT, _route_body(CONTEXT, stale))
    _error(again, code="stale_revision", status=409)
    assert wired.store.remote_head() == winner  # no hub move, no event published

    page = _json(_call(server, REPLAY, _route_body(REPLAY, stale)))
    assert [event["revision"] for event in page["events"]] == (
        wired.revisions[1:] + [winner]
    )


def test_replay_pages_in_order_and_carries_no_domain_payload(wired, server):
    r0, r1, r2, r3 = wired.revisions
    first = _json(_call(server, REPLAY, _route_body(REPLAY, r0)))
    assert set(first) == {"head_revision", "next_revision", "events", "has_more"}
    assert first["head_revision"] == r3
    assert first["events"][0]["revision"] == r1
    assert first["events"][0]["context_changed"] is True
    assert first["events"][1] == {"previous_revision": r1, "revision": r2,
                                  "context_changed": False, "changed_task_ids": ["t-ui"]}
    assert first["events"][2]["changed_task_ids"] == ["t-api"]
    for event in first["events"]:
        assert set(event) == {"previous_revision", "revision", "context_changed",
                              "changed_task_ids"}
        for banned in ("context", "tasks", "goal", "payload", "code", "status"):
            assert banned not in event
    assert [event["previous_revision"] for event in first["events"]] == [r0, r1, r2]
    assert first["next_revision"] == r3 and first["has_more"] is False

    one = _json(_call(server, REPLAY,
                      _route_body(REPLAY, r0).replace(b"}", b', "limit": 1}')))
    assert len(one["events"]) == 1 and one["has_more"] is True
    assert one["next_revision"] == r1
    two = _json(_call(server, REPLAY,
                      _route_body(REPLAY, one["next_revision"]).replace(
                          b"}", b', "limit": 1}')))
    assert [event["revision"] for event in two["events"]] == [r2]
    assert two["has_more"] is True
    rest = _json(_call(server, REPLAY, _route_body(REPLAY, two["next_revision"])))
    assert [event["revision"] for event in rest["events"]] == [r3]
    assert rest["has_more"] is False

    empty = _json(_call(server, REPLAY, _route_body(REPLAY, r3)))
    assert empty["events"] == []
    assert empty["next_revision"] == empty["head_revision"] == r3
    assert empty["has_more"] is False


def test_unknown_replay_cursor_is_gone_and_a_snapshot_recovers(wired, server, leaks):
    for cursor in ("b" * 40, "0" * 40, "f" * 40, SHA0):
        reply = _call(server, REPLAY, _route_body(REPLAY, cursor))
        _error(reply, code="replay_unavailable", status=410)
        _no_leak(reply, leaks)
    fresh = _call(server)
    assert fresh.status == 200
    revision = _json(fresh)["revision"]
    recovered = _json(_call(server, REPLAY, _route_body(REPLAY, revision)))
    assert recovered["events"] == [] and recovered["next_revision"] == revision


def test_expired_replay_cursor_requires_a_new_snapshot_over_http(wired, server, monkeypatch):
    monkeypatch.setattr(store_module, "REPLAY_WINDOW", 2)
    expired = _call(server, REPLAY, _route_body(REPLAY, wired.revisions[0]))
    failure = _error(expired, code="replay_unavailable", status=410)
    assert "fetch a new snapshot" in failure["message"]
    fresh = _json(_call(server))["revision"]
    assert fresh == wired.head
    page = _json(_call(server, REPLAY, _route_body(REPLAY, fresh)))
    assert page["events"] == [] and page["next_revision"] == fresh


@pytest.mark.parametrize("limit", [b"true", b"false", b"0", b"33", b'"2"', b"null",
                                   b"2.0", b"[]", b"-1"])
def test_replay_limit_must_be_a_real_integer(server, wired, limit, leaks):
    body = _route_body(REPLAY, wired.head)[:-1] + b', "limit": ' + limit + b"}"
    reply = _call(server, REPLAY, body)
    _error(reply, code="invalid_request", status=400)
    _no_leak(reply, leaks)
    assert _call(server)[0] == 200  # the session is untouched


def test_replay_cursor_survives_a_listener_restart(wired, servers):
    first = servers(wired.coordinator).start()
    page = _json(_call(first, REPLAY, _route_body(REPLAY, wired.revisions[0])))
    first.close()
    second = servers(wired.coordinator).start()
    assert _json(_call(second, REPLAY, _route_body(REPLAY, wired.revisions[0]))) == page
    assert _json(_call(second))["revision"] == wired.head


# ---------------------------------------------- stop-and-replace credentials


def test_stop_and_replace_rotates_the_owner_and_revokes_the_old_token(
    wired, servers, leaks
):
    first = servers(wired.coordinator).start()
    cursor = wired.revisions[-1]
    write = _call(first, TASK_STATUS, _route_body(TASK_STATUS, cursor))
    assert write.status == 200, write.body
    head = _json(write)["revision"]
    first.close()  # completion of close() is the credential replacement point
    del wired.coordinator  # the old core is discarded, not reused

    replaced = Coordinator(wired.store, session_id=SESSION_ID, owner_id="bob",
                           member_credentials=REPLACED)
    second = servers(replaced).start()

    for route in ROUTES:
        reply = _call(second, route, _route_body(route, cursor), auth=f"Bearer {ALICE}")
        _error(reply, code="access_denied", status=401)
        assert reply.headers.get("WWW-Authenticate") == "Bearer"
        _no_leak(reply, leaks)
    for credential in (BOB, OTHER, DAVE):
        assert _call(second, auth=f"Bearer {credential}")[0] == 200
    assert _json(_call(second, auth=f"Bearer {BOB}"))["revision"] == head

    promoted = _call(second, CONTEXT, _route_body(CONTEXT, head), auth=f"Bearer {BOB}")
    assert promoted.status == 200, promoted.body  # the owner rotated
    denied = _call(second, CONTEXT, _route_body(CONTEXT, _json(promoted)["revision"]),
                   auth=f"Bearer {DAVE}")
    _error(denied, code="access_denied", status=403)

    replayed = _json(_call(second, REPLAY, _route_body(REPLAY, cursor),
                           auth=f"Bearer {BOB}"))
    assert [event["revision"] for event in replayed["events"]] == [
        head, _json(promoted)["revision"]
    ]
    assert replayed["events"][0]["previous_revision"] == cursor
    assert replayed["has_more"] is False


def test_stop_and_replace_rotates_a_credential_without_changing_owner_identity(wired, servers):
    old = servers(wired.coordinator).start()
    cursor = _json(_call(old))["revision"]
    old.close()
    replacement = Coordinator(
        wired.store, session_id=SESSION_ID, owner_id="alice",
        member_credentials={"alice": DAVE, "bob": BOB},
    )
    new = servers(replacement).start()
    for credential in (ALICE, OTHER):
        for route in ROUTES:
            _error(_call(new, route, _route_body(route, cursor), auth=f"Bearer {credential}"),
                   code="access_denied", status=401)
    for credential in (DAVE, BOB):
        assert _call(new, auth=f"Bearer {credential}").status == 200
    write = _call(new, CONTEXT, _route_body(CONTEXT, cursor), auth=f"Bearer {DAVE}")
    assert write.status == 200
    revision = _json(write)["revision"]
    _error(_call(new, CONTEXT, _route_body(CONTEXT, revision), auth=f"Bearer {BOB}"),
           code="access_denied", status=403)
    replayed = _json(_call(new, REPLAY, _route_body(REPLAY, cursor), auth=f"Bearer {BOB}"))
    assert [event["revision"] for event in replayed["events"]] == [revision]


def test_there_is_no_credential_or_code_endpoint(server, leaks):
    for path in UNSUPPORTED_PATHS:
        anonymous = _call(server, path, EMPTY, auth=None)
        _error(anonymous, code="access_denied", status=401)
        _no_leak(anonymous, leaks)
        authenticated = _call(server, path, EMPTY)
        _error(authenticated, code="invalid_request", status=404)
        _no_leak(authenticated, leaks)
        assert authenticated.headers.get("WWW-Authenticate") is None


# -------------------------------------------- authentication strictly first


AUTH_CASES = [
    ("missing", None),
    ("empty-scheme", "Bearer"),
    ("no-token", "Bearer "),
    ("no-separator", f"Bearer{ALICE}"),
    ("basic-scheme", f"Basic {BOB}"),
    ("unknown-token", f"Bearer {MALLORY}"),
    ("wrong-case-token", f"Bearer {ALICE.lower()}"),
    ("blank-credential", "Bearer    "),
]
AUTH_IDS = [name for name, _ in AUTH_CASES]
AUTH_BY_ID = dict(AUTH_CASES)


@pytest.mark.parametrize("authorization", [f"bearer {ALICE}", f"BEARER {ALICE}",
                                           f"BeArEr {ALICE}", f"Bearer   {ALICE}"])
def test_bearer_scheme_uses_http_case_and_space_rules(server, authorization):
    reply = _call(server, auth=authorization)
    assert reply.status == 200
    assert _json(reply)["state"]["session_id"] == SESSION_ID


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("auth_id", AUTH_IDS)
def test_bad_bearer_is_401_before_any_body_read_parse_or_git(
    wired, servers, route, auth_id, monkeypatch, caplog, capsys, leaks
):
    auth = AUTH_BY_ID[auth_id]
    server = servers(wired.coordinator).start()
    seen = _forbid_after_construction(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        for method in ("POST", "GET"):
            # Send one complete wire frame: HTTPConnection.endheaders(body)
            # writes the request head and body separately, so the listener's
            # intentional auth-first close can race the second client write.
            reply = _raw(server, route, _route_body(route, wired.head),
                         method=method, auth=auth,
                         extra=(("Content-Type", "application/json"),))
            _error(reply, code="access_denied", status=401)
            assert reply.headers.get("WWW-Authenticate") == "Bearer"
            _no_leak(reply, leaks)
    assert seen == []
    _silent(caplog, capsys)


@pytest.mark.parametrize("route", ROUTES)
def test_duplicate_authorization_is_401_before_any_body_read_parse_or_git(
    wired, servers, route, monkeypatch, caplog, capsys, leaks
):
    server = servers(wired.coordinator).start()
    seen = _forbid_after_construction(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        for headers in ((f"Bearer {ALICE}", f"Bearer {BOB}"),
                        (f"Bearer {ALICE}", f"Bearer {ALICE}"),
                        (f"Bearer {ALICE}", f"Bearer {MALLORY}")):
            reply = _raw(server, route, _route_body(route, wired.head), auth=headers)
            _error(reply, code="access_denied", status=401)
            assert reply.headers.get("WWW-Authenticate") == "Bearer"
            _no_leak(reply, leaks)
    assert seen == []
    _silent(caplog, capsys)


@pytest.mark.parametrize("route", ROUTES)
def test_unauthorized_request_is_answered_without_reading_the_declared_body(
    wired, servers, route, monkeypatch, leaks
):
    """Valid framing, a declared 64-byte body, nothing sent and the write side
    kept open: the 401 must arrive instead of waiting for the body."""
    server = servers(wired.coordinator).start()
    seen = _forbid_after_construction(monkeypatch)
    started = time.monotonic()
    reply = _raw(server, route, b"", content_length=64, auth=f"Bearer {MALLORY}",
                 timeout=2.0, read_timeout=2.0)
    assert time.monotonic() - started < 2.0
    _error(reply, code="access_denied", status=401)
    _no_leak(reply, leaks)
    assert seen == []


def test_credentials_are_never_accepted_outside_the_bearer_header(server, wired, leaks):
    for path in (f"{SNAPSHOT}?token={ALICE}", f"{SNAPSHOT}?limit=1",
                 f"{SNAPSHOT}?", f"{CONTEXT}?x=1"):
        _error(_call(server, path, EMPTY, auth=None),
               code="invalid_request", status=400)
    cookie = _call(server, SNAPSHOT, EMPTY, auth=None,
                   extra=(("Cookie", f"token={ALICE}; auth={BOB}"),))
    _error(cookie, code="access_denied", status=401)
    _no_leak(cookie, leaks)
    bodies = (
        (SNAPSHOT, f'{{"credential": "{ALICE}"}}'.encode()),
        (SNAPSHOT, f'{{"token": "{BOB}"}}'.encode()),
        (CONTEXT, _route_body(CONTEXT, wired.head)[:-1]
         + f', "credential": "{ALICE}"}}'.encode()),
    )
    for route, body in bodies:
        anonymous = _call(server, route, body, auth=None)
        _error(anonymous, code="access_denied", status=401)
        _no_leak(anonymous, leaks)
        rejected = _call(server, route, body)
        _error(rejected, code="invalid_request", status=400)
        _no_leak(rejected, leaks)
    _error(_call(server, f"{SNAPSHOT}/{ALICE}", EMPTY, auth=None),
           code="access_denied", status=401)
    assert _call(server)[0] == 200  # a real bearer header still works


# -------------------------------------------------------- routing and methods


def test_unknown_route_and_unsupported_method_authenticate_first(server, leaks):
    for method in ("GET", "DELETE", "PUT", "PATCH", "OPTIONS", "TRACE", "BREW"):
        anonymous = _raw(server, SNAPSHOT, b"", method=method, content_length=0,
                        auth=None)
        _error(anonymous, code="access_denied", status=401)
        assert anonymous.headers.get("WWW-Authenticate") == "Bearer"
        _no_leak(anonymous, leaks)
        for path in ROUTES:
            reply = _raw(server, path, b"", method=method, content_length=0)
            _error(reply, code="invalid_request", status=405)
            assert reply.headers.get("Allow") == "POST"
            assert reply.headers.get("WWW-Authenticate") is None
            _no_leak(reply, leaks)
    head = _raw(server, SNAPSHOT, b"", method="HEAD", content_length=0)
    assert head.status == 405
    assert head.headers.get("Allow") == "POST"
    assert head.headers.get("Content-Type") == "application/json"
    assert head.body == b""

    for path in ("/v1/snapshots", "/v1/snapshot/", "/V1/SNAPSHOT", "/v1"):
        _error(_raw(server, path, b"", content_length=0, auth=None),
               code="access_denied", status=401)
        _error(_raw(server, path, b"", content_length=0),
               code="invalid_request", status=404)

    for headers, status in (((), 405),
                            ((("Access-Control-Request-Method", "POST"),), 405),
                            ((("Origin", "http://127.0.0.1"),), 400)):
        preflight = _raw(server, SNAPSHOT, b"", method="OPTIONS", content_length=0,
                         extra=headers)
        _error(preflight, code="invalid_request", status=status)
        for banned in ("Access-Control-Allow-Origin", "Access-Control-Allow-Methods",
                       "Set-Cookie", "Location", "Vary"):
            assert preflight.headers.get(banned) is None


# ------------------------------------------- authority, origin and query guards


@pytest.mark.parametrize("host", ["localhost:{port}", "127.0.0.1", "127.0.0.1:0",
                                  "127.0.0.1:{port}x", "[::1]:{port}", "0.0.0.0:{port}",
                                  "127.0.0.1:{other}", ""])
def test_a_wrong_or_missing_host_is_rejected_before_authentication(server, host, leaks):
    value = host.format(port=_port(server), other=_port(server) + 1)
    for candidate, omit in ((value, False), (None, True)):
        for auth in (None, f"Bearer {ALICE}"):
            reply = _raw(server, SNAPSHOT, EMPTY, host=candidate,
                         omit_host=omit, auth=auth)
            _error(reply, code="invalid_request", status=400)
            _no_leak(reply, leaks)
    assert _call(server)[0] == 200  # only the bound authority is accepted


def test_duplicate_host_header_is_rejected(server, leaks):
    payload = _head(server, "POST", SNAPSHOT, omit_host=True) + EMPTY
    payload = payload.replace(
        b"\r\n\r\n",
        f"\r\nHost: {_authority(server)}\r\nHost: {_authority(server)}\r\n\r\n".encode(),
        1,
    )
    reply = _exchange(server, payload)
    _error(reply, code="invalid_request", status=400)
    _no_leak(reply, leaks)


@pytest.mark.parametrize("origin", ["http://127.0.0.1:{port}", "http://localhost",
                                    "https://127.0.0.1", "null", "file://",
                                    "http://127.0.0.1:{port}, http://localhost", ""])
def test_any_origin_header_is_rejected(server, origin, leaks):
    value = origin.format(port=_port(server))
    for auth in (None, f"Bearer {ALICE}"):
        reply = _raw(server, SNAPSHOT, EMPTY, extra=(("Origin", value),),
                     auth=auth)
        _error(reply, code="invalid_request", status=400)
        _no_leak(reply, leaks)
    duplicate = _raw(server, SNAPSHOT, EMPTY,
                     extra=(("Origin", "null"), ("Origin", "null")))
    _error(duplicate, code="invalid_request", status=400)


@pytest.mark.parametrize("path", [f"{SNAPSHOT}?a=1", f"{SNAPSHOT}?", f"{CONTEXT}?x",
                                  f"{REPLAY}?after_revision=a", f"{TASK_STATUS}?a=1&b=2",
                                  "/v1/nope?a=1"])
def test_any_query_string_is_rejected_before_authentication(server, path, leaks):
    for auth in (None, f"Bearer {ALICE}"):
        reply = _raw(server, path, EMPTY, auth=auth)
        _error(reply, code="invalid_request", status=400)
        _no_leak(reply, leaks)


def test_a_rejection_never_waits_for_the_declared_body(server, monkeypatch, leaks):
    """Framing/authority guards may reject without authentication, but they must
    not read a body either."""
    seen = _forbid_after_construction(monkeypatch)
    reply = _raw(server, "/v1/nope", b"", content_length=64, timeout=2.0,
                 read_timeout=2.0)
    _error(reply, code="invalid_request", status=404)
    _no_leak(reply, leaks)
    assert seen == []


# ------------------------------------------------- framing and transfer rules


@pytest.mark.parametrize("value", [b"chunked", b"identity", b"gzip, chunked"])
def test_transfer_encoding_is_rejected(server, value, leaks):
    for auth in (None, f"Bearer {ALICE}"):
        reply = _raw(server, SNAPSHOT, EMPTY,
                     extra=(("Transfer-Encoding", value),), auth=auth)
        _error(reply, code="invalid_request", status=400)
        _no_leak(reply, leaks)
    chunked = _raw(server, SNAPSHOT, b"2\r\n{}\r\n0\r\n\r\n", content_length=None,
                   extra=(("Transfer-Encoding", b"chunked"),))
    _error(chunked, code="invalid_request", status=400)


def test_expect_100_continue_is_rejected_and_never_answered_with_100(server, leaks):
    payload = _head(server, "POST", SNAPSHOT, extra=(("Expect", "100-continue"),)) + EMPTY
    raw = _raw_bytes(server, payload, read_timeout=3.0)
    assert raw, "the listener answered nothing at all"
    assert raw.startswith(b"HTTP/1.0 400"), raw[:64]
    assert raw.count(b"HTTP/1.") == 1  # never an interim 100 Continue
    assert b"100-continue" not in raw
    _no_leak(_exchange(server, payload), leaks)


@pytest.mark.parametrize("path", ROUTES)
def test_every_request_needs_one_decimal_content_length(server, path, leaks):
    missing = _raw(server, path, b"", content_length=None, extra=JSON_CT)
    _error(missing, code="invalid_request", status=400)
    _no_leak(missing, leaks)
    for method in ("GET", "POST"):
        for value in (b"-1", b"+2", b"2.0", b"0x2", b"two", b"2 2", b" 2 x", b"2,2", b""):
            reply = _raw(server, path, b"", method=method, content_length=None,
                         extra=(("Content-Length", value),))
            assert reply.status == 400, (value, reply.status, reply.body)
            _error(reply, code="invalid_request", status=400)
        huge = _raw(server, path, b"", method=method, content_length=None,
                    extra=(("Content-Length", b"9" * 120),))
        _error(huge, code="invalid_request", status=413)  # bounded, never a 500
        duplicate = _raw(server, path, b"", method=method, content_length=None,
                         extra=(("Content-Length", b"0"), ("Content-Length", b"0")))
        _error(duplicate, code="invalid_request", status=400)
        # bounded zero padding is accepted, and the guards after it still apply
        zeroed = _raw(server, path, b"", method=method, content_length=b"0000000")
        _error(zeroed, code="invalid_request", status=405 if method == "GET" else 415)
    # 0000002 really is two bytes, and the announced body is the one served
    padded = _raw(server, SNAPSHOT, EMPTY, content_length=b"0000002",
                  extra=JSON_CT)
    assert padded.status == 200, padded.body
    short = _raw(server, SNAPSHOT, EMPTY, content_length=b"0000003",
                 extra=JSON_CT, close_write=True)
    _error(short, code="invalid_request", status=400)


# ------------------------------------------------------------ the head budget


def test_the_request_head_budget_is_exactly_64_kib(server, leaks):
    assert len(_padded_head(server, MAX_HEAD)) == MAX_HEAD
    at_limit = _exchange(server, _padded_head(server, MAX_HEAD) + EMPTY)
    assert at_limit.status == 200, (at_limit.status, at_limit.body[:200])
    over = _exchange(server, _padded_head(server, MAX_HEAD + 1) + EMPTY)
    _error(over, code="invalid_request", status=413)
    _no_leak(over, leaks)
    assert _call(server)[0] == 200  # the listener survived the over-long head


def test_many_headers_share_the_same_head_budget(server, leaks):
    """~80 KiB of header lines is refused like any other over-long head, and the
    listener survives it. The reply itself is only observable when the whole
    upload was consumed before the reset: a half-sent head may be cut off."""
    payload = _head(server, "POST", SNAPSHOT,
                    extra=tuple(("X-Pad", "p" * 400) for _ in range(200))) + EMPTY
    assert len(payload) > MAX_HEAD
    reply = _exchange(server, payload, drain_send=True, read_timeout=5.0)
    if reply.status is not None:
        _error(reply, code="invalid_request", status=413)
        _no_leak(reply, leaks)
    assert _call(server)[0] == 200


class _SpyStream:
    """Minimal reader that records the exact size of every read it serves."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)
        self.sizes: list[int] = []

    def readline(self, *args):
        chunk = self._buffer.readline(*args)
        self.sizes.append(len(chunk))
        return chunk

    def read(self, *args):
        chunk = self._buffer.read(*args)
        self.sizes.append(len(chunk))
        return chunk


def test_the_head_budget_is_enforced_while_reading_not_after_buffering():
    budget = transport._MAX_HEAD_BYTES
    assert budget == MAX_HEAD

    one_line = _SpyStream(b"x" * (budget * 3))
    reader = transport._HeadReader(one_line)
    with pytest.raises(http.client.LineTooLong):
        reader.readline(65537)  # exactly what the stdlib handler asks for
    assert sum(one_line.sizes) <= budget + 1  # never buffered the whole stream
    assert max(one_line.sizes) <= budget + 1

    many = _SpyStream(b"abc\r\n" * (budget // 2))
    counted = transport._HeadReader(many)
    seen = 0
    with pytest.raises(http.client.LineTooLong):
        while True:
            seen += len(counted.readline())
    assert seen <= budget
    assert sum(many.sizes) <= budget + 1

    line = b"x" * 30 + b"\r\n"  # 32 bytes, so 2048 lines are exactly the budget
    at_budget = _SpyStream(line * 2048)
    exact = transport._HeadReader(at_budget)
    total = 0
    while True:
        chunk = exact.readline()
        if not chunk:
            break
        total += len(chunk)
    assert total == MAX_HEAD and exact.count == MAX_HEAD
    assert max(at_budget.sizes) <= budget + 1

    body = _SpyStream(b"y" * 32)
    body_reader = transport._HeadReader(body)
    assert body_reader.read(16) == b"y" * 16  # body reads are never counted
    assert body_reader.count == 0


# ------------------------------------------------- media type and JSON body


@pytest.mark.parametrize("content_type", [
    b"text/plain", b"application/json; charset=utf-8", b"application/json ; x=1",
    b"application/jsonx", b"application/vnd.api+json", b"text/json",
    b"multipart/form-data; boundary=x", b"", b"*/*",
])
def test_content_type_must_be_exactly_application_json(server, content_type, leaks):
    reply = _raw(server, SNAPSHOT, EMPTY, extra=(("Content-Type", content_type),))
    _error(reply, code="invalid_request", status=415)
    _no_leak(reply, leaks)
    duplicate = _raw(server, SNAPSHOT, EMPTY,
                     extra=(("Content-Type", b"application/json"),
                            ("Content-Type", b"application/json")))
    _error(duplicate, code="invalid_request", status=415)
    _error(_raw(server, SNAPSHOT, EMPTY), code="invalid_request", status=415)
    exact = _raw(server, SNAPSHOT, EMPTY, extra=JSON_CT)
    assert exact.status == 200, exact.body


@pytest.mark.parametrize("body", [
    b'{"a": 1, "a": 2}',             # duplicate keys
    b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}',
    b'{"a": "\\ud800"}',             # unpaired surrogate
    b'{"a": 1}', b'{"schema": 1}',   # unknown field on /v1/snapshot
    b'{"a": 1',                      # truncated
    b"[1, 2]", b'"text"', b"1", b"null", b"true", b"", b"   ",
    b'{"a": 1}\n{"b": 2}',           # trailing garbage
    b"\xff\xfe{}",                   # not UTF-8
    b"\x00{}",
])
def test_snapshot_bodies_must_be_one_strict_empty_json_object(server, body, leaks):
    reply = _call(server, SNAPSHOT, body)
    _error(reply, code="invalid_request", status=400)
    _no_leak(reply, leaks)
    assert _call(server)[0] == 200


@pytest.mark.parametrize("route,body", [
    (CONTEXT, b'{"context": {"goal": "g", "decisions": [], "interfaces": {}}}'),
    (CONTEXT, b'{"expected_revision": "a"}'),
    (CONTEXT, b'{"expected_revision": "a", "context": {}}'),
    (CONTEXT, b'{"expected_revision": "a", "context": {"goal": "g"}, "extra": 1}'),
    (TASK_STATUS, b'{"expected_revision": "a", "task_id": "t-ui"}'),
    (TASK_STATUS, b'{"expected_revision": "a", "status": "running"}'),
    (TASK_STATUS, b'{"expected_revision": "a", "task_id": "t-ui", "status": "running",'
                  b' "assignee": "bob"}'),
    (REPLAY, b"{}"),
    (REPLAY, b'{"limit": 1}'),
    (REPLAY, b'{"after_revision": "a", "extra": 1}'),
    (REPLAY, b'{"after_revision": "a", "limit": 1, "cursor": "b"}'),
])
def test_write_bodies_must_carry_exactly_the_contract_fields(server, route, body, leaks):
    reply = _call(server, route, body)
    _error(reply, code="invalid_request", status=400)
    _no_leak(reply, leaks)


@pytest.mark.parametrize("revision", [b"deadbeef", b"A" * 40, b"", b"0" * 39, b"0" * 41])
def test_a_malformed_expected_revision_is_invalid_data_not_a_conflict(
    server, wired, revision, leaks
):
    for route in (CONTEXT, TASK_STATUS):
        body = _route_body(route, wired.head).replace(wired.head.encode(), revision)
        reply = _call(server, route, body)
        _error(reply, code="invalid_request", status=400)
        _no_leak(reply, leaks)


def test_the_json_envelope_budget_is_exactly_64_kib(server, monkeypatch, leaks):
    exact = b" " * (MAX_ENVELOPE - 2) + b"{}"
    assert len(exact) == MAX_ENVELOPE
    allowed = _call(server, SNAPSHOT, exact)
    assert allowed.status == 200, allowed.body
    assert set(_json(allowed)) == {"revision", "state"}

    reads = _forbid_after_construction(monkeypatch)
    over = _raw(server, SNAPSHOT, b"", content_length=str(MAX_ENVELOPE + 1),
                extra=JSON_CT, timeout=2.0, read_timeout=2.0)
    _error(over, code="invalid_request", status=413)
    _no_leak(over, leaks)
    assert reads == []  # the declared length alone rejected it, no body was read


# ----------------------------------------------- response shape and lifecycle


def test_every_response_is_http10_unstoreable_nosniff_and_closes(server, leaks):
    replies = [
        _call(server),                                           # 200
        _call(server, "/v1/nope", EMPTY),                # 404
        _raw(server, SNAPSHOT, b"", method="GET", content_length=0),  # 405
        _call(server, SNAPSHOT, EMPTY, auth=None),       # 401
        _call(server, CONTEXT, b"[]"),                   # 400
        _call(server, SNAPSHOT, EMPTY, content_type=b"text/plain"),
        _raw(server, SNAPSHOT, b"", content_length=str(MAX_ENVELOPE + 1)),
    ]
    assert [reply.status for reply in replies] == [200, 404, 405, 401, 400, 415, 413]
    for reply in replies:
        assert reply.version == 10, reply.version  # always answered HTTP/1.0
        assert reply.headers.get("Connection") == "close"
        assert reply.headers.get("Cache-Control") == "no-store"
        assert reply.headers.get("X-Content-Type-Options") == "nosniff"
        assert reply.headers.get("Content-Type") == "application/json"
        if reply.status >= 400:
            _no_leak(reply, leaks)
        else:
            _no_secret(reply)


def test_http11_keepalive_is_closed_and_a_pipelined_request_is_never_answered(
    wired, server
):
    first = _head(server, "POST", SNAPSHOT, extra=JSON_CT + (
        ("Connection", "keep-alive"),), content_length=len(EMPTY)) + EMPTY
    body = _route_body(REPLAY, wired.head)
    second = _head(server, "POST", REPLAY, extra=JSON_CT,
                   content_length=len(body)) + body
    raw = _raw_bytes(server, first + second)
    assert raw.startswith(b"HTTP/1.0 200"), raw[:64]
    assert raw.count(b"HTTP/1.") == 1  # no second, pipelined response
    assert b"Connection: close" in raw
    assert b"Keep-Alive" not in raw
    get = _raw_bytes(server, _head(server, "GET", SNAPSHOT, content_length=0,
                                   extra=(("Connection", "keep-alive"),)),
                     read_timeout=3.0)
    assert get.startswith(b"HTTP/1.0 405") and get.count(b"HTTP/1.") == 1


# --------------------------------------------------- core failure translation


@pytest.mark.parametrize("error,status,code", CORE_FAILURES, ids=CORE_FAILURE_IDS)
def test_core_failures_map_to_fixed_codes_without_echo(
    wired, server, error, status, code, monkeypatch, caplog, capsys, leaks
):
    secret = (f"credential={ALICE} context=keep it small at {wired.store.store_path} "
              "git push failed: fatal: unable to access")
    messages: list[str] = []

    def raising(self, credential):
        raise error(secret)

    monkeypatch.setattr(Coordinator, "snapshot", raising)
    with caplog.at_level(logging.DEBUG):
        for _ in range(2):  # deterministic and fixed, not an echo of the core text
            reply = _call(server)
            messages.append(_error(reply, code=code, status=status)["message"])
            _no_leak(reply, leaks)
    assert len(set(messages)) == 1
    _silent(caplog, capsys)


def test_the_core_failure_classes_share_a_small_fixed_vocabulary(
    wired, server, monkeypatch, leaks
):
    messages = set()
    for error, status, code in CORE_FAILURES:
        def raising(self, credential, _kind=error):
            raise _kind(f"raw {ALICE} at {wired.store.store_path} keep it small git")

        monkeypatch.setattr(Coordinator, "snapshot", raising)
        reply = _call(server)
        messages.add(_error(reply, code=code, status=status)["message"])
        _no_leak(reply, leaks)
    assert len(messages) == 6  # one per documented status class, no more


def test_an_unserializable_snapshot_becomes_a_fixed_json_500(server, leaks):
    class _Broken:
        def to_dict(self):
            return {"revision": "a" * 40, "state": {"goal": "\ud800"}}

    server._coordinator.snapshot = lambda credential: _Broken()
    reply = _call(server)
    _error(reply, code="internal_error", status=500)
    _no_leak(reply, leaks)
    assert "\\ud800" not in reply.body.decode("utf-8")


# ----------------------------------------------- truncated and idle requests


def test_a_truncated_body_is_a_safe_400_after_authentication(server, leaks):
    for route, body in ((SNAPSHOT, b'{"a"'), (REPLAY, b'{"after_revision":'),
                        (CONTEXT, b'{"expected_revision": "a"')):
        payload = _head(server, "POST", route, content_length=len(body) + 40,
                        extra=JSON_CT) + body
        reply = _exchange(server, payload, close_write=True, read_timeout=5.0)
        _error(reply, code="invalid_request", status=400)
        _no_leak(reply, leaks)
    assert _call(server)[0] == 200


def test_an_idle_authenticated_request_ends_in_a_safe_400(server, monkeypatch, leaks):
    """A declared body that never arrives times out into a fixed 400, not a
    hang, a 500 or an echo. The handler timeout is shortened so the test is
    deterministic and fast instead of waiting the full 5 seconds."""
    monkeypatch.setattr(transport._LoopbackRequestHandler, "timeout", 0.3)
    for route in ROUTES:
        payload = _head(server, "POST", route, content_length=64, extra=JSON_CT)
        started = time.monotonic()
        reply = _exchange(server, payload, timeout=5.0, read_timeout=5.0)
        assert time.monotonic() - started < 4.0
        _error(reply, code="invalid_request", status=400)
        _no_leak(reply, leaks)
    assert _call(server)[0] == 200


def test_an_idle_head_produces_no_response_and_no_leak(
    server, monkeypatch, caplog, capsys, leaks
):
    monkeypatch.setattr(transport._LoopbackRequestHandler, "timeout", 0.3)
    partial = f"POST {SNAPSHOT} HTTP/1.1\r\nHost: {_authority(server)}\r\n"
    with caplog.at_level(logging.DEBUG):
        reply = _exchange(server, partial.encode(), timeout=5.0, read_timeout=5.0)
        if reply.status is None:
            assert reply.body == b""  # a dropped idle connection answers nothing
        else:  # a fixed, safe rejection is equally acceptable
            assert reply.status == 400
            _no_leak(reply, leaks)
        _silent(caplog, capsys)
    assert _call(server)[0] == 200


def test_parser_level_failures_answer_fixed_json_without_echo(server, leaks):
    authority = _authority(server)
    cases = {
        "one-word": b"GARBAGE\r\n\r\n",
        "bad-version": f"POST {SNAPSHOT} HTTP/9.9\r\nHost: {authority}\r\n\r\n".encode(),
        "non-numeric-version":
            f"POST {SNAPSHOT} HTTP/one.two\r\nHost: {authority}\r\n\r\n".encode(),
        "http09": f"GET {SNAPSHOT}\r\n\r\n".encode(),
        "bad-header-line":
            f"POST {SNAPSHOT} HTTP/1.1\r\nHost: {authority}\r\nNot A Header\r\n\r\n".encode(),
        "header-without-colon":
            f"POST {SNAPSHOT} HTTP/1.1\r\nHost: {authority}\r\nBroken\r\n\r\n".encode(),
        "empty-path": b"POST  HTTP/1.1\r\nHost: x\r\n\r\n",
        "binary-request-line": b"\x00\x01\x02 HTTP/1.1\r\n\r\n",
    }
    for name, payload in cases.items():
        reply = _exchange(server, payload, read_timeout=5.0)
        assert reply.status is not None, name
        assert reply.status in (400, 413), (name, reply.status)
        _error(reply, code="invalid_request")
        _no_leak(reply, leaks)
        text = reply.body.decode("utf-8", "replace")
        for fragment in ("GARBAGE", "HTTP/9.9", "Not A Header", "Broken",
                         "one.two", "Request timed out", authority):
            assert fragment not in text, (name, fragment)
    # A bare CRLF is not a request at all: the standard library ignores it and
    # closes the connection, so only the "no reply, no echo" half is assertable.
    blank = _exchange(server, b"\r\n", read_timeout=5.0)
    assert blank.body == b""
    assert _call(server)[0] == 200


# ------------------------------------------- deterministic races (no sleeps)


def test_two_concurrent_http_writes_leave_exactly_one_published_event(wired, server):
    barrier = threading.Barrier(2, timeout=30)
    outcomes: dict = {}

    def write(status):
        body = _route_body(TASK_STATUS, wired.head).replace(b"running", status.encode())
        try:
            barrier.wait()
            outcomes[status] = ("ok", _call(server, TASK_STATUS, body))
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcomes[status] = ("error", exc)

    threads = [threading.Thread(target=write, args=(status,), daemon=True, name=status)
               for status in ("running", "done")]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join(timeout=60)
    finally:
        barrier.abort()
    assert all(not thread.is_alive() for thread in threads)
    assert set(outcomes) == {"running", "done"}

    replies = [outcome[1] for outcome in outcomes.values() if outcome[0] == "ok"]
    assert len(replies) == 2, outcomes
    assert sorted(reply.status for reply in replies) == [200, 409]
    _error(next(reply for reply in replies if reply.status == 409),
           code="stale_revision", status=409)
    revision = _json(next(reply for reply in replies if reply.status == 200))["revision"]
    assert wired.store.remote_head() == revision

    page = _json(_call(server, REPLAY, _route_body(REPLAY, wired.head)))
    assert [event["revision"] for event in page["events"]] == [revision]
    assert page["has_more"] is False


def test_close_waits_for_the_in_flight_request_and_shares_one_drain(
    wired, server, monkeypatch
):
    parked, released = threading.Event(), threading.Event()
    real_snapshot = wired.coordinator.snapshot
    outcome: dict = {}

    def parked_snapshot(credential):
        parked.set()
        assert released.wait(timeout=30), "the parked request was never released"
        return real_snapshot(credential)

    monkeypatch.setattr(wired.coordinator, "snapshot", parked_snapshot)

    def request():
        try:
            outcome["reply"] = _call(server)
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
        assert parked.wait(timeout=30), "the request never reached the core"
        for thread in closers:
            thread.start()
        for thread in closers:
            thread.join(timeout=0.5)
        # both closers are still blocked on the same unfinished drain
        assert [thread.is_alive() for thread in closers] == [True, True]
        assert "reply" not in outcome and "close-error" not in outcome, outcome
    finally:
        released.set()
        worker.join(timeout=30)
        for thread in closers:
            thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in [worker, *closers])
    assert "request-error" not in outcome and "close-error" not in outcome, outcome
    assert outcome["reply"].status == 200, outcome["reply"].body
    assert outcome["close-1"] is None and outcome["close-2"] is None

    with socket.socket() as probe:
        probe.settimeout(1.0)
        with pytest.raises((ConnectionError, TimeoutError, OSError)):
            probe.connect(("127.0.0.1", _port(server)))  # the listener is closed


def test_reconnect_snapshot_then_writes_then_replay_loses_no_event(wired, servers):
    first = servers(wired.coordinator).start()
    cursor = _json(_call(first))["revision"]
    assert cursor == wired.head
    written = []
    for status in ("running", "done", "waiting"):
        body = _route_body(TASK_STATUS, written[-1] if written else cursor).replace(
            b"running", status.encode())
        reply = _call(first, TASK_STATUS, body)
        assert reply.status == 200, reply.body
        written.append(_json(reply)["revision"])
    first.close()

    second = servers(wired.coordinator).start()
    assert _json(_call(second))["revision"] == written[-1]
    page = _json(_call(second, REPLAY, _route_body(REPLAY, cursor)))
    assert [event["revision"] for event in page["events"]] == written
    assert [event["previous_revision"] for event in page["events"]] == (
        [cursor, *written[:-1]]
    )
    assert page["next_revision"] == page["head_revision"] == written[-1]
    assert page["has_more"] is False
    # the same interval replays identically, page after page
    assert _json(_call(second, REPLAY, _route_body(REPLAY, cursor))) == page
