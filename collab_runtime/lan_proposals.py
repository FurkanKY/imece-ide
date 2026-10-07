"""Opt-in, separately bound TLS transport for explicitly selected proposal bytes.

This module intentionally has no pairing, listing, control, filesystem-write,
or candidate-apply route. A caller may publish one captured proposal or fetch
one named immutable proposal; integration remains a separate explicit action.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collab_runtime.errors import AccessDeniedError, StaleRevisionError, ValidationError
from collab_runtime.models import MAX_JSON_BYTES, canonical_json_bytes, parse_json_bytes, safe_id, sha_hex
from collab_runtime.proposals import parse_proposal_bytes, proposal_bytes, publish_proposal, read_proposal
from collab_runtime.store import MAX_PROPOSAL_JSON_BYTES
from collab_runtime.transport import _HeadReader

_MAX_REQUEST = MAX_PROPOSAL_JSON_BYTES + 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)


class ProposalChannelError(ValidationError):
    """Fixed safe transport result; ambiguous publish outcomes require reconciliation."""
    def __init__(self, code: str, *, outcome_uncertain: bool):
        self.code = code
        self.outcome_uncertain = outcome_uncertain
        message = "proposal publication outcome is uncertain; fetch the same immutable id to reconcile." if outcome_uncertain else "proposal channel request was rejected."
        super().__init__(message)


def _private_ipv4(value):
    try:
        address = ipaddress.ip_address(value)
        return isinstance(address, ipaddress.IPv4Address) and (
            address.is_loopback or any(address in ipaddress.ip_network(net) for net in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
    except ValueError:
        return False


class _BoundedTlsHttpServer(HTTPServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, address, owner):
        self.owner = owner
        self._slots = threading.BoundedSemaphore(8)
        self._threads = set()
        self._thread_lock = threading.Lock()
        super().__init__(address, _ProposalHandler)

    def server_close(self):
        super().server_close()
        self.drain()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        def serve():
            tls = None
            try:
                request.settimeout(5)
                tls = self.owner.context.wrap_socket(request, server_side=True)
                self.finish_request(tls, client_address)
            except Exception:
                try: self.shutdown_request(tls or request)
                except Exception: pass
            else:
                self.shutdown_request(tls)
            finally:
                with self._thread_lock: self._threads.discard(threading.current_thread())
                self._slots.release()
        thread = threading.Thread(target=serve, name="imece-proposal-tls", daemon=False)
        with self._thread_lock: self._threads.add(thread)
        try: thread.start()
        except Exception:
            with self._thread_lock: self._threads.discard(thread)
            self._slots.release(); self.shutdown_request(request)

    def drain(self):
        with self._thread_lock: threads = tuple(self._threads)
        for thread in threads: thread.join()


class _ProposalHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "ImeceProposal/1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.rfile = _HeadReader(self.rfile)

    def log_message(self, fmt, *args):
        return None

    def send_error(self, code, message=None, explain=None):
        # Never let BaseHTTPRequestHandler reflect untrusted method/URL/parser text.
        status = 417 if code == 417 else 413 if code in (413, 414, 431) else 400
        body = canonical_json_bytes({"error": {"code": "request_rejected", "message": "proposal request rejected."}})
        self._reply(status, body)
        self.close_connection = True

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._handle_any
        raise AttributeError(name)

    def _handle_any(self):
        self._process_request()

    @property
    def owner(self): return self.server.owner

    def handle_expect_100(self):
        # Never emit an interim 100 Continue; the fixed rejection reveals no
        # method/path details and does not consume a request body.
        body = canonical_json_bytes({"error": {"code": "request_rejected", "message": "proposal request rejected."}})
        self._reply(417, body)
        self.close_connection = True
        return False

    def _reply(self, status, payload, *, content_type="application/json", headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in headers: self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _fail(self, status=400):
        body = canonical_json_bytes({"error": {"code": "request_rejected", "message": "proposal request rejected."}})
        self._reply(status, body)

    def do_POST(self):
        self._process_request()

    def _process_request(self):
        owner = self.owner
        try:
            if self.request_version not in ("HTTP/1.0", "HTTP/1.1") or "?" in self.path:
                return self._fail()
            if self.headers.get_all("Host") != [f"{owner.address}:{owner.port}"]:
                return self._fail()
            if self.headers.get_all("X-Imece-Session") != [owner.coordinator.session_id]:
                return self._fail(403)
            if (self.headers.get_all("Origin") or self.headers.get_all("Transfer-Encoding")
                    or self.headers.get_all("Expect")):
                return self._fail()
            auth = self.headers.get_all("Authorization") or []
            if len(auth) != 1:
                return self._fail(401)
            scheme, sep, credential = auth[0].strip().partition(" ")
            if scheme.lower() != "bearer" or not sep or not credential:
                return self._fail(401)
            content_types = self.headers.get_all("Content-Type") or []
            if content_types != ["application/json"]:
                return self._fail(415)
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0].strip(), re.ASCII):
                return self._fail()
            length = int(lengths[0].strip())
            if not 1 <= length <= _MAX_REQUEST:
                return self._fail(413)
            with owner.coordinator._lock:
                owner.coordinator.authenticate_member(credential)
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self._fail()
            if self.command != "POST":
                return self._fail(405)
            publish = self.path == "/v1/proposal/publish"
            match = re.fullmatch(r"/v1/proposal/fetch/([A-Za-z0-9_-]{1,128})", self.path, re.ASCII)
            if not publish and not match:
                return self._fail(404)
            if publish:
                expected = self.headers.get_all("X-Imece-Revision") or []
                if len(expected) != 1:
                    return self._fail()
                proposal = parse_proposal_bytes(raw)
                with owner.coordinator._lock:
                    principal = owner.coordinator.authenticate_member(credential)
                    if proposal.owner != principal:
                        return self._fail(403)
                    revision, commit = publish_proposal(owner.coordinator._store, proposal,
                                                        expected_revision=expected[0])
                artifact_hash = hashlib.sha256(proposal_bytes(proposal)).hexdigest()
                receipt = {"proposal_id": proposal.proposal_id, "proposal_commit": commit,
                           "session_revision": revision, "artifact_sha256": artifact_hash,
                           "file_count": len(proposal.files)}
                return self._reply(200, canonical_json_bytes(receipt))
            payload = parse_json_bytes(raw, what="proposal fetch request", max_bytes=1024)
            if payload != {}:
                return self._fail()
            identifier = safe_id(match[1], "proposal id")
            with owner.coordinator._lock:
                owner.coordinator.authenticate_member(credential)
                proposal = read_proposal(owner.coordinator._store, identifier)
                artifact = proposal_bytes(proposal)
            if len(artifact) > MAX_PROPOSAL_JSON_BYTES:
                return self._fail(413)
            digest = hashlib.sha256(artifact).hexdigest()
            return self._reply(200, artifact, content_type="application/vnd.imece.proposal+json",
                               headers=(("X-Imece-SHA256", digest),))
        except AccessDeniedError:
            return self._fail(401)
        except StaleRevisionError:
            return self._fail(409)
        except ValidationError:
            return self._fail(400)
        except Exception:
            return self._fail(500)

    def do_GET(self): self._process_request()
    def do_PUT(self): self._process_request()
    def do_DELETE(self): self._process_request()


class TlsProposalServer:
    """Explicit second TLS listener sharing coordinator credentials/session."""
    def __init__(self, coordinator, *, bind_address, certificate, private_key, port=0):
        from collab_runtime.coordinator import Coordinator
        if (not isinstance(coordinator, Coordinator) or type(port) is not int
                or not _private_ipv4(bind_address) or not 0 <= port <= 65535):
            raise ValidationError("proposal transport requires an explicit private IPv4 address and port.")
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certificate, private_key, password=lambda: "")
            self.context = context
        except Exception:
            raise ValidationError("proposal TLS certificate/key could not be loaded safely.") from None
        self.coordinator = coordinator
        self.address = str(ipaddress.ip_address(bind_address))
        self._lock = threading.RLock()
        try: self._server = _BoundedTlsHttpServer((self.address, port), self)
        except OSError: raise ValidationError("proposal TLS listener could not bind.") from None
        self.port = self._server.server_address[1]
        self._thread = None
        self._closed = False

    @property
    def base_url(self): return f"https://{self.address}:{self.port}"

    def start(self):
        if self._closed: raise ValidationError("closed proposal listener cannot restart.")
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever,
                kwargs={"poll_interval": .1}, name="imece-proposal-accept", daemon=True)
            self._thread.start()
        return self

    def close(self):
        current = threading.current_thread()
        with self._server._thread_lock:
            if current is self._thread or current in self._server._threads:
                raise ValidationError("proposal listener cannot close from its worker thread.")
        if self._closed: return
        if self._thread is not None:
            self._server.shutdown(); self._thread.join()
        self._server.server_close(); self._server.drain(); self._closed = True


class TlsProposalClient:
    """Pinned direct-IP client for one explicit proposal publish/fetch."""
    def __init__(self, base_url, *, certificate_sha256, session_id):
        if not isinstance(base_url, str) or not re.fullmatch(r"https://[0-9.]+:[1-9][0-9]{0,4}", base_url):
            raise ValidationError("an explicit HTTPS literal-IP proposal endpoint is required.")
        host, port = base_url[8:].rsplit(":", 1)
        if not _private_ipv4(host) or int(port) > 65535 or not re.fullmatch(r"[0-9a-f]{64}", certificate_sha256):
            raise ValidationError("a pinned private-IP proposal endpoint is required.")
        self.host, self.port, self.pin = host, int(port), certificate_sha256
        self.session_id = safe_id(session_id, "session id")

    def _request(self, route, payload, credential, *, revision=None, fetching=False):
        raw = payload if isinstance(payload, bytes) else canonical_json_bytes(payload)
        limit = MAX_PROPOSAL_JSON_BYTES if fetching else _MAX_REQUEST
        if not 0 < len(raw) <= limit:
            raise ValidationError("proposal artifact exceeds its transport limit.")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=5, context=context)
        sent = False
        try:
            conn.connect()
            cert = conn.sock.getpeercert(binary_form=True)
            if not cert or hashlib.sha256(cert).hexdigest() != self.pin:
                raise ValidationError("proposal TLS certificate pin mismatch.")
            headers = {"Authorization": "Bearer " + credential,
                "X-Imece-Session": self.session_id, "Content-Type": "application/json",
                "Content-Length": str(len(raw)), "Connection": "close"}
            if revision is not None: headers["X-Imece-Revision"] = sha_hex(revision, "expected revision")
            sent = True
            conn.request("POST", route, body=raw, headers=headers)
            response = conn.getresponse()
            # Reject redirects and ambiguous framing. http.client headers preserve duplicates.
            if response.status in (301, 302, 303, 307, 308): raise ValidationError("proposal redirect refused.")
            if response.getheader("Transfer-Encoding") is not None: raise ValidationError("ambiguous proposal framing.")
            lengths = response.msg.get_all("Content-Length") or []
            maximum = MAX_PROPOSAL_JSON_BYTES if fetching else MAX_JSON_BYTES
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0].strip(), re.ASCII):
                raise ValidationError("invalid proposal response framing.")
            size = int(lengths[0].strip())
            if size > maximum or size <= 0: raise ValidationError("oversize proposal response.")
            body = response.read(size + 1)
            if len(body) != size: raise ValidationError("truncated proposal response.")
            if response.status != 200:
                uncertain = response.status >= 500 and not fetching
                raise ProposalChannelError("outcome_unknown" if uncertain else "rejected",
                                          outcome_uncertain=uncertain)
            if fetching:
                if response.getheader("Content-Type") != "application/vnd.imece.proposal+json":
                    raise ValidationError("invalid proposal response type.")
                digest = hashlib.sha256(body).hexdigest()
                if response.getheader("X-Imece-SHA256") != digest: raise ValidationError("proposal checksum mismatch.")
                proposal = parse_proposal_bytes(body)
                if proposal.session_id != self.session_id or proposal.proposal_id != route.rsplit("/", 1)[-1]:
                    raise ValidationError("fetched proposal belongs to another session or id.")
                return proposal
            receipt = parse_json_bytes(body, what="proposal receipt", max_bytes=MAX_JSON_BYTES)
            expected_keys = {"proposal_id", "proposal_commit", "session_revision", "artifact_sha256", "file_count"}
            proposal = parse_proposal_bytes(raw)
            if not isinstance(receipt, dict) or set(receipt) != expected_keys: raise ValidationError("invalid proposal receipt.")
            sha_hex(receipt["proposal_commit"], "proposal commit"); sha_hex(receipt["session_revision"], "session revision")
            if (receipt["proposal_id"] != proposal.proposal_id
                    or type(receipt["file_count"]) is not int
                    or receipt["file_count"] != len(proposal.files)
                    or receipt["artifact_sha256"] != hashlib.sha256(proposal_bytes(proposal)).hexdigest()):
                raise ValidationError("proposal receipt does not match the canonical sent artifact.")
            return receipt
        except ProposalChannelError:
            raise
        except Exception:
            if sent and not fetching:
                raise ProposalChannelError("outcome_unknown", outcome_uncertain=True) from None
            if sent:
                raise ValidationError("proposal fetch failed after request transmission.") from None
            raise
        finally: conn.close()

    def publish(self, proposal, *, expected_revision, credential):
        from collab_runtime.proposals import Proposal, proposal_bytes
        if not isinstance(proposal, Proposal) or proposal.session_id != self.session_id:
            raise ValidationError("publish requires an explicitly captured proposal for this session.")
        if not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", credential):
            raise ValidationError("invalid proposal credential.")
        return self._request("/v1/proposal/publish", proposal_bytes(proposal), credential,
                             revision=expected_revision)

    def fetch(self, proposal_id, *, credential):
        identifier = safe_id(proposal_id, "proposal id")
        if not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", credential):
            raise ValidationError("invalid proposal credential.")
        return self._request(f"/v1/proposal/fetch/{identifier}", b"{}", credential, fetching=True)
