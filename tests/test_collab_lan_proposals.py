from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest

from collab_runtime.coordinator import Coordinator
from collab_runtime.errors import ValidationError
from collab_runtime.lan_proposals import TlsProposalClient, TlsProposalServer
from test_collab_proposals import _world, _mutate_frontend, _capture_frontend

OWNER = "O" * 40
MEMBER = "A" * 40


@pytest.fixture
def proposal_lan(tmp_path, monkeypatch):
    import test_collab_proposals as proposal_fixture
    # Candidate assembly deliberately rejects symlink baselines; this fixture
    # focuses the regular-file LAN proposal flow.
    monkeypatch.setattr(proposal_fixture, "SYMLINKS_AVAILABLE", False)
    original_write = proposal_fixture._write
    def write_baseline_check(root, rel, content, **kwargs):
        if rel == ".gitignore":
            original_write(root, rel, "build/\n", **kwargs)
            original_write(root, ".imece/verify.json", json.dumps([{
                "id": "candidate-proof", "title": "Verify selected result",
                "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('app.py').read_text() == 'A2\\n'"],
                "timeout_ms": 5000,
            }]))
            return root / rel
        return original_write(root, rel, content, **kwargs)
    monkeypatch.setattr(proposal_fixture, "_write", write_baseline_check)
    world = _world(tmp_path)
    _mutate_frontend(world)
    proposal = _capture_frontend(world, "lan-prop-1", paths=["app.py"])
    coordinator = Coordinator(world.store_a, session_id="demo-1", owner_id="owner",
                              member_credentials={"owner": OWNER, "alice": MEMBER})
    key, cert, der = tmp_path / "key.pem", tmp_path / "cert.pem", tmp_path / "cert.der"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    subprocess.run(["openssl", "x509", "-in", str(cert), "-outform", "DER", "-out", str(der)], check=True)
    control = __import__("collab_runtime.transport", fromlist=["LoopbackServer"]).LoopbackServer(
        coordinator, allow_lan=True, bind_address="127.0.0.1", certificate=str(cert), private_key=str(key)).start()
    server = TlsProposalServer(coordinator, bind_address="127.0.0.1", certificate=str(cert), private_key=str(key)).start()
    client = TlsProposalClient(server.base_url, certificate_sha256=hashlib.sha256(der.read_bytes()).hexdigest(),
                               session_id="demo-1")
    try:
        yield world, coordinator, control, server, client, proposal
    finally:
        server.close()
        control.close()


def test_separate_pinned_tls_channel_transports_only_selected_proposal(proposal_lan):
    world, coordinator, _control, server, client, proposal = proposal_lan
    receipt = client.publish(proposal, expected_revision=world.rev3, credential=MEMBER)
    assert receipt["proposal_id"] == proposal.proposal_id
    assert receipt["file_count"] == 1
    fetched = client.fetch(proposal.proposal_id, credential=MEMBER)
    assert fetched == proposal
    assert [item.path for item in fetched.files] == ["app.py"]
    assert fetched.files[0].after_bytes == b"A2\n"
    assert (world.frontend / "app.py").read_bytes() == b"A2\n"
    assert "new.py" not in [entry.path for entry in fetched.files]  # unselected changes never cross the channel
    from collab_runtime.candidates import assemble_candidate
    candidate_root = world.frontend.parent / "explicit-candidate"
    candidate = assemble_candidate(world.store_b, world.backend, [proposal.proposal_id], candidate_root,
        expected_revision=receipt["session_revision"], verify=True)
    assert candidate["verification"]["status"] == "pass"
    assert (candidate_root / "app.py").read_bytes() == b"A2\n"
    assert (world.backend / "app.py").read_bytes() == b"A\n"  # transport/materialization never applies to Source
    with pytest.raises(ValidationError):
        client.publish(proposal, expected_revision=world.rev3, credential=MEMBER)  # immutable duplicate ID


def test_noncanonical_json_publish_receipt_hashes_persisted_canonical_artifact(proposal_lan):
    world, _coordinator, _control, server, _client, proposal = proposal_lan
    from collab_runtime.proposals import proposal_bytes
    canonical = proposal_bytes(proposal)
    raw = b" \n" + canonical + b" \n"
    import socket, ssl
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
    headers = (f"POST /v1/proposal/publish HTTP/1.1\r\nHost: {server.address}:{server.port}\r\n"
        "X-Imece-Session: demo-1\r\nAuthorization: Bearer " + MEMBER + "\r\n"
        "Content-Type: application/json\r\nX-Imece-Revision: " + world.rev3 + "\r\n"
        f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n").encode()
    with socket.create_connection((server.address, server.port), timeout=3) as sock:
        with context.wrap_socket(sock, server_hostname="127.0.0.1") as stream:
            stream.sendall(headers + raw)
            response = bytearray()
            while chunk := stream.recv(4096): response.extend(chunk)
    body = bytes(response).split(b"\r\n\r\n", 1)[1]
    assert hashlib.sha256(canonical).hexdigest().encode() in body
    assert hashlib.sha256(raw).hexdigest().encode() not in body


def test_proposal_channel_rejects_wrong_owner_without_publication(proposal_lan):
    world, _coordinator, _control, _server, client, proposal = proposal_lan
    forged = type(proposal)(proposal.proposal_id, proposal.task_id, "mallory", proposal.session_id,
        proposal.base_commit, proposal.context_revision, proposal.context_hash, proposal.files)
    with pytest.raises(Exception):
        client.publish(forged, expected_revision=world.rev3, credential=MEMBER)
    assert world.store_a.remote_proposal_head(proposal.proposal_id) is None


def test_revoked_member_cannot_fetch_or_publish_on_proposal_listener(proposal_lan):
    world, coordinator, control, _server, client, proposal = proposal_lan
    client.publish(proposal, expected_revision=world.rev3, credential=MEMBER)
    coordinator.revoke_member_credential(OWNER, "alice")
    from collab_runtime.errors import AccessDeniedError
    with pytest.raises(AccessDeniedError):
        coordinator.authenticate_member(MEMBER)
    with pytest.raises(ValidationError):
        client.fetch(proposal.proposal_id, credential=MEMBER)


def test_proposal_bytes_are_not_a_control_route(proposal_lan):
    _world_data, _coordinator, control, _server, _client, _proposal = proposal_lan
    assert control._pairing is not None
    assert "/v1/proposal/fetch/lan-prop-1" not in __import__("collab_runtime.transport", fromlist=["_ROUTES"])._ROUTES


def test_proposal_listener_authenticates_and_binds_session_before_reading_body(proposal_lan):
    _world_data, _coordinator, _control, server, _client, _proposal = proposal_lan
    import socket
    import ssl
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((server.address, server.port), timeout=3) as raw:
        with context.wrap_socket(raw, server_hostname="127.0.0.1") as stream:
            stream.sendall((f"POST /v1/proposal/publish HTTP/1.1\r\nHost: {server.address}:{server.port}\r\n"
                "X-Imece-Session: demo-1\r\nAuthorization: Bearer invalid-credential\r\n"
                "Content-Type: application/json\r\nContent-Length: 2097152\r\nConnection: close\r\n\r\n").encode("ascii"))
            response = stream.recv(1024)
            assert b"401" in response
    with socket.create_connection((server.address, server.port), timeout=3) as raw:
        with context.wrap_socket(raw, server_hostname="127.0.0.1") as stream:
            stream.sendall((f"POST /v1/proposal/publish HTTP/1.1\r\nHost: {server.address}:{server.port}\r\n"
                "X-Imece-Session: another-session\r\nAuthorization: Bearer " + MEMBER + "\r\n"
                "Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}").encode("ascii"))
            assert b"403" in stream.recv(1024)


def test_publish_transport_failure_after_request_is_explicitly_uncertain(proposal_lan, monkeypatch):
    import collab_runtime.lan_proposals as lan_proposals
    world, _coordinator, _control, server, _client, proposal = proposal_lan
    client = TlsProposalClient(server.base_url, certificate_sha256=hashlib.sha256(b"pinned-cert").hexdigest(),
                               session_id="demo-1")
    class Peer:
        def getpeercert(self, binary_form=False): return b"pinned-cert"
    class LostAfterSend:
        def __init__(self, *args, **kwargs): self.sock = Peer()
        def connect(self): pass
        def request(self, *args, **kwargs): raise OSError("simulated response loss")
        def close(self): pass
    monkeypatch.setattr(lan_proposals.http.client, "HTTPSConnection", LostAfterSend)
    with pytest.raises(lan_proposals.ProposalChannelError) as caught:
        client.publish(proposal, expected_revision=world.rev3, credential=MEMBER)
    assert caught.value.code == "outcome_unknown" and caught.value.outcome_uncertain is True
    assert "simulated" not in str(caught.value)


def test_publish_server_failure_is_reported_as_uncertain(proposal_lan, monkeypatch):
    import collab_runtime.lan_proposals as transport
    from collab_runtime.errors import GitOperationError
    world, _coordinator, _control, _server, client, proposal = proposal_lan
    monkeypatch.setattr(transport, "publish_proposal", lambda *args, **kwargs: (_ for _ in ()).throw(
        GitOperationError("safe failure")))
    with pytest.raises(transport.ProposalChannelError) as caught:
        client.publish(proposal, expected_revision=world.rev3, credential=MEMBER)
    assert caught.value.code == "outcome_unknown" and caught.value.outcome_uncertain
    assert "safe failure" not in str(caught.value)


def test_proposal_listener_never_sends_continue_before_authentication(proposal_lan):
    _world_data, _coordinator, _control, server, _client, _proposal = proposal_lan
    import socket
    import ssl
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((server.address, server.port), timeout=3) as raw:
        with context.wrap_socket(raw, server_hostname="127.0.0.1") as stream:
            stream.sendall((f"POST /v1/proposal/publish HTTP/1.1\r\nHost: {server.address}:{server.port}\r\n"
                "Expect: 100-continue\r\nContent-Length: 2\r\nConnection: close\r\n\r\n").encode("ascii"))
            response = stream.recv(1024)
            assert b"100 Continue" not in response
            assert response.startswith(b"HTTP/1.0 403")  # no session supplied; fixed rejection precedes body


def test_proposal_listener_rejects_large_headers_origin_and_unknown_methods_safely(proposal_lan):
    _world_data, _coordinator, _control, server, _client, _proposal = proposal_lan
    import socket
    import ssl
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
    def request(raw):
        with socket.create_connection((server.address, server.port), timeout=3) as sock:
            with context.wrap_socket(sock, server_hostname="127.0.0.1") as stream:
                stream.sendall(raw)
                response = bytearray()
                while chunk := stream.recv(4096): response.extend(chunk)
                return bytes(response)
    large = (f"POST /v1/proposal/publish HTTP/1.1\r\nHost: {server.address}:{server.port}\r\n"
             f"X-Long: {'x' * 66000}\r\n\r\n").encode()
    response = request(large)
    assert b"413" in response and b"proposal request rejected" in response
    assert b"x" * 100 not in response and b"<html" not in response.lower()
    origin = request((f"POST /v1/proposal/publish HTTP/1.1\r\nHost: {server.address}:{server.port}\r\n"
        f"X-Imece-Session: demo-1\r\nOrigin: https://attacker.invalid\r\nAuthorization: Bearer {MEMBER}\r\n"
        "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}").encode())
    assert b"400" in origin
    unknown = request((f"WEIRD /v1/secret HTTP/1.1\r\nHost: {server.address}:{server.port}\r\n"
        f"X-Imece-Session: demo-1\r\nAuthorization: Bearer {MEMBER}\r\nContent-Type: application/json\r\n"
        "Content-Length: 2\r\n\r\n{}").encode())
    assert b"404" in unknown or b"405" in unknown
    assert b"WEIRD" not in unknown and b"<html" not in unknown.lower()


def test_revocation_does_not_wait_for_slow_proposal_body_and_denies_publish(proposal_lan):
    _world_data, coordinator, _control, server, _client, _proposal = proposal_lan
    import socket
    import ssl
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((server.address, server.port), timeout=3) as sock:
        with context.wrap_socket(sock, server_hostname="127.0.0.1") as stream:
            stream.sendall((f"POST /v1/proposal/publish HTTP/1.1\r\nHost: {server.address}:{server.port}\r\n"
                "X-Imece-Session: demo-1\r\nAuthorization: Bearer " + MEMBER + "\r\n"
                "Content-Type: application/json\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{").encode())
            coordinator.revoke_member_credential(OWNER, "alice")
            stream.sendall(b" " * 99)
            response = stream.recv(1024)
            assert b"401" in response
    assert _world_data.store_a.remote_proposal_head(_proposal.proposal_id) is None


def test_stale_revision_does_not_publish(proposal_lan):
    world, _coordinator, _control, _server, client, proposal = proposal_lan
    with pytest.raises(ValidationError):
        client.publish(proposal, expected_revision="0" * 40, credential=MEMBER)
    assert world.store_a.remote_proposal_head(proposal.proposal_id) is None


def test_proposal_listener_constructor_requires_real_coordinator_and_exact_port(proposal_lan, tmp_path):
    from collab_runtime.lan_proposals import TlsProposalServer
    _world_data, coordinator, _control, _server, _client, _proposal = proposal_lan
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    # Existing fixture owns the certificate paths; use them from the active TLS context's source fixture.
    # Constructor rejects these inputs before trying to bind/load any listener.
    with pytest.raises(ValidationError):
        TlsProposalServer(object(), bind_address="127.0.0.1", certificate=str(cert), private_key=str(key))
    with pytest.raises(ValidationError):
        TlsProposalServer(coordinator, bind_address="127.0.0.1", certificate=str(cert), private_key=str(key), port=True)


def test_proposal_close_from_owned_worker_is_refused_before_shutdown(proposal_lan):
    _world_data, _coordinator, _control, server, _client, _proposal = proposal_lan
    import threading
    failures = []
    def close_from_worker():
        current = threading.current_thread()
        with server._server._thread_lock:
            server._server._threads.add(current)
        try:
            server.close()
        except ValidationError as exc:
            failures.append(str(exc))
        finally:
            with server._server._thread_lock:
                server._server._threads.discard(current)
    worker = threading.Thread(target=close_from_worker)
    worker.start(); worker.join(3)
    assert not worker.is_alive() and failures
    assert not server._closed and server._thread.is_alive()


def test_candidate_materialization_rejects_casefold_path_aliases():
    # Legal on some source filesystems, ambiguous on Windows/NTFS; reject
    # before candidate writes rather than letting the last file win.
    from collab_runtime.candidates import _validate_final_set
    with pytest.raises(ValidationError, match="case collision"):
        _validate_final_set([("A.txt", b"A", 0o644), ("a.txt", b"a", 0o644)])
