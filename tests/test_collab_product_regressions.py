"""Owner-side shared product board: CAS writes, honest reads, proposal provenance.

Real temporary Git repositories, a real loopback listener on 127.0.0.1 and real
proposal capture/publication are used everywhere. Fakes appear only where a
blocked metadata fetch, an unreadable proposal hub or an unexpected coordinator
failure cannot otherwise be produced deterministically. Every listener is closed
in a fixture ``finally`` block; no process-wide state (webhost.state singletons,
the monotonic clock, ``collab_runtime.proposals`` helpers) survives a test.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from collab_runtime import proposals as proposals_module
from collab_runtime.context import SharedSnapshot
from collab_runtime.coordinator import Snapshot
from collab_runtime.errors import AccessDeniedError, StaleRevisionError, ValidationError
from collab_runtime.host import CollaborationHost
from collab_runtime.models import build_context
from collab_runtime.owner import OwnerError, OwnerSessionManager
from collab_runtime.proposals import capture_proposal, list_proposals, publish_proposal
from collab_runtime.store import GitStore
from webhost import state
from webhost.api import owner as owner_api  # registers the owner RPC handlers
from webhost.bridge import BridgeError, HostBridge


SESSION = "demo"
MEMBERS = ["alice", "bob", "carol", "dave"]
REVISION = "a" * 40


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root: Path, message: str = "change") -> str:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", message],
                   check=True)
    return git(root, "rev-parse", "HEAD")


def seeded_project(base: Path, name: str = "project") -> tuple[Path, str]:
    """A real repository with one committed in-scope file."""
    root = base / name
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('base')\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git(root, "add", "-A")
    subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
    return root, git(root, "rev-parse", "HEAD")


def source_manifest(root: Path) -> dict[str, object]:
    """Everything a shared-metadata binding must not disturb."""
    files: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if ".git" in Path(relative).parts:
            continue
        files[relative] = None if path.is_dir() else path.read_bytes().hex()
    return {
        "head": git(root, "rev-parse", "HEAD"),
        "index": git(root, "ls-files", "-s"),
        "status": git(root, "status", "--porcelain=v1"),
        "config": git(root, "config", "--local", "--list"),
        "refs": git(root, "show-ref"),
        "diff": git(root, "diff", "HEAD", "--stat"),
        "files": files,
    }


def plan(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": SESSION, "target_version": "v1", "goal": "Coordinate safely",
        "owner_id": "alice", "member_ids": list(MEMBERS),
        "tasks": [{"id": "task-a", "owner": "bob", "goal": "Implement safely", "scopes": ["src/"]},
                  {"id": "task-b", "owner": "carol", "goal": "Review safely", "scopes": ["src/"]}],
    }
    payload.update(overrides)
    return payload


def wait_for(predicate, message: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.005)
    raise AssertionError(message)


def cas(board: dict[str, object], **overrides: object) -> dict[str, object]:
    args: dict[str, object] = {"expected_revision": board["revision"],
                               "expected_epoch": board["epoch"],
                               "expected_session_id": board["sessionId"]}
    args.update(overrides)
    return args


def set_status(world: SimpleNamespace, status: str, *, task_id: str = "task-a",
               member_id: str | None = None) -> dict[str, object]:
    board = world.manager.product_snapshot(world.root)
    return world.manager.update_product_task(
        world.root, task_id=task_id, status=status, member_id=member_id, **cas(board))


def store_of(manager: OwnerSessionManager) -> GitStore:
    """A private store reader that does not touch manager internals."""
    status = manager.status()
    return GitStore(store=status["storePath"], remote=status["hubPath"])


@pytest.fixture
def configured(tmp_path):
    root, head = seeded_project(tmp_path)
    private = tmp_path / "private"
    manager = OwnerSessionManager(private)
    preview = manager.preview_create(root, **plan())
    manager.create(preview["previewId"], root)
    world = SimpleNamespace(root=root, head=head, private=private, manager=manager,
                            store=manager._config["store"])
    try:
        yield world
    finally:
        try:
            manager.stop()
        except OwnerError:  # pragma: no cover - defensive only
            pass


@pytest.fixture
def running(configured):
    configured.manager.start(configured.root)
    try:
        yield configured
    finally:
        try:
            configured.manager.stop()
        except OwnerError:  # pragma: no cover - defensive only
            pass


class _Generation:
    """Project generation for the bridge staleness contract.

    ``advance`` is what every call AFTER the handler captured the generation
    reports, so one RPC deterministically straddles a project switch.
    """

    def __init__(self, value: int = 1, advance: int = 0) -> None:
        self.value = value
        self.advance = advance
        self.calls = 0

    def reset(self) -> None:
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        if self.advance and self.calls > 1:
            return self.advance
        return self.value


@pytest.fixture
def bridge_world(running, monkeypatch):
    generation = _Generation()
    monkeypatch.setattr(state, "get_project", lambda: SimpleNamespace(root=str(running.root)))
    monkeypatch.setattr(state, "project_generation", generation)
    monkeypatch.setattr(state, "get_owner_manager", lambda: running.manager)
    monkeypatch.setattr(state, "peek_owner_manager", lambda: running.manager)
    running.generation = generation
    return running


def rpc(method: str, params: dict[str, object], timeout: float = 20.0) -> dict[str, object]:
    app = QCoreApplication.instance() or QCoreApplication([])
    bridge, replies = HostBridge(), []
    bridge.reply.connect(lambda raw: replies.append(json.loads(raw)))
    bridge.call(json.dumps({"id": 7, "method": method, "params": params}))
    deadline = time.monotonic() + timeout
    while not replies and time.monotonic() < deadline:
        app.processEvents()
        threading.Event().wait(0.003)
    assert replies, f"no reply for {method}"
    return replies[0]


# --------------------------------------------------------------------------
# 1-3. compare-and-swap writes and honest snapshots
# --------------------------------------------------------------------------


def test_stale_revision_is_refused_and_never_overwrites_the_published_state(running):
    world = running
    board = world.manager.product_snapshot(world.root)
    moved = world.manager.update_product_context(
        world.root, context={"goal": "second goal", "decisions": [], "interfaces": {}},
        **cas(board))
    assert moved["revision"] != board["revision"]
    head = world.store.fetch_state()[0]
    assert head == moved["revision"]

    with pytest.raises(OwnerError) as failure:
        world.manager.update_product_context(
            world.root, context={"goal": "clobber", "decisions": [], "interfaces": {}},
            **cas(board))
    assert failure.value.code == "product_stale_revision"
    with pytest.raises(OwnerError) as failure:
        world.manager.update_product_task(
            world.root, task_id="task-a", status="done", **cas(board))
    assert failure.value.code == "product_stale_revision"

    after = world.manager.product_snapshot(world.root)
    assert after["revision"] == head
    assert after["context"]["goal"] == "second goal" and after["context"]["decisions"] == []
    assert {task["id"]: task["status"] for task in after["tasks"]} == {"task-a": "queued", "task-b": "queued"}
    assert world.store.fetch_state()[0] == head


def test_snapshot_is_complete_deeply_detached_and_refreshed_after_coordinator_changes(running):
    world = running
    board = world.manager.product_snapshot(world.root)
    assert set(board) == {"projectRoot", "sessionId", "baseCommit", "targetVersion", "revision",
                          "contextHash", "context", "ownerId", "memberIds", "tasks", "overlaps",
                          "waitingTaskIds", "epoch", "state"}
    assert board["state"] == "running" and board["epoch"] >= 1
    assert board["sessionId"] == SESSION and board["baseCommit"] == world.head
    assert board["projectRoot"] == str(world.root) and board["ownerId"] == "alice"
    assert board["memberIds"] == MEMBERS and board["waitingTaskIds"] == []
    assert board["overlaps"] == [{"tasks": ["task-a", "task-b"], "shared": ["src/"]}]
    assert board["contextHash"] == world.store.fetch_state()[1].context_hash
    assert set(board["tasks"][0]) == {"id", "owner", "goal", "scopes", "status", "contextRevision"}
    assert [task["id"] for task in board["tasks"]] == ["task-a", "task-b"]

    # Detached in depth: no nested value may alias the manager's own view.
    board["context"]["decisions"].append("local")
    board["context"]["interfaces"]["api"] = "local"
    board["tasks"][0]["scopes"].append("docs/")
    board["tasks"][0]["status"] = "done"
    board["memberIds"].append("mallory")
    board["overlaps"][0]["shared"].append("docs/")
    board["waitingTaskIds"].append("task-a")

    fresh = world.manager.product_snapshot(world.root)
    assert fresh["context"] == {"goal": "Coordinate safely", "decisions": [], "interfaces": {}}
    assert fresh["memberIds"] == MEMBERS and fresh["tasks"][0]["scopes"] == ["src/"]
    assert fresh["tasks"][0]["status"] == "queued" and fresh["waitingTaskIds"] == []
    assert fresh["overlaps"] == [{"tasks": ["task-a", "task-b"], "shared": ["src/"]}]
    assert fresh["revision"] == board["revision"]

    changed = world.manager.update_product_context(
        world.root, context={"goal": "new goal", "decisions": ["d1"], "interfaces": {"api": "v1"}},
        **cas(fresh))
    refreshed = world.manager.product_snapshot(world.root)
    assert refreshed["revision"] == changed["revision"]
    assert refreshed["context"]["goal"] == "new goal" and refreshed["context"]["decisions"] == ["d1"]
    assert refreshed["contextHash"] != fresh["contextHash"]

    waiting = set_status(world, "waiting")
    board_after = world.manager.product_snapshot(world.root)
    assert board_after["waitingTaskIds"] == ["task-a"]
    assert board_after["revision"] == waiting["revision"]
    assert board_after["overlaps"] == [{"tasks": ["task-a", "task-b"], "shared": ["src/"]}]


def test_product_writes_touch_metadata_only_and_preserve_the_source_checkout(running):
    world = running
    (world.root / "notes.txt").write_text("scratch\n", encoding="utf-8")
    before = source_manifest(world.root)
    assert before["status"]  # dirty worktree is part of the fixture

    board = world.manager.product_snapshot(world.root)
    world.manager.update_product_context(
        world.root, context={"goal": "metadata only", "decisions": [], "interfaces": {}},
        **cas(board))
    set_status(world, "running")
    set_status(world, "done", task_id="task-b")

    assert source_manifest(world.root) == before
    assert (world.root / ".imece").exists() is False
    status = world.manager.status()
    assert status["storePath"].startswith(str(world.private))
    assert status["hubPath"].startswith(str(world.private))
    assert status["state"] == "running"


# --------------------------------------------------------------------------
# 4-6. authorization and fixed refusal codes
# --------------------------------------------------------------------------


def test_owner_and_assignee_may_write_while_other_members_are_denied(running):
    world = running
    assignee = set_status(world, "running", member_id="bob")
    assert assignee["action"] == "task" and assignee["taskId"] == "task-a"
    assert assignee["status"] == "running" and assignee["sessionId"] == SESSION
    assert assignee["revision"] == world.store.fetch_state()[0]

    own = set_status(world, "running")  # the owner's own credential by default
    assert own["revision"] != assignee["revision"]
    named_owner = set_status(world, "waiting", member_id="alice")
    assert named_owner["revision"] != own["revision"]
    other_owner_task = set_status(world, "running", task_id="task-b", member_id="carol")
    assert other_owner_task["revision"] != named_owner["revision"]

    board = world.manager.product_snapshot(world.root)
    for member_id in ("carol", "dave"):
        with pytest.raises(OwnerError) as failure:
            world.manager.update_product_task(
                world.root, task_id="task-a", status="done", member_id=member_id, **cas(board))
        assert failure.value.code == "product_access_denied"
    assert world.manager.product_snapshot(world.root)["revision"] == board["revision"]


@pytest.mark.parametrize("overrides,code", [
    ({"expected_epoch": "1"}, "epoch_mismatch"),
    ({"expected_epoch": True}, "epoch_mismatch"),
    ({"expected_epoch": 99}, "epoch_mismatch"),
    ({"expected_session_id": "other"}, "session_identity_mismatch"),
    ({"expected_session_id": ""}, "session_identity_mismatch"),
    ({"expected_revision": "f" * 40}, "product_stale_revision"),
    ({"expected_revision": "not-a-revision"}, "product_stale_revision"),
    ({"task_id": "ghost"}, "product_invalid"),
    ({"status": "archived"}, "product_invalid"),
    ({"status": None}, "product_invalid"),
    ({"member_id": "mallory"}, "invalid_members"),
])
def test_identity_epoch_and_scope_refusals_have_fixed_codes(running, overrides, code):
    world = running
    board = world.manager.product_snapshot(world.root)
    with pytest.raises(OwnerError) as failure:
        world.manager.update_product_task(
            world.root, task_id=overrides.pop("task_id", "task-a"),
            status=overrides.pop("status", "running"), **cas(board, **overrides))
    assert failure.value.code == code
    assert world.manager.product_snapshot(world.root)["revision"] == board["revision"]
    assert world.manager.status()["state"] == "running"


def test_reads_and_writes_refuse_a_foreign_or_unusable_project_root(running, tmp_path):
    world = running
    other, _ = seeded_project(tmp_path / "side", "other")
    plain = tmp_path / "plain"
    plain.mkdir()
    board = world.manager.product_snapshot(world.root)
    context = {"goal": "elsewhere", "decisions": [], "interfaces": {}}

    for foreign, code in ((other, "wrong_project"), (plain, "invalid_project"),
                          (tmp_path / "absent", "invalid_project")):
        with pytest.raises(OwnerError) as failure:
            world.manager.product_snapshot(foreign)
        assert failure.value.code == code
        with pytest.raises(OwnerError) as failure:
            world.manager.update_product_context(foreign, context=context, **cas(board))
        assert failure.value.code == code
        with pytest.raises(OwnerError) as failure:
            world.manager.update_product_task(
                foreign, task_id="task-a", status="running", **cas(board))
        assert failure.value.code == code
        with pytest.raises(OwnerError) as failure:
            world.manager.product_proposals(foreign, expected_session_id=SESSION)
        assert failure.value.code == code

    assert world.store.fetch_state()[0] == board["revision"]


# --------------------------------------------------------------------------
# 7-10. lifecycle honesty under blocked reads and drains
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stop_first", [False, True], ids=["configured", "stopped"])
def test_reads_survive_a_paused_lifecycle_but_mutations_are_refused(configured, stop_first):
    world = configured
    if stop_first:
        world.manager.start(world.root)
        world.manager.stop()
    lifecycle = "stopped" if stop_first else "configured"

    board = world.manager.product_snapshot(world.root)
    assert board["state"] == lifecycle and board["revision"] == world.store.fetch_state()[0]
    assert board["baseCommit"] == world.head and [task["id"] for task in board["tasks"]] == ["task-a", "task-b"]
    listing = world.manager.product_proposals(world.root, expected_session_id=SESSION)
    assert listing["sessionId"] == SESSION and listing["proposals"] == []
    assert listing["revision"] == board["revision"]

    for call, kwargs in (
        (world.manager.update_product_context, {"context": {"goal": "x", "decisions": [], "interfaces": {}}}),
        (world.manager.update_product_task, {"task_id": "task-a", "status": "running"}),
    ):
        with pytest.raises(OwnerError) as failure:
            call(world.root, **cas(board), **kwargs)
        assert failure.value.code == "not_running"
    assert world.store.fetch_state()[0] == board["revision"]


def _blocking_store(store: GitStore, monkeypatch, *, timeout: float = 10.0):
    """Gate the real metadata fetch so a race window is deterministic."""
    entered, release = threading.Event(), threading.Event()
    original = store.fetch_state

    def blocked():
        entered.set()
        if not release.wait(timeout):
            raise AssertionError("the blocked metadata fetch was never released")
        return original()

    monkeypatch.setattr(store, "fetch_state", blocked)
    return entered, release


def test_status_stays_fast_and_secret_free_while_a_product_fetch_blocks(running, monkeypatch):
    world = running
    entered, release = _blocking_store(world.store, monkeypatch)
    outcome: dict[str, object] = {}

    def read() -> None:
        try:
            outcome["board"] = world.manager.product_snapshot(world.root)
        except BaseException as error:  # noqa: BLE001 - asserted below
            outcome["error"] = error

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    assert entered.wait(5), "the product fetch never reached the metadata store"

    began = time.monotonic()
    status = world.manager.status()
    elapsed = time.monotonic() - began
    assert elapsed < 2.0 and worker.is_alive()
    assert status["state"] == "running" and status["endpoint"].startswith("http://127.0.0.1:")
    assert status["tasks"] and "credential" not in json.dumps(status)
    assert sorted(world.manager._credentials) == MEMBERS
    for credential in world.manager._credentials.values():
        assert credential not in json.dumps(status)

    release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert "error" not in outcome, outcome.get("error")
    assert outcome["board"]["state"] == "running"
    assert outcome["board"]["revision"] == world.store.fetch_state()[0]


def test_a_concurrent_stop_never_discards_a_live_read_and_still_drains_the_listener(running, monkeypatch):
    world = running
    entered, release = _blocking_store(world.store, monkeypatch)
    outcome: dict[str, object] = {}
    drained: dict[str, object] = {}

    def read() -> None:
        try:
            outcome["board"] = world.manager.product_snapshot(world.root)
        except BaseException as error:  # noqa: BLE001 - asserted below
            outcome["error"] = error

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    assert entered.wait(5), "the product fetch never reached the metadata store"

    def drain() -> None:
        try:
            drained["status"] = world.manager.stop()
        except BaseException as error:  # noqa: BLE001 - asserted below
            drained["error"] = error

    stopper = threading.Thread(target=drain, daemon=True)
    stopper.start()
    threading.Event().wait(0.2)  # let the drain queue behind the in-flight read
    assert stopper.is_alive(), "a drain must not tear down a live read's runtime"
    live = world.manager.status()
    assert live["state"] == "running" and live["endpoint"] is not None
    assert sorted(world.manager._credentials) == MEMBERS

    release.set()
    worker.join(10)
    stopper.join(10)
    assert not worker.is_alive() and not stopper.is_alive()

    # The read is serialized ahead of the drain: it reports the lifecycle it
    # captured, and the drain that followed still owns and closes the listener.
    assert "error" not in outcome, outcome.get("error")
    assert outcome["board"]["state"] == "running"
    assert outcome["board"]["revision"] == world.store.fetch_state()[0]
    assert "error" not in drained and drained["status"]["state"] == "stopped"
    status = world.manager.status()
    assert status["state"] == "stopped" and status["endpoint"] is None
    assert world.manager._credentials == {} and world.manager._server is None
    assert world.manager._coordinator is None
    assert world.manager.product_snapshot(world.root)["state"] == "stopped"


def test_a_committed_receipt_survives_a_failing_post_write_read(running):
    world = running
    board = world.manager.product_snapshot(world.root)
    receipt = world.manager.update_product_task(
        world.root, task_id="task-a", status="running", **cas(board))
    assert receipt["revision"] != board["revision"]
    reader = store_of(world.manager)
    assert reader.fetch_state()[0] == receipt["revision"]

    commit(world.root, "the source moved on")
    with pytest.raises(OwnerError) as failure:
        world.manager.product_snapshot(world.root)
    assert failure.value.code == "source_head_mismatch"

    assert reader.fetch_state()[0] == receipt["revision"]
    assert reader.fetch_state()[1].tasks["task-a"].status == "running"
    assert world.manager.status()["state"] == "running"


# --------------------------------------------------------------------------
# 11-12. local handoff follows the refreshed assignment
# --------------------------------------------------------------------------


def recording_host(cursor_root: Path, snapshot: Snapshot, recorder: dict[str, object]) -> CollaborationHost:
    class FakeClient:
        def __init__(self, endpoint: str, *, credential: str) -> None:
            recorder["credentials"] = list(recorder.get("credentials", [])) + [(endpoint, credential)]

        def snapshot(self) -> Snapshot:
            recorder["snapshots"] = recorder.get("snapshots", 0) + 1
            return snapshot

    return CollaborationHost(cursor_root, head_reader=lambda _root: snapshot.state.base_commit,
                             client_factory=FakeClient)


def test_a_finished_task_cannot_be_locally_previewed(running, tmp_path):
    world = running
    revision, live = world.store.fetch_state()
    recorder: dict[str, object] = {}
    host = recording_host(tmp_path / "cursors", Snapshot(revision, live), recorder)

    accepted = world.manager.preview_local_collaboration(
        host, world.root, member_id="bob", task_id="task-a")
    assert accepted["taskId"] == "task-a" and accepted["epoch"] == world.manager.status()["epoch"]
    assert accepted["preview"]["task"]["status"] == "queued"
    assert recorder["snapshots"] == 1 and len(host._candidates) == 1
    assert host._approved == {}
    revealed = world.manager.reveal_member_once("bob")
    assert recorder["credentials"] == [(accepted["endpoint"], revealed["credential"])]

    set_status(world, "done", member_id="bob")
    board = world.manager.product_snapshot(world.root)
    assert board["waitingTaskIds"] == [] and "task-a" in {
        task["id"] for task in board["tasks"] if task["status"] == "done"}

    with pytest.raises(OwnerError) as failure:
        world.manager.preview_local_collaboration(host, world.root, member_id="bob", task_id="task-a")
    assert failure.value.code == "invalid_task_assignment"
    assert recorder["snapshots"] == 1 and len(host._candidates) == 1
    assert host._approved == {}


# --------------------------------------------------------------------------
# 13-14. proposal provenance through the owner product surface
# --------------------------------------------------------------------------


def _publish_proposal(world: SimpleNamespace, proposal_id: str = "prop-1") -> tuple[object, str, str]:
    (world.root / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    revision, live = world.store.fetch_state()
    binding = SharedSnapshot(revision=revision, context_hash=live.context_hash,
                             state=live, task_id="task-a")
    proposal = capture_proposal(world.store, world.root, proposal_id, "task-a",
                                ["src/app.py"], revision, binding=binding)
    _child, root_commit = publish_proposal(world.store, proposal, expected_revision=revision)
    return proposal, revision, root_commit


def test_proposals_report_real_capture_and_publish_provenance(running):
    world = running
    proposal, capture_revision, root_commit = _publish_proposal(world)
    hub_ref = git(world.private.parent, "ls-remote", world.manager.status()["hubPath"],
                  f"refs/heads/imece-proposals/{proposal.proposal_id}")
    assert hub_ref.split("\t")[0] == root_commit

    listing = world.manager.product_proposals(world.root, expected_session_id=SESSION)
    assert set(listing) == {"sessionId", "baseCommit", "revision", "contextHash", "epoch", "proposals"}
    assert listing["sessionId"] == SESSION and listing["baseCommit"] == world.head
    assert listing["revision"] == world.store.fetch_state()[0]
    assert len(listing["proposals"]) == 1
    receipt = listing["proposals"][0]
    assert receipt["proposalId"] == proposal.proposal_id
    assert receipt["taskId"] == "task-a" and receipt["owner"] == "bob"
    assert receipt["contextRevision"] == capture_revision == proposal.context_revision
    assert receipt["contextHash"] == proposal.context_hash == listing["contextHash"]
    assert receipt["proposalRevision"] == root_commit
    assert receipt["baseCommit"] == world.head and receipt["sessionId"] == SESSION
    assert receipt["fileCount"] == len(proposal.files) == 1
    assert receipt["staleContext"] is False
    blob = json.dumps(listing)
    assert "after_base64" not in blob and "print('changed')" not in blob

    board = world.manager.product_snapshot(world.root)
    world.manager.update_product_context(
        world.root, context={"goal": "moved on", "decisions": [], "interfaces": {}},
        **cas(board))
    after = world.manager.product_proposals(world.root, expected_session_id=SESSION)
    assert after["contextHash"] != listing["contextHash"]
    assert len(after["proposals"]) == 1
    stale = after["proposals"][0]
    assert stale["staleContext"] is True and stale["contextRevision"] == capture_revision
    assert stale["contextHash"] == proposal.context_hash
    assert stale["proposalRevision"] == root_commit


@pytest.mark.parametrize("mode", ["shift", "failure"], ids=["session-moved", "hub-unreadable"])
def test_proposal_listing_refuses_a_moving_session_instead_of_mislabeling(running, monkeypatch, mode):
    world = running
    _publish_proposal(world)

    def shifting(inner):
        receipts = list_proposals(inner)
        revision, live = inner.fetch_state()
        inner.publish(live.with_context(build_context(goal=live.context.goal,
                                                       decisions=["moved"], interfaces={})),
                      expected_revision=revision)
        return receipts

    def unreadable(_inner):
        raise OSError("the collaboration hub is unreachable")

    monkeypatch.setattr(proposals_module, "list_proposals",
                        shifting if mode == "shift" else unreadable)
    with pytest.raises(OwnerError) as failure:
        world.manager.product_proposals(world.root, expected_session_id=SESSION)
    assert failure.value.code == ("product_stale" if mode == "shift" else "proposals_read_failed")
    assert "Traceback" not in str(failure.value) and "unreachable" not in str(failure.value)


def test_proposal_listing_refuses_foreign_sessions_and_leaks_nothing(running):
    world = running
    for session_id in ("other", "", 7, "demo-2", None):
        with pytest.raises(OwnerError) as failure:
            world.manager.product_proposals(world.root, expected_session_id=session_id)
        assert failure.value.code == "session_identity_mismatch"

    listing = world.manager.product_proposals(world.root, expected_session_id=SESSION)
    assert listing["proposals"] == [] and listing["epoch"] == world.manager.status()["epoch"]
    blob = json.dumps(listing)
    for credential in world.manager._credentials.values():
        assert credential not in blob
        assert hashlib.sha256(credential.encode("ascii")).hexdigest() not in blob


# --------------------------------------------------------------------------
# 15-16. failure mapping and credential hygiene
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raised,code", [
    (RuntimeError("unexpected"), "product_write_rejected"),
    (OSError("git died"), "product_write_rejected"),
    (AccessDeniedError("denied"), "product_access_denied"),
    (ValidationError("invalid"), "product_invalid"),
    (StaleRevisionError("stale"), "product_stale_revision"),
])
def test_unexpected_and_domain_failures_keep_distinct_fixed_codes(running, monkeypatch, raised, code):
    world = running
    board = world.manager.product_snapshot(world.root)

    def boom(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(world.manager._coordinator, "update_task_status", boom)
    with pytest.raises(OwnerError) as failure:
        world.manager.update_product_task(world.root, task_id="task-a", status="running", **cas(board))
    assert failure.value.code == code
    monkeypatch.setattr(world.manager._coordinator, "update_context", boom)
    with pytest.raises(OwnerError) as failure:
        world.manager.update_product_context(
            world.root, context={"goal": "g", "decisions": [], "interfaces": {}}, **cas(board))
    assert failure.value.code == code
    assert world.store.fetch_state()[0] == board["revision"]


def test_board_receipt_and_errors_never_carry_credentials_or_their_hashes(running):
    world = running
    board = world.manager.product_snapshot(world.root)
    receipt = world.manager.update_product_task(
        world.root, task_id="task-a", status="running", **cas(board))
    listing = world.manager.product_proposals(world.root, expected_session_id=SESSION)
    payloads = [json.dumps(item) for item in (board, receipt, listing, world.manager.status())]
    assert all("credential" not in blob for blob in payloads)
    assert sorted(world.manager._credentials) == MEMBERS

    with pytest.raises(OwnerError) as failure:
        world.manager.update_product_task(world.root, task_id="task-a", status="running", **cas(board))
    assert failure.value.code == "product_stale_revision"
    leaked = str(failure.value) + repr(failure.value) + json.dumps(failure.value.receipt)
    for credential in world.manager._credentials.values():
        digest = hashlib.sha256(credential.encode("ascii")).hexdigest()
        for blob in payloads + [leaked]:
            assert credential not in blob, "a raw member credential escaped the product surface"
            assert digest not in blob, "a credential hash escaped the product surface"


# --------------------------------------------------------------------------
# 17-19. bridge parameter and generation contracts
# --------------------------------------------------------------------------


BRIDGE_HANDLERS = {
    "collab.owner.snapshot": owner_api._product_snapshot,
    "collab.owner.proposals": owner_api._product_proposals,
    "collab.owner.updateContext": owner_api._product_context,
    "collab.owner.updateTaskStatus": owner_api._product_task,
}
CONTEXT = {"goal": "changed", "decisions": [], "interfaces": {}}
WRITE_BASE = {"confirm": True, "expectedRevision": REVISION, "expectedEpoch": 1,
              "expectedSessionId": SESSION}


@pytest.mark.parametrize("method,params", [
    ("collab.owner.snapshot", {"unexpected": 1}),
    ("collab.owner.snapshot", {"expectedRevision": REVISION}),
    ("collab.owner.proposals", {"unexpected": 1}),
    ("collab.owner.proposals", {}),
    ("collab.owner.proposals", {"expectedSessionId": "../escape"}),
    ("collab.owner.proposals", {"expectedSessionId": "s" * 129}),
    ("collab.owner.updateContext", WRITE_BASE | {"unexpected": 1}),
    ("collab.owner.updateContext", WRITE_BASE | {"context": CONTEXT, "note": "no"}),
    ("collab.owner.updateContext", WRITE_BASE | {"expectedRevision": "a" * 41}),
    ("collab.owner.updateContext", WRITE_BASE | {"expectedRevision": "z" * 40}),
    ("collab.owner.updateContext", WRITE_BASE | {"expectedRevision": 1}),
    ("collab.owner.updateContext", WRITE_BASE | {"expectedEpoch": True}),
    ("collab.owner.updateContext", WRITE_BASE | {"expectedEpoch": -1}),
    ("collab.owner.updateContext", WRITE_BASE | {"expectedEpoch": "1"}),
    ("collab.owner.updateContext", WRITE_BASE | {"expectedSessionId": "../escape"}),
    ("collab.owner.updateContext", WRITE_BASE | {"expectedSessionId": "s" * 129}),
    ("collab.owner.updateContext", WRITE_BASE | {"context": "not-a-dict"}),
    ("collab.owner.updateContext", WRITE_BASE | {"context": {"goal": "g"}}),
    ("collab.owner.updateContext", WRITE_BASE | {"context": CONTEXT | {"extra": 1}}),
    ("collab.owner.updateContext", WRITE_BASE | {"context": {"goal": "g", "decisions": ["d" * 3000],
                                                            "interfaces": {}}}),
    ("collab.owner.updateTaskStatus", WRITE_BASE | {"taskId": "../escape", "status": "running"}),
    ("collab.owner.updateTaskStatus", WRITE_BASE | {"taskId": "t" * 129, "status": "running"}),
    ("collab.owner.updateTaskStatus", WRITE_BASE | {"taskId": "task-a", "status": "archived"}),
    ("collab.owner.updateTaskStatus", WRITE_BASE | {"taskId": "task-a", "status": True}),
    ("collab.owner.updateTaskStatus", WRITE_BASE | {"taskId": "task-a"}),
    ("collab.owner.updateTaskStatus", WRITE_BASE | {"taskId": "task-a", "status": "running",
                                                    "memberId": "dave"}),
])
def test_product_bridge_refuses_unknown_oversized_and_mistyped_params(method, params):
    with pytest.raises(BridgeError) as failure:
        BRIDGE_HANDLERS[method](params, None)
    assert failure.value.code == "owner_invalid"
    assert "Traceback" not in str(failure.value)


@pytest.mark.parametrize("method,params", [
    ("collab.owner.snapshot", {}),
    ("collab.owner.proposals", {"expectedSessionId": SESSION}),
    ("collab.owner.updateContext", WRITE_BASE | {"context": CONTEXT}),
    ("collab.owner.updateTaskStatus", WRITE_BASE | {"taskId": "task-a", "status": "running"}),
])
def test_valid_product_params_pass_validation_and_reach_the_project_lookup(method, params):
    """Control for the matrix above: only the project lookup may refuse these."""
    with pytest.raises(BridgeError) as failure:
        BRIDGE_HANDLERS[method](params, None)
    assert failure.value.code == "no_project"


@pytest.mark.parametrize("confirm", ["true", 1, None, {}, "yes"])
@pytest.mark.parametrize("method,extra", [
    ("collab.owner.updateContext", {"context": CONTEXT}),
    ("collab.owner.updateTaskStatus", {"taskId": "task-a", "status": "running"}),
])
def test_product_bridge_requires_an_explicit_boolean_confirmation(method, extra, confirm):
    params = WRITE_BASE | extra | {"confirm": confirm}
    with pytest.raises(BridgeError) as failure:
        BRIDGE_HANDLERS[method](params, None)
    assert failure.value.code == "owner_confirmation_required"
    params.pop("confirm")
    with pytest.raises(BridgeError) as failure:
        BRIDGE_HANDLERS[method](params, None)
    assert failure.value.code == "owner_confirmation_required"


def test_reads_report_the_current_generation_while_a_write_returns_a_truthful_receipt(bridge_world):
    world = bridge_world
    generation = world.generation

    board_reply = rpc("collab.owner.snapshot", {})
    assert board_reply["ok"] is True, board_reply
    board = board_reply["result"]
    assert board["state"] == "running" and "credential" not in json.dumps(board)
    listing_reply = rpc("collab.owner.proposals", {"expectedSessionId": SESSION})
    assert listing_reply["ok"] is True and listing_reply["result"]["proposals"] == []
    task_reply = rpc("collab.owner.updateTaskStatus",
                     {"confirm": True, "expectedRevision": board["revision"], "expectedEpoch": board["epoch"],
                      "expectedSessionId": SESSION, "taskId": "task-a", "status": "running"})
    assert task_reply["ok"] is True, task_reply
    assert task_reply["result"]["revision"] == world.store.fetch_state()[0]
    assert task_reply["result"]["status"] == "running"

    # A newer project generation invalidates only what is still a READ.
    generation.advance = 2
    for method, params in (("collab.owner.snapshot", {}),
                           ("collab.owner.proposals", {"expectedSessionId": SESSION})):
        generation.reset()  # the next call is the handler's capture
        stale = rpc(method, params)
        assert stale["ok"] is False, stale
        assert stale["error"]["code"] == "owner_stale"
        assert "credential" not in json.dumps(stale)

    settled = world.manager.product_snapshot(world.root)
    generation.reset()  # the same switch, but this handler commits a write
    committed = rpc("collab.owner.updateContext",
                    {"confirm": True, "expectedRevision": settled["revision"],
                     "expectedEpoch": settled["epoch"], "expectedSessionId": SESSION,
                     "context": {"goal": "committed anyway", "decisions": ["d1"], "interfaces": {}}})
    assert committed["ok"] is True, committed
    assert committed["result"]["action"] == "context"
    assert committed["result"]["revision"] == world.store.fetch_state()[0]
    assert committed["result"]["sessionId"] == SESSION
    assert committed["result"]["revision"] != settled["revision"]

    generation.advance = 0
    final = rpc("collab.owner.snapshot", {})
    assert final["ok"] is True, final
    assert final["result"]["revision"] == committed["result"]["revision"]
    assert final["result"]["context"]["goal"] == "committed anyway"
    for credential in world.manager._credentials.values():
        assert credential not in json.dumps(final)
