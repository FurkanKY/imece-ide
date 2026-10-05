"""Loopback HTTP/JSON v1 transport with bounded revision subscriptions.

Implements the "Loopback HTTP/JSON v1 contract" from docs/COLLABORATION.md.
Trusted local code wraps an already configured Coordinator in a
LoopbackServer bound to literal IPv4 127.0.0.1 only. The listener is the
Python standard library (socketserver/http.server); no dependency is needed,
there is no framework-driven 100 Continue before authentication, no
post-response body draining and no process exit on bind failure. The adapter
offers finite POST metadata routes and revision SSE, rejects everything else, authenticates
every request before reading or parsing any body, enforces separate strict
head and body budget guards, answers with fixed safe JSON errors (no URL,
path, body, credential, traceback or Git echo) and never logs requests or
errors. There is no keep-alive, CORS, redirect, proxy-header trust, static
file, CLI or code-proposal channel here. This module is not part of any
public export.
"""

from __future__ import annotations

import http.client
import re
import socket
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event, Lock, Thread, current_thread
from typing import Any

from collab_runtime.coordinator import Coordinator, ReplayPage
from collab_runtime.errors import (
    AccessDeniedError,
    CollabError,
    GitOperationError,
    ReplayUnavailableError,
    SessionNotFoundError,
    StaleRevisionError,
    ValidationError,
)
from collab_runtime.models import MAX_JSON_BYTES, canonical_json_bytes, parse_json_bytes

__all__ = ["LoopbackServer"]

# The JSON envelope and the request head have independent 64 KiB budgets.
_MAX_ENVELOPE_BYTES = MAX_JSON_BYTES
_MAX_HEAD_BYTES = MAX_JSON_BYTES
_ROUTES = ("/v1/snapshot", "/v1/context", "/v1/task-status", "/v1/replay", "/v1/subscribe")
_MAX_WORKERS = 12
_MAX_SUBSCRIPTIONS = 8
_REPLAY_LIMIT = 32
_OBSERVATION_INTERVAL = 1.0
_SUBSCRIBER_LIMIT = "the authenticated subscription limit has been reached."
_DIGITS = re.compile(r"[0-9]+", re.ASCII)
_ACCESS_DENIED = "access denied: a valid member credential and permission are required."
_PERMISSION_DENIED = "the member credential is not permitted to perform this operation."
_STALE_REVISION = "the expected revision does not match the current head revision."
_REPLAY_UNAVAILABLE = "the replay cursor is unavailable; fetch a new snapshot."
_INVALID_DATA = "the request contains invalid data."
_INVALID_JSON = "the request body is not valid strict JSON."
_SESSION_UNAVAILABLE = "the session is currently unavailable."
_INTERNAL_ERROR = "an unexpected internal error occurred."
_BIND_FAILURE = "the loopback listener could not be bound on 127.0.0.1."
_BEARER_CHALLENGE: tuple[tuple[str, str], ...] = (("WWW-Authenticate", "Bearer"),)
_ALLOW_POST: tuple[tuple[str, str], ...] = (("Allow", "POST"),)
_FIXED_RESPONSE_HEADERS: tuple[tuple[str, str], ...] = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Connection", "close"),
)
_REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    410: "Gone",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class _RequestError(Exception):
    """A fixed, safe transport-level rejection raised before any response."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str = "invalid_request",
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers


def _parser_error_response(code: int) -> tuple[int, str, str]:
    """Fixed JSON error for parser-level HTTP failures (request line,
    version, header parsing). Sizes that overflow a budget are 413; an
    unsupported version and everything else are 400 invalid_request."""
    if code in (413, 414, 431):
        return 413, "invalid_request", (
            f"the request envelope exceeds the {_MAX_ENVELOPE_BYTES}-byte limit."
        )
    if code == 505:
        return 400, "invalid_request", "the HTTP version of the request is not supported."
    return 400, "invalid_request", "the request could not be parsed."


def _domain_failure(error: CollabError) -> tuple[int, dict[str, Any]]:
    """Map a core error to the fixed status, code and message of the v1
    contract; core error messages are never echoed."""
    if isinstance(error, AccessDeniedError):
        return 403, {"code": "access_denied", "message": _PERMISSION_DENIED}
    if isinstance(error, StaleRevisionError):
        return 409, {"code": "stale_revision", "message": _STALE_REVISION}
    if isinstance(error, ReplayUnavailableError):
        return 410, {"code": "replay_unavailable", "message": _REPLAY_UNAVAILABLE}
    if isinstance(error, ValidationError):
        return 400, {"code": "invalid_request", "message": _INVALID_DATA}
    if isinstance(error, (SessionNotFoundError, GitOperationError)):
        return 503, {"code": "session_unavailable", "message": _SESSION_UNAVAILABLE}
    return 500, {"code": "internal_error", "message": _INTERNAL_ERROR}


def _bounded_length(raw: str, bound: int) -> int:
    """Convert a digit-only Content-Length value to an int without ever
    converting an unbounded digit string: leading zeros are stripped, the
    remaining digits are compared by length and lexicographic order against
    the bound, and only the bounded value is converted. An over-long value
    raises the fixed 413 envelope error."""
    if bound < 0:
        bound = 0
    digits = raw.lstrip("0")
    if not digits:
        return 0
    bound_digits = str(bound)
    if len(digits) > len(bound_digits) or (
        len(digits) == len(bound_digits) and digits > bound_digits
    ):
        raise _RequestError(
            413, f"the request envelope exceeds the {_MAX_ENVELOPE_BYTES}-byte limit."
        )
    return int(digits)


def _read_body_bytes(stream: Any, length: int) -> bytes:
    """Read exactly `length` body bytes; a short or interrupted read is a
    fixed 400, never a partial parse."""
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        try:
            chunk = stream.read(min(remaining, _MAX_ENVELOPE_BYTES))
        except (TimeoutError, ConnectionError, OSError):
            raise _RequestError(400, "the request body could not be read.") from None
        if not chunk:
            raise _RequestError(400, "the request body could not be read.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _HeadReader:
    """Reader proxy active while the request head is parsed: every readline
    is counted, the request line included, against the separate 64 KiB head
    budget and an overrun raises http.client.LineTooLong while reading.
    Body reads delegate to the underlying stream without counting."""

    __slots__ = ("stream", "count")

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.count = 0

    def readline(self, *args: Any) -> bytes:
        limit = _MAX_HEAD_BYTES - self.count + 1
        if args and args[0] >= 0:
            limit = min(limit, args[0])
        data = self.stream.readline(limit)
        self.count += len(data)
        if self.count > _MAX_HEAD_BYTES:
            raise http.client.LineTooLong("request line and headers") from None
        return data

    def read(self, *args: Any) -> bytes:
        return self.stream.read(*args)


class _LoopbackRequestHandler(BaseHTTPRequestHandler):
    """Sanitized standard-library request handler for the loopback listener.

    HTTP/1.0 responses only, so every response closes the connection; a
    5-second socket idle timeout; fully suppressed logging; dynamic do_*
    dispatch that routes every parsed method through the same authenticated
    handler; a separate 64 KiB head budget enforced while the request line
    and headers are read; and fixed JSON responses for parser-level HTTP
    errors. The duplicate-preserving email.message.Message headers are used
    directly so duplicates (Host, Content-Length, Authorization) stay
    visible. Parser error details and stdlib HTML/traceback output are
    never sent.
    """

    protocol_version = "HTTP/1.0"  # never keep-alive; every response closes
    timeout = 5  # socket idle timeout, not an overall execution deadline
    server_version = "imece-loopback"
    sys_version = ""

    def __getattr__(self, name: str) -> Any:
        """Route every parsed HTTP method through the single authenticated
        handler: unknown do_* dispatches (any unsupported method) reach the
        same authorization path and only then answer 404/405."""
        if name.startswith("do_"):
            return self._handle_any
        raise AttributeError(name)

    def handle_one_request(self) -> None:
        """Wrap the request stream in a head-budget reader for the duration
        of one request, so the raw request line and every header line count
        against the head budget while they are read."""
        reader = _HeadReader(self.rfile)
        self._head_reader = reader
        self.rfile = reader  # type: ignore[assignment]
        try:
            super().handle_one_request()
        except http.client.LineTooLong:
            # The over-long raw request line never reached parse_request.
            self.requestline = ""
            self.request_version = ""
            self.command = ""
            self.close_connection = True
            self.send_error(414)
        finally:
            self.rfile = reader.stream

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        """Parser-level HTTP errors become fixed safe JSON responses; the
        stdlib HTML body and any echo of the offending request fragment are
        discarded."""
        status, error_code, text = _parser_error_response(code)
        self.close_connection = True
        self._send_json(status, {"error": {"code": error_code, "message": text}}, ())

    def _send_json(
        self, status: int, payload: dict[str, Any], extra: tuple[tuple[str, str], ...]
    ) -> None:
        """Serialize the fixed payload and send it as one HTTP/1.0 response
        that closes the connection; nothing is logged."""
        self.close_connection = True
        if self.request_version not in ("HTTP/1.0", "HTTP/1.1"):
            # Parser failures, including HTTP/0.9, still get a framed error.
            self.request_version = "HTTP/1.0"
        try:
            body = canonical_json_bytes(payload)
        except Exception:
            # Serialization of a computed payload failed before any header
            # was sent; fall back to the fixed internal-error body only.
            status = 500
            body = canonical_json_bytes(
                {"error": {"code": "internal_error", "message": _INTERNAL_ERROR}}
            )
        try:
            self.send_response(status, _REASONS[status])
            self.send_header("Content-Type", "application/json")
            for name, value in extra:
                self.send_header(name, value)
            for name, value in _FIXED_RESPONSE_HEADERS:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (ConnectionError, TimeoutError, OSError):
            self.close_connection = True

    def _handle_any(self) -> None:
        """The single entry point for every parsed method; the loopback
        server owns all framing, authorization and domain decisions."""
        loopback: Any = self.server.loopback
        try:
            response = loopback._process(self)
            if response is None:
                return  # The subscription owns its entire response, including EOF.
            status, payload, extra = response
        except _RequestError as error:
            status = error.status
            payload = {"error": {"code": error.code, "message": error.message}}
            extra = error.headers
        except CollabError as error:
            status, failure = _domain_failure(error)
            payload = {"error": failure}
            extra = ()
        except Exception:
            status = 500
            payload = {"error": {"code": "internal_error", "message": _INTERNAL_ERROR}}
            extra = ()
        self._send_json(status, payload, extra)

    def log_message(self, format: str, *args: Any) -> None:
        return None


class _LoopbackHTTPServer(HTTPServer):
    """Bounded-worker standard-library HTTP server bound to literal IPv4
    127.0.0.1 with no reverse-DNS lookup and all server-side logging
    disabled."""

    def __init__(self, port: int, loopback: LoopbackServer) -> None:
        self.loopback = loopback
        super().__init__(("127.0.0.1", port), _LoopbackRequestHandler)

    def server_bind(self) -> None:
        """Bind exactly as TCPServer does but skip HTTPServer's
        socket.getfqdn reverse-DNS lookup: server_name stays the literal
        IPv4 address and server_port is the bound port."""
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]

    def handle_error(self, request: Any, client_address: Any) -> None:
        return None

    def process_request(self, request: Any, client_address: Any) -> None:
        """Admit directly into a worker, or close without parsing the socket.

        Registration and thread start share the tracking lock with shutdown.
        Completed threads remain tracked until reaped, so close joins even a
        worker finishing its final cleanup. There is no executor or queue.
        """
        admitted = False
        with self.loopback._tracking:
            if not self.loopback._stopping and len(self.loopback._workers) < _MAX_WORKERS:
                self.loopback._worker_threads = {
                    thread for thread in self.loopback._worker_threads if thread.is_alive()
                }
                thread = Thread(
                    target=self._run_request,
                    args=(request, client_address),
                    name="imece-loopback-request",
                    daemon=True,
                )
                self.loopback._workers.add(thread)
                self.loopback._worker_threads.add(thread)
                try:
                    thread.start()
                    admitted = True
                except Exception:
                    self.loopback._workers.discard(thread)
                    self.loopback._worker_threads.discard(thread)
        if not admitted:
            self.shutdown_request(request)

    def _run_request(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            try:
                self.shutdown_request(request)
            finally:
                with self.loopback._tracking:
                    self.loopback._workers.discard(current_thread())


class LoopbackServer:
    """Authenticated finite routes and revision streams on one loopback listener.

    Construction binds only literal IPv4 127.0.0.1 (no host argument,
    resolution or wildcard); port 0 selects a fresh ephemeral port. Bind
    failure raises a fixed local ValidationError. start()/close() are
    explicit, close is idempotent and works before start, and a closed
    server cannot be restarted. The context manager starts on entry and
    closes on exit.
    """

    def __init__(self, coordinator: Coordinator, *, port: int = 0) -> None:
        if not isinstance(coordinator, Coordinator):
            raise ValidationError("coordinator must be an already configured Coordinator.")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValidationError("port must be an integer between 0 and 65535.")
        self._coordinator = coordinator
        self._tracking = Lock()
        self._workers: set[Thread] = set()
        self._worker_threads: set[Thread] = set()
        self._subscriptions: set[socket.socket] = set()
        self._stopping = False
        self._stop = Event()
        try:
            self._server = _LoopbackHTTPServer(port, self)
        except OSError:
            raise ValidationError(_BIND_FAILURE) from None
        self._bound_port = int(self._server.server_address[1])
        self._thread: Thread | None = None
        self._closed = False
        self._lifecycle = Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._bound_port}"

    def start(self) -> LoopbackServer:
        with self._lifecycle:
            if self._closed or self._stopping:
                raise ValidationError(
                    "a closed loopback server cannot be restarted; construct a new server."
                )
            if self._thread is not None:
                return self
            thread = Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="imece-loopback-http",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                raise
        return self

    def close(self) -> None:
        # Check before taking the lifecycle lock: another closer may already
        # hold it while joining this very worker. Workers never take that lock.
        with self._tracking:
            if current_thread() is self._thread or current_thread() in self._worker_threads:
                raise ValidationError(
                    "close() cannot be called from a loopback accept or request worker thread."
                )
        with self._lifecycle:
            if self._closed:
                return
            with self._tracking:
                self._stopping = True
                self._stop.set()
                streams = tuple(self._subscriptions)
            # Registration is now closed, so this includes every live stream,
            # including a subscription still validating its first replay page.
            for connection in streams:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            thread = self._thread
            if thread is not None:
                self._server.shutdown()
                thread.join()
            with self._tracking:
                workers = tuple(self._worker_threads)
            # No admission is possible now. Unbounded joins intentionally drain
            # finite requests and Git/core calls, not just network activity.
            for worker in workers:
                worker.join()
            with self._tracking:
                self._worker_threads.clear()
            try:
                self._server.server_close()
            except OSError:
                raise ValidationError("the loopback listener could not be closed; retry close().") from None
            self._closed = True

    def __enter__(self) -> LoopbackServer:
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Request pipeline
    # ------------------------------------------------------------------

    def _process(
        self, handler: _LoopbackRequestHandler
    ) -> tuple[int, dict[str, Any], tuple[tuple[str, str], ...]] | None:
        """Guards, authentication and one domain operation for one parsed
        request. Returns the status, the fixed payload and any extra
        response headers; raises _RequestError for fixed transport-level
        rejections."""
        raw_headers = handler.headers
        route = handler.path

        # Framing and authority guards; they may reject without
        # authentication and never read metadata or a body.
        if handler.request_version not in ("HTTP/1.0", "HTTP/1.1"):
            raise _RequestError(400, "the HTTP version of the request is not supported.")
        if "?" in route:
            raise _RequestError(400, "query strings are not accepted.")
        hosts = raw_headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip() != f"127.0.0.1:{self._bound_port}":
            raise _RequestError(
                400,
                "the request must carry exactly one Host header naming the bound loopback authority.",
            )
        if raw_headers.get_all("Origin"):
            raise _RequestError(400, "Origin headers are not accepted.")
        if raw_headers.get_all("Transfer-Encoding"):
            raise _RequestError(400, "Transfer-Encoding is not accepted.")
        if raw_headers.get_all("Expect"):
            raise _RequestError(400, "the Expect header is not accepted.")
        lengths = raw_headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not _DIGITS.fullmatch(lengths[0].strip()):
            raise _RequestError(400, "exactly one decimal Content-Length header is required.")
        content_length = _bounded_length(lengths[0].strip(), _MAX_ENVELOPE_BYTES)

        # Authentication strictly before content-type, routing and any
        # body read/parse. Unknown routes and unsupported methods also
        # require authentication first.
        authorizations = raw_headers.get_all("Authorization") or []
        if len(authorizations) != 1:
            raise _RequestError(
                401, _ACCESS_DENIED, code="access_denied", headers=_BEARER_CHALLENGE
            )
        scheme, separator, credential = authorizations[0].strip(" \t").partition(" ")
        credential = credential.lstrip(" ")
        if scheme.lower() != "bearer" or not separator or not credential:
            raise _RequestError(
                401, _ACCESS_DENIED, code="access_denied", headers=_BEARER_CHALLENGE
            )
        try:
            self._coordinator.check_access(credential)
        except AccessDeniedError:
            raise _RequestError(
                401, _ACCESS_DENIED, code="access_denied", headers=_BEARER_CHALLENGE
            ) from None

        if route not in _ROUTES:
            raise _RequestError(404, "unknown route.")
        if handler.command != "POST":
            raise _RequestError(
                405, "only POST is accepted on the v1 routes.", headers=_ALLOW_POST
            )
        if route == "/v1/subscribe" and raw_headers.get_all("Last-Event-ID"):
            raise _RequestError(400, "Last-Event-ID is not accepted; use the JSON cursor.")

        content_types = raw_headers.get_all("Content-Type") or []
        if len(content_types) != 1 or content_types[0].strip().lower() != "application/json":
            raise _RequestError(415, "Content-Type must be application/json.")

        body = _read_body_bytes(handler.rfile, content_length)
        try:
            payload = parse_json_bytes(body, what="request body")
        except ValidationError:
            raise _RequestError(400, _INVALID_JSON) from None
        if not isinstance(payload, dict):
            raise _RequestError(400, "the request body must be a JSON object.")

        if route == "/v1/subscribe":
            if set(payload) != {"after_revision"}:
                raise _RequestError(
                    400, "the subscribe body must contain exactly after_revision."
                )
            self._subscribe(handler, credential, payload["after_revision"])
            return None

        if route == "/v1/snapshot":
            if payload:
                raise _RequestError(400, "the snapshot body must be an empty JSON object.")
            return 200, self._coordinator.snapshot(credential).to_dict(), ()

        if route == "/v1/context":
            if set(payload) != {"expected_revision", "context"}:
                raise _RequestError(
                    400, "the context body must contain exactly expected_revision and context."
                )
            revision = self._coordinator.update_context(
                credential,
                payload["context"],
                expected_revision=payload["expected_revision"],
            )
            return 200, {"revision": revision}, ()

        if route == "/v1/task-status":
            if set(payload) != {"expected_revision", "task_id", "status"}:
                raise _RequestError(
                    400,
                    "the task-status body must contain exactly expected_revision, "
                    "task_id and status.",
                )
            revision = self._coordinator.update_task_status(
                credential,
                task_id=payload["task_id"],
                status=payload["status"],
                expected_revision=payload["expected_revision"],
            )
            return 200, {"revision": revision}, ()

        # /v1/replay
        if "after_revision" not in payload or set(payload) - {"after_revision", "limit"}:
            raise _RequestError(
                400, "the replay body must contain after_revision and an optional limit."
            )
        if "limit" in payload:
            page = self._coordinator.replay(
                credential, after_revision=payload["after_revision"], limit=payload["limit"]
            )
        else:
            page = self._coordinator.replay(credential, after_revision=payload["after_revision"])
        return 200, page.to_dict(), ()

    def _subscribe(
        self, handler: _LoopbackRequestHandler, credential: str, after_revision: Any
    ) -> None:
        """Reserve before replay; release on every initial/streaming failure."""
        connection = handler.connection
        with self._tracking:
            if self._stopping:
                raise _RequestError(503, _SESSION_UNAVAILABLE, code="session_unavailable")
            if len(self._subscriptions) >= _MAX_SUBSCRIPTIONS:
                raise _RequestError(503, _SUBSCRIBER_LIMIT, code="subscriber_limit")
            self._subscriptions.add(connection)
        try:
            self._stream(handler, credential, after_revision)
        finally:
            with self._tracking:
                self._subscriptions.discard(connection)

    @staticmethod
    def _write_event(
        handler: _LoopbackRequestHandler,
        name: str,
        payload: dict[str, Any],
        *,
        revision: str | None = None,
    ) -> None:
        data = canonical_json_bytes(payload)
        frame = f"event: {name}\n".encode("ascii")
        if revision is not None:
            frame += f"id: {revision}\n".encode("ascii")
        handler.wfile.write(frame + b"data: " + data + b"\n\n")
        handler.wfile.flush()

    def _stream(
        self, handler: _LoopbackRequestHandler, credential: str, after_revision: Any
    ) -> None:
        """Keep one detached page; all writes and waits are outside core calls.

        After headers, failures must stay SSE (or EOF), never a second HTTP
        response. The same five-second socket timeout bounds stream writes.
        """
        # Only this frame owns the page. Initial core failures propagate as
        # ordinary fixed JSON responses before headers, outside the SSE guard.
        page: ReplayPage = self._coordinator.replay(
            credential, after_revision=after_revision, limit=_REPLAY_LIMIT
        )
        handler.close_connection = True
        try:
            if self._stop.is_set():
                return
            handler.send_response(200, _REASONS[200])
            handler.send_header("Content-Type", "text/event-stream")
            for name, value in _FIXED_RESPONSE_HEADERS:
                handler.send_header(name, value)
            handler.end_headers()
            handler.wfile.write(b": ready\n\n")
            handler.wfile.flush()
            while not self._stop.is_set():
                for event in page.events:
                    if self._stop.is_set():
                        return
                    self._write_event(
                        handler, "revision", event.to_dict(), revision=event.revision
                    )
                cursor = page.next_revision
                pending = page.has_more
                # Drop the old page before fetching another: no accumulating
                # event queue, even while Git or a network write is blocked.
                del page
                if not pending:
                    if self._stop.wait(_OBSERVATION_INTERVAL):
                        return
                    handler.wfile.write(b": heartbeat\n\n")
                    handler.wfile.flush()
                if self._stop.is_set():
                    return
                try:
                    page = self._coordinator.replay(
                        credential, after_revision=cursor, limit=_REPLAY_LIMIT
                    )
                except Exception as error:
                    # Distinguish even an unexpected core OSError from a
                    # socket failure: core failures get a fixed terminal event.
                    self._stream_error(handler, error)
                    return
        except (ConnectionError, TimeoutError, OSError):
            return
        except Exception as error:
            self._stream_error(handler, error)

    def _stream_error(self, handler: _LoopbackRequestHandler, error: Exception) -> None:
        if self._stop.is_set():
            return
        if isinstance(error, CollabError):
            _, failure = _domain_failure(error)
        else:
            failure = {"code": "internal_error", "message": _INTERNAL_ERROR}
        name = "resnapshot_required" if isinstance(error, ReplayUnavailableError) else "error"
        try:
            self._write_event(handler, name, {"error": failure})
        except Exception:
            pass  # A failed terminal write is simply EOF; never log details.
