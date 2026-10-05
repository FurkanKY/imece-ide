from __future__ import annotations

import socket
import threading

import pytest

from collab_runtime.commands import LoopbackTaskClient, TaskCommandError, TaskStatusReceipt
from collab_runtime.coordinator import Coordinator
from collab_runtime.errors import ValidationError
from collab_runtime.models import build_initial_state, build_task
from collab_runtime.store import GitStore
from collab_runtime.transport import LoopbackServer


OWNER, ALICE, BOB = "O" * 40, "A" * 40, "B" * 40
SHA = "a" * 40


@pytest.fixture
def live(tmp_path):
    hub_path = GitStore.create_bare(tmp_path / "hub.git", what="hub")
    store_path = GitStore.create_bare(tmp_path / "store.git", what="store")
    store = GitStore(store=store_path, remote=str(hub_path))
    rev = store.init_session(build_initial_state(
        session_id="session", target_version="v1", base_commit=SHA,
    ))
    rev = store.upsert_task(build_task(
        task_id="task", owner="alice", goal="goal", scopes=["src/"],
        status="queued", context_revision=rev,
    ), expected_revision=rev)
    coordinator = Coordinator(
        store, session_id="session", owner_id="owner",
        member_credentials={"owner": OWNER, "alice": ALICE, "bob": BOB},
    )
    server = LoopbackServer(coordinator).start()
    try:
        yield server, store
    finally:
        server.close()


def test_explicit_snapshot_then_owned_update_and_owner_update(live):
    server, _store = live
    alice = LoopbackTaskClient(server.base_url, credential=ALICE)
    bob = LoopbackTaskClient(server.base_url, credential=BOB)
    owner = LoopbackTaskClient(server.base_url, credential=OWNER)
    from collab_runtime.client import LoopbackSnapshotClient

    before = LoopbackSnapshotClient(server.base_url, credential=ALICE).snapshot()
    receipt = alice.update_task_status(
        task_id="task", status="running", expected_revision=before.revision,
    )
    assert receipt == TaskStatusReceipt(receipt.revision, "task", "running")
    assert receipt.revision != before.revision
    after = LoopbackSnapshotClient(server.base_url, credential=ALICE).snapshot()
    assert after.state.tasks["task"].status == "running"
    assert after.state.tasks["task"].owner == "alice"
    with pytest.raises(TaskCommandError) as denied:
        bob.update_task_status(task_id="task", status="done", expected_revision=after.revision)
    assert denied.value.code == "access_denied"
    assert denied.value.outcome_uncertain is False
    owner.update_task_status(task_id="task", status="done", expected_revision=after.revision)
    final = LoopbackSnapshotClient(server.base_url, credential=ALICE).snapshot()
    assert final.state.tasks["task"].status == "done"
    assert final.state.tasks["task"].owner == "alice"


def test_stale_revision_is_known_rejection_and_does_not_overwrite(live):
    server, _store = live
    from collab_runtime.client import LoopbackSnapshotClient

    client = LoopbackTaskClient(server.base_url, credential=ALICE)
    snapshots = LoopbackSnapshotClient(server.base_url, credential=ALICE)
    stale = snapshots.snapshot().revision
    current = snapshots.snapshot()
    client.update_task_status(task_id="task", status="running", expected_revision=current.revision)
    with pytest.raises(TaskCommandError) as error:
        client.update_task_status(task_id="task", status="done", expected_revision=stale)
    assert (error.value.code, error.value.outcome_uncertain) == ("stale_revision", False)
    assert snapshots.snapshot().state.tasks["task"].status == "running"


@pytest.mark.parametrize("url", [
    "http://localhost:1234", "https://127.0.0.1:1234", "http://127.0.0.1:0",
    "http://127.0.0.1:65536", "http://127.0.0.1:1234/", "http://127.000.0.1:1234",
])
def test_constructor_rejects_nonliteral_or_unusual_endpoints(url):
    with pytest.raises(ValidationError):
        LoopbackTaskClient(url, credential=ALICE)


def test_local_invalid_arguments_are_rejected_before_network(monkeypatch):
    def no_socket(*_args, **_kwargs):
        raise AssertionError("local validation attempted network access")

    monkeypatch.setattr(socket, "socket", no_socket)
    client = LoopbackTaskClient("http://127.0.0.1:1234", credential=ALICE)
    for args in (
        {"task_id": "../x", "status": "done", "expected_revision": SHA},
        {"task_id": "task", "status": "DONE", "expected_revision": SHA},
        {"task_id": "task", "status": "done", "expected_revision": "A" * 40},
        {"task_id": "task", "status": True, "expected_revision": SHA},
    ):
        with pytest.raises(ValidationError):
            client.update_task_status(**args)


def test_connect_failure_has_known_noncommit_outcome(monkeypatch):
    class Refused:
        def __init__(self, *_args): pass
        def settimeout(self, _value): pass
        def connect(self, _address): raise ConnectionRefusedError("secret peer text")
        def close(self): pass

    monkeypatch.setattr(socket, "socket", Refused)
    client = LoopbackTaskClient("http://127.0.0.1:1234", credential=ALICE)
    with pytest.raises(TaskCommandError) as error:
        client.update_task_status(task_id="task", status="done", expected_revision=SHA)
    assert (error.value.code, error.value.outcome_uncertain) == ("connection_error", False)
    assert ALICE not in str(error.value) + repr(error.value)


class _OneShotPeer:
    def __init__(self, response: bytes):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.response = response
        self.count = 0
        self.thread = threading.Thread(target=self._serve)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.listener.accept()
            with conn:
                conn.settimeout(2)
                data = bytearray()
                while b"\r\n\r\n" not in data:
                    data.extend(conn.recv(4096))
                head, _, body = data.partition(b"\r\n\r\n")
                length = int(next(line.split(b":", 1)[1] for line in head.split(b"\r\n")
                                  if line.lower().startswith(b"content-length:")))
                while len(body) < length:
                    body += conn.recv(length - len(body))
                self.count += 1
                if self.response:
                    conn.sendall(self.response)
        finally:
            self.listener.close()

    def close(self):
        self.thread.join(3)


@pytest.mark.parametrize("response", [
    b"HTTP/1.0 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: 55\r\n\r\n{\"revision\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}",
    b"HTTP/1.0 200 OK\r\nConnection: close\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
    b"HTTP/1.0 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
    b"HTTP/1.0 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
])
def test_peer_protocol_failures_are_uncertain_and_single_attempt(response):
    peer = _OneShotPeer(response)
    try:
        client = LoopbackTaskClient(f"http://127.0.0.1:{peer.port}", credential=ALICE)
        if b"Content-Length: 55" in response:
            receipt = client.update_task_status(task_id="task", status="done", expected_revision=SHA)
            assert receipt.revision == SHA
        else:
            with pytest.raises(TaskCommandError) as error:
                client.update_task_status(task_id="task", status="done", expected_revision=SHA)
            assert error.value.code in {"protocol_error", "outcome_unknown"}
            assert error.value.outcome_uncertain is True
    finally:
        peer.close()
    assert peer.count == 1
