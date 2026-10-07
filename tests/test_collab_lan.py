from __future__ import annotations

import concurrent.futures
import hashlib
import ipaddress
import socket
import ssl
import subprocess
import threading
import time

import pytest

from collab_runtime.coordinator import Coordinator
from collab_runtime.commands import TaskCommandError
from collab_runtime.errors import AccessDeniedError, ValidationError
from collab_runtime.lan import InvitationRegistry
from collab_runtime.lan_client import PairingCommandError, PinnedLanClient
from collab_runtime.models import build_initial_state, build_task
from collab_runtime.store import GitStore
from collab_runtime.transport import LoopbackServer

OWNER = "O" * 40
SHA = "a" * 40


@pytest.fixture
def lan(tmp_path):
    hub_path = GitStore.create_bare(tmp_path / "hub.git", what="hub")
    store_path = GitStore.create_bare(tmp_path / "store.git", what="store")
    store = GitStore(store=store_path, remote=str(hub_path))
    revision = store.init_session(build_initial_state(session_id="lan-test", target_version="v1", base_commit=SHA))
    store.upsert_task(build_task(task_id="task", owner="alice", goal="goal", scopes=["src/"],
                                 status="queued", context_revision=revision), expected_revision=revision)
    coordinator = Coordinator(store, session_id="lan-test", owner_id="owner",
                              member_credentials={"owner": OWNER})
    key, cert, der = tmp_path / "key.pem", tmp_path / "cert.pem", tmp_path / "cert.der"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    subprocess.run(["openssl", "x509", "-in", str(cert), "-outform", "DER", "-out", str(der)], check=True)
    server = LoopbackServer(coordinator, allow_lan=True, bind_address="127.0.0.1",
                            certificate=str(cert), private_key=str(key)).start()
    pin = hashlib.sha256(der.read_bytes()).hexdigest()
    server._test_key, server._test_cert = key, cert
    try:
        yield coordinator, server, PinnedLanClient(server.base_url, certificate_sha256=pin, session_id="lan-test"), pin
    finally:
        server.close()


def test_tls_pair_snapshot_owned_status_revoke_and_close(lan):
    coordinator, server, client, _pin = lan
    invite = server.issue_invitation(OWNER, "alice")
    member = client.pair(invite.code, "alice")
    with pytest.raises(ValidationError):
        client.pair(invite.code, "alice")
    revision, state = client.snapshot(member)
    assert state.session_id == "lan-test"
    revision = client.update_task_status(member, task_id="task", status="running", expected_revision=revision)
    assert client.snapshot(member)[1].tasks["task"].status == "running"
    server.revoke_member(OWNER, "alice")
    with pytest.raises(ValidationError):
        client.snapshot(member)
    invite2 = server.issue_invitation(OWNER, "bob")
    bob = client.pair(invite2.code, "bob")
    server.close()
    with pytest.raises(AccessDeniedError):
        coordinator.check_access(bob)


def test_member_self_leave_and_owner_cannot_leave(lan):
    coordinator, server, client, _pin = lan
    invite = server.issue_invitation(OWNER, "alice")
    member = client.pair(invite.code, "alice")
    client.leave(member)
    with pytest.raises(ValidationError):
        client.snapshot(member)
    with pytest.raises(PairingCommandError) as error:
        client.leave(OWNER)
    assert not error.value.outcome_uncertain
    with pytest.raises(AccessDeniedError):
        coordinator.check_access(member)


def test_pair_wire_failure_after_request_is_uncertain_but_wrong_pin_is_definitive(lan, monkeypatch):
    _coordinator, server, client, pin = lan
    invite = server.issue_invitation(OWNER, "alice")
    wrong = PinnedLanClient(server.base_url, certificate_sha256="0" * 64, session_id="lan-test")
    with pytest.raises(ValidationError):
        wrong.pair(invite.code, "alice")
    assert client.pair(invite.code, "alice")

    invite = server.issue_invitation(OWNER, "bob")
    broken = PinnedLanClient(server.base_url, certificate_sha256=pin, session_id="lan-test")
    from collab_runtime.client import LoopbackSnapshotClient
    def lost_response(*args, **kwargs):
        raise OSError("network detail must not escape")
    monkeypatch.setattr(LoopbackSnapshotClient, "_read_exact", staticmethod(lost_response))
    with pytest.raises(PairingCommandError) as error:
        broken.pair(invite.code, "bob")
    assert error.value.outcome_uncertain
    assert "network detail" not in str(error.value)


def test_wrong_pin_sends_no_invitation_and_tls_has_no_plaintext_fallback(lan):
    _coordinator, server, client, _pin = lan
    invite = server.issue_invitation(OWNER, "alice")
    wrong = PinnedLanClient(server.base_url, certificate_sha256="0" * 64, session_id="lan-test")
    with pytest.raises(ValidationError, match="pin"):
        wrong.pair(invite.code, "alice")
    # Pin failure occurs before the invitation is sent/redeemed.
    with pytest.raises(ValidationError, match="pin"):
        wrong.pair(invite.code, "alice")
    assert client.pair(invite.code, "alice")  # bad pin never sent/redeemed the invite
    for address in ("8.8.8.8", "0.0.0.0", "localhost", "192.0.2.1"):
        with pytest.raises(ValidationError):
            LoopbackServer(_coordinator, allow_lan=True, bind_address=address,
                           certificate="x", private_key="y")
    import socket
    raw = socket.create_connection(("127.0.0.1", server._bound_port), timeout=2)
    raw.sendall(b"GET /v1/snapshot HTTP/1.0\r\n\r\n")
    try:
        response = raw.recv(32)
    except ConnectionResetError:
        response = b""
    assert response == b""
    raw.close()


def test_invitation_one_use_expiry_and_atomic_redemption(lan):
    coordinator, _server, _client, _pin = lan
    now = [10.0]
    registry = InvitationRegistry(coordinator, clock=lambda: now[0])
    invite = registry.issue(OWNER, "alice")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: _redeem(registry, invite.code), range(2)))
    assert sum(isinstance(value, str) for value in outcomes) == 1
    assert sum(isinstance(value, AccessDeniedError) for value in outcomes) == 1
    registry.close()
    registry2 = InvitationRegistry(coordinator, clock=lambda: now[0])
    expired = registry2.issue(OWNER, "bob")
    now[0] += 301
    with pytest.raises(AccessDeniedError):
        registry2.check(expired.code)
    registry2.close()


def _redeem(registry, code):
    try:
        return registry.redeem(code, "alice")
    except AccessDeniedError as exc:
        return exc


def test_tls_auth_precedes_body_and_store_io(lan, monkeypatch):
    coordinator, server, _client, _pin = lan
    invite = server.issue_invitation(OWNER, "alice")
    # An invalid bearer/invitation must be rejected from headers before any
    # request-body read or canonical store access.
    monkeypatch.setattr(coordinator._store, "fetch_state", lambda: (_ for _ in ()).throw(AssertionError("store touched")))
    context = ssl.create_default_context(); context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", server._bound_port), timeout=2)
    tls = context.wrap_socket(raw, server_hostname="127.0.0.1")
    tls.sendall((f"POST /v1/pair HTTP/1.0\r\nHost: 127.0.0.1:{server._bound_port}\r\n"
                 "X-Imece-Session: lan-test\r\n"
                 "Authorization: Bearer invalid-invitation-credential\r\n"
                 "Content-Length: 65536\r\n\r\n").encode())
    response = tls.recv(1024)
    while b"\r\n\r\n" not in response:
        response += tls.recv(1024)
    body = tls.recv(1024)
    assert b"401 Unauthorized" in response and b"access_denied" in body
    tls.close()
    # The valid invitation is still usable: the failed attempt didn't consume it.
    assert _client.pair(invite.code, "alice")


def test_stale_revision_and_owner_role_cannot_be_escalated(lan):
    _coordinator, server, client, _pin = lan
    with pytest.raises(AccessDeniedError):
        server.issue_invitation("B" * 40, "alice")
    with pytest.raises(ValidationError):
        server.issue_invitation(OWNER, "owner")
    invite = server.issue_invitation(OWNER, "alice")
    member = client.pair(invite.code, "alice")
    revision, _state = client.snapshot(member)
    with pytest.raises(TaskCommandError) as error:
        client.update_task_status(member, task_id="task", status="done", expected_revision="0" * 40)
    assert error.value.code == "stale_revision" and not error.value.outcome_uncertain
    # Alice owns this task in the test session; a separate task-owner check is
    # enforced by Coordinator and is not bypassed by pairing identity.
    assert client.snapshot(member)[0] == revision


@pytest.mark.parametrize("action", ["revoke", "close"])
def test_revoke_or_close_serializes_with_inflight_redemption(lan, monkeypatch, action):
    coordinator, server, _client, _pin = lan
    invitation = server.issue_invitation(OWNER, "alice")
    entered = threading.Event()
    release = threading.Event()
    original = coordinator.register_member_credential_internal
    def blocked_register(member_id, credential):
        entered.set()
        assert release.wait(5)
        return original(member_id, credential)
    monkeypatch.setattr(coordinator, "register_member_credential_internal", blocked_register)
    redeemed = {}
    redeem_thread = threading.Thread(target=lambda: redeemed.setdefault(
        "credential", server._pairing.redeem(invitation.code, "alice")))
    redeem_thread.start()
    assert entered.wait(3)  # redeem holds PairingRegistry lock while registering
    revoked = threading.Event()
    def revoke_or_close():
        if action == "revoke":
            server.revoke_member(OWNER, "alice")
        else:
            server._pairing.close()
        revoked.set()
    revoke_thread = threading.Thread(target=revoke_or_close)
    revoke_thread.start()
    assert not revoked.wait(.1), "revocation must serialize behind the in-flight redemption"
    release.set()
    redeem_thread.join(3); revoke_thread.join(3)
    assert not redeem_thread.is_alive() and not revoke_thread.is_alive() and revoked.is_set()
    with pytest.raises(AccessDeniedError):
        coordinator.check_access(redeemed["credential"])
    with pytest.raises(AccessDeniedError):
        server._pairing.check(invitation.code)


def test_session_binding_rejects_before_body_or_store_io(lan, monkeypatch):
    coordinator, server, _client, _pin = lan
    monkeypatch.setattr(coordinator._store, "fetch_state", lambda: (_ for _ in ()).throw(AssertionError("store touched")))
    context = ssl.create_default_context(); context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", server._bound_port), timeout=2)
    tls = context.wrap_socket(raw, server_hostname="127.0.0.1")
    tls.sendall((f"POST /v1/snapshot HTTP/1.0\r\nHost: 127.0.0.1:{server._bound_port}\r\n"
                 "X-Imece-Session: another-session\r\nAuthorization: Bearer " + OWNER +
                 "\r\nContent-Type: application/json\r\nContent-Length: 65536\r\n\r\n").encode())
    response = tls.recv(4096)
    assert b"400 Bad Request" in response
    tls.close()


def test_task_status_malformed_tls_success_is_uncertain(lan):
    _coordinator, _server, _client, pin = lan
    key, cert = _server._test_key, _server._test_cert
    listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(1)
    port = listener.getsockname()[1]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(str(cert), str(key))
    def respond():
        connection, _ = listener.accept()
        with context.wrap_socket(connection, server_side=True) as tls:
            stream = tls.makefile("rb")
            headers = []
            while True:
                line = stream.readline(4096)
                headers.append(line)
                if line in (b"\r\n", b"\n", b""):
                    break
            request_headers = b"".join(headers)
            assert b"X-Imece-Session: lan-test" in request_headers
            content_length = next(int(line.split(b":", 1)[1]) for line in headers
                                  if line.lower().startswith(b"content-length:"))
            assert len(stream.read(content_length)) == content_length
            body = b'{"wrong":"shape"}'
            tls.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                        + str(len(body)).encode() + b"\r\n\r\n" + body)
        listener.close()
    thread = threading.Thread(target=respond); thread.start()
    malformed = PinnedLanClient(f"https://127.0.0.1:{port}", certificate_sha256=pin, session_id="lan-test")
    with pytest.raises(TaskCommandError) as error:
        malformed.update_task_status(OWNER, task_id="task", status="running", expected_revision="a" * 40)
    assert error.value.code == "protocol_error" and error.value.outcome_uncertain
    thread.join(3)
    assert not thread.is_alive()


def test_encrypted_private_key_fails_without_prompt(lan, tmp_path):
    coordinator, _server, _client, _pin = lan
    key, cert = tmp_path / "encrypted.pem", tmp_path / "encrypted.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-passout", "pass:secret",
                    "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=test"],
                   check=True, capture_output=True, timeout=10)
    with pytest.raises(ValidationError, match="LAN requires"):
        LoopbackServer(coordinator, allow_lan=True, bind_address="127.0.0.1",
                       certificate=str(cert), private_key=str(key))


def test_tls_handshake_runs_in_bounded_workers_and_close_drains(lan):
    _coordinator, server, _client, _pin = lan
    sockets = [socket.create_connection(("127.0.0.1", server._bound_port), timeout=2) for _ in range(4)]
    try:
        deadline = time.monotonic() + 2
        while len(server._workers) < len(sockets) and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(server._workers) <= 12
        assert len(server._workers) >= 1
    finally:
        for connection in sockets:
            connection.close()
    server.close()  # bounded TLS handshake workers drain before close returns.
    assert not server._workers


def test_lan_options_are_explicit_and_loopback_default_unchanged(lan):
    coordinator, server, _client, _pin = lan
    assert server.base_url.startswith("https://127.0.0.1:")
    with pytest.raises(ValidationError):
        LoopbackServer(coordinator, bind_address="0.0.0.0")
    loopback = LoopbackServer(coordinator)
    assert loopback.base_url.startswith("http://127.0.0.1:")
    loopback.close()
