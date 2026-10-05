"""Owner-only new-task creation: append-only, CAS-guarded and metadata-only.

Real temporary Git repositories, real metadata stores and a real loopback
listener are used everywhere; the fixtures and helpers are IMPORTED from
``test_collab_product_regressions`` (never copied), so every listener is closed
in their ``finally`` blocks and no process-wide state (webhost.state
singletons, the monotonic clock) survives a test. Fakes appear only where a
blocked metadata fetch, a blocked publication or an unexpected coordinator
failure cannot otherwise be produced deterministically -- and even then the real
coordinator/store boundary is wrapped in place, never replaced.

The contract pinned here: creation is owner-credential-only, member-scoped and
decided BEFORE any metadata fetch or publication; an existing task is never
overwritten, reordered or reassigned; stale CAS/epoch/session, a moved source
HEAD and a foreign root refuse with fixed codes and publish nothing; the new
task is always ``queued`` on the currently reviewed published parent revision;
the 256-task count limit and the combined 64 KiB state limit are refused before
a commit; goal/scope/identity shapes (32 scopes, traversal, absolute and drive
paths, ``.git``, globs, duplicates, unknown fields) stay bounded; assignment is
limited to declared members; the source checkout, the ``credentials`` map and
the epoch are untouched and no token, credential hash, native cursor, approval
or run ever changes; the new task shows up in the authoritative snapshot, in the
read-only history as an ``added`` change with an affected task, and in an
explicit local preview that still needs its own approval; a committed receipt is
never converted by a failing refresh; two concurrent creations accept exactly
one; a drain queued behind a gated publish still drains while status stays fast;
and the bridge refuses unknown, overridden, oversized, mistyped and unconfirmed
parameters before the project lookup while a real create RPC still reports a
truthful receipt across a generation change.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from collab_runtime.errors import AccessDeniedError
from collab_runtime.host import CollaborationHost
from collab_runtime.models import (
    MAX_JSON_BYTES,
    MAX_TASKS,
    SessionState,
    build_task,
    canonical_json_bytes,
)
from collab_runtime.owner import OwnerError
from webhost.api import owner as owner_api  # registers the owner RPC handlers
from webhost.bridge import BridgeError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_collab_product_regressions import (  # noqa: E402  (imported fixtures, not copies)
    MEMBERS,
    REVISION,
    SESSION,
    bridge_world,
    cas,
    commit,
    configured,
    rpc,
    running,
    seeded_project,
    source_manifest,
    store_of,
)


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")

_UNSET = object()
CREATE_BASE = {"confirm": True, "expectedRevision": REVISION, "expectedEpoch": 1,
               "expectedSessionId": SESSION}
CREATE_TASK = {"id": "task-c", "owner": "carol", "goal": "Second slice", "scopes": ["src/next.py"]}


# --------------------------------------------------------------------------
# helpers: thin call shapes only (all infrastructure is imported)
# --------------------------------------------------------------------------


def create_task(world, *, board=None, task_id="new-task", owner="bob",
                goal="Ship the follow-up", scopes=_UNSET, **cas_overrides):
    """One manager-level creation against the current (or a given) board."""
    if scopes is _UNSET:
        scopes = ["src/next.py"]
    elif isinstance(scopes, tuple):
        scopes = list(scopes)
    board = world.manager.product_snapshot(world.root) if board is None else board
    return world.manager.create_product_task(
        world.root, task_id=task_id, owner=owner, goal=goal, scopes=scopes,
        **cas(board, **cas_overrides))


def counted_publishes(world, monkeypatch) -> list[str]:
    """Count real publications so 'refused' can mean 'nothing was committed'."""
    calls: list[str] = []
    original = world.store.publish

    def counting(candidate, *, expected_revision):
        calls.append(expected_revision)
        return original(candidate, expected_revision=expected_revision)

    monkeypatch.setattr(world.store, "publish", counting)
    return calls


def bulk_task(index: int, revision: str, scopes: list[str]):
    return build_task(task_id=f"bulk-{index:03d}", owner="bob", goal="g", scopes=scopes,
                      status="queued", context_revision=revision)


def state_size(live: SessionState, tasks: dict) -> int:
    return len(canonical_json_bytes(SessionState(
        live.session_id, live.target_version, live.base_commit, live.context, tasks).to_dict()))


def seed_state(world, tasks: dict) -> str:
    """Publish one real bounded state that adds `tasks` to this session."""
    revision, live = world.store.fetch_state()
    seeded = SessionState(live.session_id, live.target_version, live.base_commit,
                          live.context, {**dict(live.tasks), **tasks})
    return world.store.publish(seeded, expected_revision=revision)


def board_over(world) -> list[str]:
    return sorted(item["id"] for item in world.manager.product_snapshot(world.root)["tasks"])


def bridge_board(world) -> dict:
    reply = rpc("collab.owner.snapshot", {})
    assert reply["ok"] is True, reply
    return reply["result"]


# --------------------------------------------------------------------------
# 1-3. append-only, owner-only, pre-fetch authorization
# --------------------------------------------------------------------------


@pytest.mark.parametrize("existing,attempt_owner", [
    ("task-a", "carol"), ("task-b", "alice"),
], ids=["task-a", "task-b"])
def test_creation_never_overwrites_reorders_or_reassigns_an_existing_task(
        running, monkeypatch, existing, attempt_owner):
    world = running
    board = world.manager.product_snapshot(world.root)
    before = {item["id"]: item for item in board["tasks"]}
    published = counted_publishes(world, monkeypatch)

    with pytest.raises(OwnerError) as failure:
        create_task(world, board=board, task_id=existing, owner=attempt_owner,
                    goal="hijacked", scopes=["src/"])
    assert failure.value.code == "task_exists"
    assert published == [], "a refused creation must never publish"

    after = {item["id"]: item for item in world.manager.product_snapshot(world.root)["tasks"]}
    assert sorted(after) == sorted(before) == ["task-a", "task-b"]
    assert after[existing] == before[existing]
    assert {item["owner"] for item in after.values()} == {"bob", "carol"}
    assert world.store.fetch_state()[0] == board["revision"]


def test_creation_is_owner_credential_only_and_is_decided_before_any_fetch(
        running, monkeypatch):
    world = running
    coordinator = world.manager._coordinator
    board = world.manager.product_snapshot(world.root)
    published = counted_publishes(world, monkeypatch)

    def forbidden():
        raise AssertionError("the metadata fetch must not precede the owner check")

    for member in ("bob", "carol", "dave"):
        credential = world.manager.reveal_member_once(member)["credential"]
        with monkeypatch.context() as gate:
            gate.setattr(coordinator, "_fetch_state", forbidden)
            with pytest.raises(AccessDeniedError):
                coordinator.create_task(credential, task_id="ghost", owner="bob", goal="g",
                                        scopes=[], expected_revision=board["revision"])

    for credential in ("Z" * 40, "A" * 31, "x" * 257, "ü" * 40, "short", 7, None):
        with pytest.raises(AccessDeniedError):
            coordinator.create_task(credential, task_id="ghost", owner="bob", goal="g",
                                    scopes=[], expected_revision=board["revision"])
    assert published == []
    assert board_over(world) == ["task-a", "task-b"]

    # Control: the owner's own credential is not blanket-denied by the same gate.
    owner_credential = world.manager.reveal_member_once("alice")["credential"]
    with monkeypatch.context() as gate:
        gate.setattr(coordinator, "_fetch_state", forbidden)
        with pytest.raises(AccessDeniedError):
            coordinator.create_task("A" * 40, task_id="ghost", owner="bob", goal="g",
                                    scopes=[], expected_revision=board["revision"])
    assert world.manager._credentials["alice"] == owner_credential
    receipt = create_task(world, board=board, task_id="ghost", owner="carol")
    assert receipt["taskId"] == "ghost" and len(published) == 1
    assert len(world.store.fetch_state()[1].tasks) == 3


@pytest.mark.parametrize("overrides,code,mutate", [
    ({"expected_revision": "f" * 40}, "product_stale_revision", None),
    ({"expected_revision": "z" * 40}, "product_stale_revision", None),
    ({"expected_revision": None}, "product_stale_revision", None),
    ({"expected_epoch": 99}, "epoch_mismatch", None),
    ({"expected_epoch": True}, "epoch_mismatch", None),
    ({"expected_epoch": "1"}, "epoch_mismatch", None),
    ({"expected_epoch": -1}, "epoch_mismatch", None),
    ({"expected_session_id": "other"}, "session_identity_mismatch", None),
    ({"expected_session_id": ""}, "session_identity_mismatch", None),
    ({}, "source_head_mismatch", "head"),
    ({}, "wrong_project", "root"),
], ids=["stale-revision", "non-hex-revision", "missing-revision", "epoch-ahead",
        "epoch-bool", "epoch-string", "epoch-negative", "other-session", "empty-session",
        "source-head-moved", "foreign-root"])
def test_stale_identity_and_head_gates_refuse_before_publication(
        running, monkeypatch, tmp_path, overrides, code, mutate):
    world = running
    board = world.manager.product_snapshot(world.root)
    root = world.root
    if mutate == "head":
        commit(world.root, "the source moved on")
    elif mutate == "root":
        root, _head = seeded_project(tmp_path / "elsewhere", "other")
    published = counted_publishes(world, monkeypatch)

    if root == world.root:
        call = lambda: create_task(world, board=board, **overrides)          # noqa: E731
    else:
        call = lambda: world.manager.create_product_task(                    # noqa: E731
            root, task_id="new-task", owner="bob", goal="g", scopes=["src/"], **cas(board))
    with pytest.raises(OwnerError) as failure:
        call()
    assert failure.value.code == code
    assert published == []
    assert world.store.fetch_state()[0] == board["revision"]
    assert world.manager.status()["state"] == "running"
    if mutate == "head":
        # The source moved: a metadata read is refused, so the store is the witness.
        assert [task.id for task in world.store.fetch_state()[1].tasks.values()] == ["task-a", "task-b"]
    else:
        assert board_over(world) == ["task-a", "task-b"]


# --------------------------------------------------------------------------
# 4-6. one queued task on the reviewed parent revision, declared members only
# --------------------------------------------------------------------------


def test_a_new_task_is_queued_on_the_current_reviewed_published_parent(running):
    world = running
    board = world.manager.product_snapshot(world.root)
    receipt = create_task(world, board=board, task_id="task-c", owner="carol",
                          goal="Second slice", scopes=["src/next.py", "src/next.py", "docs/"])

    assert set(receipt) == {"revision", "sessionId", "epoch", "action", "taskId", "status",
                            "owner", "contextRevision"}
    assert receipt["action"] == "createTask" and receipt["status"] == "queued"
    assert receipt["taskId"] == "task-c" and receipt["owner"] == "carol"
    assert receipt["contextRevision"] == board["revision"] != receipt["revision"]
    assert receipt["sessionId"] == SESSION and receipt["epoch"] == board["epoch"]
    assert receipt["revision"] == world.store.fetch_state()[0]
    assert world.store.is_ancestor(receipt["contextRevision"], receipt["revision"])

    _revision, live = world.store.fetch_state()
    task = live.tasks["task-c"]
    assert (task.status, task.owner, task.context_revision) == ("queued", "carol", board["revision"])
    assert task.scopes == ("src/next.py", "docs/")          # the duplicate collapsed once

    after = world.manager.product_snapshot(world.root)
    row = next(item for item in after["tasks"] if item["id"] == "task-c")
    assert set(row) == {"id", "owner", "goal", "scopes", "status", "contextRevision"}
    assert (row["status"], row["contextRevision"]) == ("queued", board["revision"])
    assert "task-c" not in after["waitingTaskIds"]


@pytest.mark.parametrize("owner,accepted", [
    ("alice", True), ("bob", True), ("carol", True), ("dave", True),
    ("mallory", False), ("eve", False), ("", False), (7, False), ("a" * 129, False),
    ("../escape", False), ("b ob", False),
], ids=["owner", "bob", "carol", "dave", "unknown", "unknown-2", "empty", "int",
        "overlong", "traversal", "space"])
def test_assignment_is_limited_to_declared_members(running, monkeypatch, owner, accepted):
    world = running
    board = world.manager.product_snapshot(world.root)
    published = counted_publishes(world, monkeypatch)

    if accepted:
        receipt = create_task(world, board=board, owner=owner)
        assert receipt["owner"] == owner and receipt["status"] == "queued"
        assert len(published) == 1
        assert world.store.fetch_state()[1].tasks["new-task"].owner == owner
        return

    with pytest.raises(OwnerError) as failure:
        create_task(world, board=board, owner=owner)
    assert failure.value.code == "invalid_members"
    assert published == []
    assert world.store.fetch_state()[0] == board["revision"]
    assert board_over(world) == ["task-a", "task-b"]


@pytest.mark.parametrize("field,value", [
    ("task_id", "../escape"),
    ("task_id", "-leading"),
    ("task_id", "t" * 129),
    ("task_id", 7),
    ("goal", ""),
    ("goal", "   "),
    ("goal", "g" * 4001),
    ("goal", 7),
    ("goal", None),
    ("scopes", "src/"),
    ("scopes", None),
    ("scopes", ["../escape"]),
    ("scopes", ["src/../../etc"]),
    ("scopes", ["/etc/passwd"]),
    ("scopes", ["C:/windows"]),
    ("scopes", [".git/config"]),
    ("scopes", ["src/.git/hooks"]),
    ("scopes", ["src/*.py"]),
    ("scopes", ["src/?"]),
    ("scopes", ["x" * 513]),
    ("scopes", [""]),
    ("scopes", [7]),
    ("scopes", ["src/"] * 33),
], ids=["id-traversal", "id-leading-dash", "id-overlong", "id-int", "goal-empty",
        "goal-blank", "goal-overlong", "goal-int", "goal-none", "scopes-not-a-list",
        "scopes-none", "scope-traversal", "scope-traversal-mid", "scope-absolute",
        "scope-drive", "scope-git", "scope-git-mid", "scope-glob-star", "scope-glob-question",
        "scope-overlong", "scope-empty", "scope-int", "scope-count"])
def test_new_task_shapes_are_bounded_and_refused_before_a_commit(
        running, monkeypatch, field, value):
    world = running
    board = world.manager.product_snapshot(world.root)
    published = counted_publishes(world, monkeypatch)

    with pytest.raises(OwnerError) as failure:
        create_task(world, board=board, **{field: value})
    assert failure.value.code == "product_invalid"
    assert published == []
    assert world.store.fetch_state()[0] == board["revision"]
    assert board_over(world) == ["task-a", "task-b"]
    assert world.manager.status()["state"] == "running"


# --------------------------------------------------------------------------
# 7-9. capacity limits, metadata-only writes and credential hygiene
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["task-count", "byte-limit"])
def test_capacity_limits_are_refused_before_a_commit(running, monkeypatch, mode):
    world = running
    board = world.manager.product_snapshot(world.root)
    revision, live = world.store.fetch_state()
    assert revision == board["revision"]

    if mode == "task-count":
        seed = {f"bulk-{index:03d}": bulk_task(index, revision, ["src/"])
                for index in range(MAX_TASKS - len(live.tasks))}
        attempt = {"task_id": "too-many", "owner": "bob", "goal": "g", "scopes": ["src/"]}
    else:
        filler = [f"src/{index:02d}/" + "b" * 480 for index in range(32)]
        added = (state_size(live, {**dict(live.tasks), "bulk-000": bulk_task(0, revision, filler)})
                 - state_size(live, dict(live.tasks)))
        seed = {}
        for index in range(64):
            if state_size(live, {**dict(live.tasks), **seed}) + added > MAX_JSON_BYTES:
                break
            seed[f"bulk-{index:03d}"] = bulk_task(index, revision, filler)
        else:  # pragma: no cover - the filler always crosses the limit
            raise AssertionError("the 64 KiB state limit was never approached")
        attempt = {"task_id": "too-big", "owner": "bob", "goal": "g", "scopes": filler}

    head = seed_state(world, seed)
    seeded = world.manager.product_snapshot(world.root)
    assert seeded["revision"] == head
    assert len(seeded["tasks"]) == len(live.tasks) + len(seed)
    assert attempt["task_id"] not in {item["id"] for item in seeded["tasks"]}

    published = counted_publishes(world, monkeypatch)
    with pytest.raises(OwnerError) as failure:
        create_task(world, board=seeded, **attempt)
    # Both limits are the same operator-facing capacity refusal (owner.py maps
    # the 256-task count AND the 64 KiB state byte limit to task_capacity).
    assert failure.value.code == "task_capacity"
    assert published == []
    assert world.store.fetch_state()[0] == head
    assert attempt["task_id"] not in board_over(world)
    assert world.manager.status()["state"] == "running"


def test_creation_touches_metadata_only_and_keeps_credentials_and_epoch_intact(running):
    world = running
    (world.root / "notes.txt").write_text("scratch\n", encoding="utf-8")
    before = source_manifest(world.root)
    assert before["status"]                       # a dirty worktree is part of the fixture
    cursors = world.private / "cursors"
    assert not cursors.exists()
    epoch_before = world.manager.status()["epoch"]
    credentials_before = dict(world.manager._credentials)
    assert sorted(credentials_before) == MEMBERS

    board = world.manager.product_snapshot(world.root)
    receipt = create_task(world, board=board)
    assert source_manifest(world.root) == before
    assert (world.root / ".imece").exists() is False
    assert not cursors.exists()

    status = world.manager.status()
    assert status["state"] == "running" and status["epoch"] == epoch_before
    assert dict(world.manager._credentials) == credentials_before
    assert world.manager._host._candidates == {} and world.manager._host._approved == {}
    assert len(world.manager._host._sessions) == 0

    with pytest.raises(OwnerError) as stale:
        create_task(world, board=board)
    leaked = str(stale.value) + repr(stale.value) + json.dumps(stale.value.receipt)
    payloads = [json.dumps(receipt), json.dumps(world.manager.product_snapshot(world.root)),
                json.dumps(status), leaked]
    assert all("credential" not in blob for blob in payloads)
    for credential in credentials_before.values():
        digest = hashlib.sha256(credential.encode("ascii")).hexdigest()
        for blob in payloads:
            assert credential not in blob, "a raw member credential escaped creation"
            assert digest not in blob, "a credential hash escaped creation"


def test_a_new_task_appears_in_the_authoritative_snapshot_and_read_only_history(running):
    world = running
    board = world.manager.product_snapshot(world.root)
    receipt = create_task(world, board=board, task_id="task-c", owner="carol",
                          scopes=["src/next.py"])
    assert receipt["revision"] == world.store.fetch_state()[0]

    after = world.manager.product_snapshot(world.root)
    assert sorted(item["id"] for item in after["tasks"]) == ["task-a", "task-b", "task-c"]
    row = next(item for item in after["tasks"] if item["id"] == "task-c")
    assert (row["owner"], row["status"], row["contextRevision"]) == ("carol", "queued", board["revision"])
    assert {"tasks": ["task-a", "task-c"], "shared": ["src/"]} in after["overlaps"]

    page = world.manager.product_changes(world.root, expected_session_id=SESSION,
                                         after_revision=board["revision"], limit=16)
    assert page["headRevision"] == page["lastRevision"] == receipt["revision"]
    assert page["hasMore"] is False and len(page["events"]) == 1
    event = page["events"][0]
    assert (event["fromRevision"], event["toRevision"]) == (board["revision"], receipt["revision"])
    assert event["contextChanged"] is False and event["taskChangeCount"] == 1
    assert event["taskChanges"] == [{"taskId": "task-c", "change": "added", "previousStatus": None,
                                     "status": "queued", "previousOwner": None, "owner": "carol",
                                     "fields": []}]
    assert event["affectedTaskIds"] == ["task-c"] and event["affectedTaskCount"] == 1

    # The history read is advisory: it publishes nothing and a private reader agrees.
    assert world.store.fetch_state()[0] == receipt["revision"]
    reader = store_of(world.manager)
    assert reader.fetch_state()[0] == receipt["revision"]
    assert reader.fetch_state()[1].tasks["task-c"].status == "queued"
    for credential in world.manager._credentials.values():
        assert credential not in json.dumps(page)


# --------------------------------------------------------------------------
# 10-12. explicit local handoff, honest receipts and exactly one winner
# --------------------------------------------------------------------------


@pytest.mark.parametrize("member,accepted", [
    ("carol", True), ("bob", False), ("dave", False), ("alice", False),
], ids=["assignee", "other-member", "other-member-2", "owner"])
def test_a_new_task_can_be_locally_previewed_then_needs_a_separate_approval(
        running, tmp_path, member, accepted):
    world = running
    cursors = tmp_path / "cursors"
    host = CollaborationHost(cursors)
    board = world.manager.product_snapshot(world.root)
    create_task(world, board=board, task_id="task-c", owner="carol", scopes=["src/next.py"])

    if not accepted:
        with pytest.raises(OwnerError) as failure:
            world.manager.preview_local_collaboration(host, world.root,
                                                      member_id=member, task_id="task-c")
        assert failure.value.code == "invalid_task_assignment"
        assert host._candidates == {} and host._approved == {} and len(host._sessions) == 0
        return

    handoff = world.manager.preview_local_collaboration(
        host, world.root, member_id=member, task_id="task-c")
    preview = handoff["preview"]
    assert handoff["taskId"] == "task-c" and handoff["epoch"] == board["epoch"]
    assert preview["memberId"] == "carol" and preview["taskId"] == "task-c"
    assert (preview["task"]["owner"], preview["task"]["status"]) == ("carol", "queued")
    assert preview["task"]["contextRevision"] == board["revision"]
    assert len(host._candidates) == 1 and host._approved == {}
    assert len(host._sessions) == 0 and not list(cursors.rglob("*"))

    approval = host.approve(preview["previewId"], world.root)
    assert host._candidates == {} and len(host._approved) == 1
    assert approval["resetCursor"] is False and approval["preview"]["taskId"] == "task-c"
    assert len(host._sessions) == 0, "an approval is not a native run"
    assert not list(cursors.rglob("*"))

    host.release(approval["approvalHandle"])
    assert host._approved == {} and len(host._sessions) == 0


def test_a_committed_receipt_survives_a_failing_post_write_refresh(running):
    world = running
    board = world.manager.product_snapshot(world.root)
    receipt = create_task(world, board=board, task_id="task-c")
    reader = store_of(world.manager)
    assert reader.fetch_state()[0] == receipt["revision"]
    assert receipt["contextRevision"] == board["revision"]

    commit(world.root, "the source moved on")
    for call in (lambda: world.manager.product_snapshot(world.root),
                 lambda: create_task(world, board=board, task_id="task-d")):
        with pytest.raises(OwnerError) as failure:
            call()
        assert failure.value.code == "source_head_mismatch"

    assert reader.fetch_state()[0] == receipt["revision"]
    assert reader.fetch_state()[1].tasks["task-c"].status == "queued"
    assert world.store.fetch_state()[0] == receipt["revision"]
    assert world.manager.status()["state"] == "running"


@pytest.mark.parametrize("second_id", ["dup", "other"], ids=["same-id", "different-id"])
def test_two_concurrent_creations_accept_exactly_one(running, second_id):
    world = running
    board = world.manager.product_snapshot(world.root)
    barrier = threading.Barrier(2)
    outcomes: dict[str, tuple[str, object]] = {}

    def attempt(key: str, task_id: str) -> None:
        barrier.wait(5)
        try:
            outcomes[key] = ("ok", create_task(world, board=board, task_id=task_id))
        except OwnerError as error:
            outcomes[key] = ("error", error)

    workers = [threading.Thread(target=attempt, args=("first", "dup"), daemon=True),
               threading.Thread(target=attempt, args=("second", second_id), daemon=True)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
    assert all(not worker.is_alive() for worker in workers)
    assert len(outcomes) == 2

    accepted = [value for value in outcomes.values() if value[0] == "ok"]
    refused = [value for value in outcomes.values() if value[0] == "error"]
    assert len(accepted) == 1 and len(refused) == 1
    assert refused[0][1].code == "product_stale_revision"
    receipt = accepted[0][1]

    revision, live = world.store.fetch_state()
    assert revision == receipt["revision"] != board["revision"]
    assert sorted(set(live.tasks) - {"task-a", "task-b"}) == [receipt["taskId"]]
    assert {task.status for task in live.tasks.values()} == {"queued"}
    assert world.manager.product_snapshot(world.root)["revision"] == revision


def test_a_drain_queued_behind_a_gated_publish_drains_and_status_stays_fast(running, monkeypatch):
    world = running
    entered, release = threading.Event(), threading.Event()
    original = world.store.publish

    def gated(candidate, *, expected_revision):
        entered.set()
        if not release.wait(15):
            raise AssertionError("the gated publication was never released")
        return original(candidate, expected_revision=expected_revision)

    monkeypatch.setattr(world.store, "publish", gated)
    board = world.manager.product_snapshot(world.root)
    outcome: dict[str, object] = {}
    drained: dict[str, object] = {}

    def create() -> None:
        try:
            outcome["result"] = create_task(world, board=board, task_id="task-c")
        except BaseException as error:  # noqa: BLE001 - asserted below
            outcome["error"] = error

    def drain() -> None:
        try:
            drained["status"] = world.manager.stop()
        except BaseException as error:  # noqa: BLE001 - asserted below
            drained["error"] = error

    creator = threading.Thread(target=create, daemon=True)
    creator.start()
    assert entered.wait(10), "the creation never reached the gated publication"

    began = time.monotonic()
    status = world.manager.status()
    elapsed = time.monotonic() - began
    assert elapsed < 2.0 and creator.is_alive()
    assert status["state"] == "running" and status["tasks"]
    assert "credential" not in json.dumps(status)

    stopper = threading.Thread(target=drain, daemon=True)
    stopper.start()
    threading.Event().wait(0.2)
    assert stopper.is_alive(), "the drain must queue behind the gated publication"
    assert world.manager.status()["state"] == "running"
    assert sorted(world.manager._credentials) == MEMBERS

    release.set()
    creator.join(20)
    stopper.join(20)
    assert not creator.is_alive() and not stopper.is_alive()
    assert "error" not in outcome, outcome.get("error")
    assert "error" not in drained, drained.get("error")
    assert outcome["result"]["revision"] == world.store.fetch_state()[0]
    assert drained["status"]["state"] == "stopped"
    assert world.manager._credentials == {} and world.manager._server is None
    assert world.manager._coordinator is None
    assert world.manager.product_snapshot(world.root)["state"] == "stopped"


# --------------------------------------------------------------------------
# 13-16. bridge parameter contracts, truthful receipts and fixed refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("params,code", [
    ({}, "owner_confirmation_required"),
    (CREATE_BASE | {"unexpected": 1}, "owner_invalid"),
    (CREATE_BASE | {"memberId": "bob", "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"contextRevision": REVISION, "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"credentials": "secret", "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"status": "queued"}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"contextRevision": REVISION}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"credentials": "secret"}}, "owner_invalid"),
    (CREATE_BASE | {"task": {key: value for key, value in CREATE_TASK.items() if key != "scopes"}},
     "owner_invalid"),
    (CREATE_BASE | {"task": "not-a-dict"}, "owner_invalid"),
    (CREATE_BASE | {"task": None}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"id": "../escape"}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"id": ""}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"id": "t" * 129}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"id": 7}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"owner": "../escape"}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"owner": "c" * 129}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"goal": ""}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"goal": "g" * 4001}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"goal": 7}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"scopes": "src/"}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"scopes": ["src/"] * 33}}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK | {"scopes": ["x" * 513]}}, "owner_invalid"),
    (CREATE_BASE | {"expectedRevision": "z" * 40, "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"expectedRevision": "a" * 41, "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"expectedRevision": 1, "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"expectedRevision": None, "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"expectedEpoch": True, "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"expectedEpoch": "1", "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"expectedEpoch": -1, "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"expectedEpoch": 1.0, "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"expectedSessionId": "../escape", "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"expectedSessionId": "s" * 129, "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"expectedSessionId": "", "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"expectedSessionId": 7, "task": CREATE_TASK}, "owner_invalid"),
    (CREATE_BASE | {"expectedRevision": REVISION, "task": CREATE_TASK, "confirm2": 1}, "owner_invalid"),
    (CREATE_BASE | {"task": CREATE_TASK, "confirm": "true"}, "owner_confirmation_required"),
    (CREATE_BASE | {"task": CREATE_TASK, "confirm": 1}, "owner_confirmation_required"),
    (CREATE_BASE | {"task": CREATE_TASK, "confirm": None}, "owner_confirmation_required"),
    (CREATE_BASE | {"task": CREATE_TASK, "confirm": {}}, "owner_confirmation_required"),
    (CREATE_BASE | {"task": CREATE_TASK, "confirm": "yes"}, "owner_confirmation_required"),
    (CREATE_BASE | {"task": CREATE_TASK, "confirm": 1.0}, "owner_confirmation_required"),
], ids=[
    "no-params", "unknown-key", "member-override", "context-revision-override",
    "credentials-override", "status-override", "task-context-revision", "task-credentials",
    "task-missing-scopes", "task-not-a-dict", "task-none", "id-traversal", "id-empty",
    "id-overlong", "id-int", "owner-traversal", "owner-overlong", "goal-empty",
    "goal-overlong", "goal-int", "scopes-not-a-list", "scope-count", "scope-overlong",
    "revision-non-hex", "revision-long", "revision-int", "revision-none",
    "epoch-bool", "epoch-string", "epoch-negative", "epoch-float", "session-traversal",
    "session-overlong", "session-empty", "session-int", "second-confirm-key",
    "confirm-string", "confirm-int", "confirm-none", "confirm-dict", "confirm-yes",
    "confirm-float"])
def test_the_create_task_bridge_refuses_everything_invalid_before_the_project_lookup(
        monkeypatch, params, code):
    monkeypatch.setattr(owner_api, "_project",
                        lambda: (_ for _ in ()).throw(AssertionError("project I/O")))
    with pytest.raises(BridgeError) as failure:
        owner_api._product_create_task(params, None)
    assert failure.value.code == code
    assert "Traceback" not in str(failure.value)

    # Fully valid requests reach project lookup; unsafe scopes are rejected by
    # the pure schema validator before any project/Git I/O.
    monkeypatch.undo()
    for task, expected in ((CREATE_TASK, "no_project"),
                           ({**CREATE_TASK, "scopes": ["../escape"]}, "owner_invalid")):
        with pytest.raises(BridgeError) as control:
            owner_api._product_create_task(dict(CREATE_BASE, task=dict(task)), None)
        assert control.value.code == expected


def test_a_real_bridge_create_reports_a_truthful_receipt_across_a_generation_change(
        bridge_world):
    world = bridge_world
    generation = world.generation
    board = bridge_board(world)
    params = {"confirm": True, "expectedRevision": board["revision"],
              "expectedEpoch": board["epoch"], "expectedSessionId": SESSION,
              "task": dict(CREATE_TASK)}

    created = rpc("collab.owner.createTask", params)
    assert created["ok"] is True, created
    receipt = created["result"]
    assert receipt["action"] == "createTask" and receipt["status"] == "queued"
    assert receipt["taskId"] == "task-c" and receipt["owner"] == "carol"
    assert receipt["contextRevision"] == board["revision"]
    assert receipt["epoch"] == board["epoch"] and receipt["sessionId"] == SESSION
    assert receipt["revision"] == world.store.fetch_state()[0] != board["revision"]
    assert board_over(world) == ["task-a", "task-b", "task-c"]

    # A project switch after the commit cannot relabel or discard a committed write.
    generation.advance = 2
    generation.reset()
    second = rpc("collab.owner.createTask",
                 {**params, "expectedRevision": receipt["revision"],
                  "task": {**CREATE_TASK, "id": "task-d"}})
    assert second["ok"] is True, second
    assert second["result"]["revision"] == world.store.fetch_state()[0]
    assert second["result"]["contextRevision"] == receipt["revision"]

    generation.advance = 0
    final = bridge_board(world)
    assert final["revision"] == second["result"]["revision"]
    assert sorted(item["id"] for item in final["tasks"]) == ["task-a", "task-b", "task-c", "task-d"]
    for blob in (json.dumps(created), json.dumps(second), json.dumps(final)):
        assert "credential" not in blob
        for credential in world.manager._credentials.values():
            assert credential not in blob
            assert hashlib.sha256(credential.encode("ascii")).hexdigest() not in blob
    assert world.manager.status()["state"] == "running"


def test_bridge_create_refusals_report_fixed_translated_messages(bridge_world):
    world = bridge_world
    board = bridge_board(world)
    base = {"confirm": True, "expectedRevision": board["revision"],
            "expectedEpoch": board["epoch"], "expectedSessionId": SESSION}
    task = dict(CREATE_TASK)

    cases = [
        ({**base, "expectedRevision": "f" * 40, "task": task},
         "owner_product_stale_revision", "product_stale_revision"),
        ({**base, "expectedEpoch": 99, "task": task}, "owner_epoch_mismatch", "epoch_mismatch"),
        ({**base, "expectedSessionId": "other", "task": task},
         "owner_session_identity_mismatch", "session_identity_mismatch"),
        ({**base, "task": {**task, "owner": "mallory"}}, "owner_invalid_members", "invalid_members"),
        ({**base, "task": {**task, "id": "task-a"}}, "owner_task_exists", "task_exists"),
        ({**base, "task": {**task, "scopes": ["../escape"]}}, "owner_invalid", None),
    ]
    for params, code, owner_code in cases:
        refused = rpc("collab.owner.createTask", params)
        assert refused["ok"] is False, refused
        assert refused["error"]["code"] == code, refused
        message = refused["error"]["message"]
        assert message == (owner_api._ERROR_MESSAGES[owner_code] if owner_code is not None
                           else "Yeni görev bilgileri geçersiz.")
        assert "Traceback" not in message and "/" not in message
        assert str(world.private) not in message and "secret" not in message
        for credential in world.manager._credentials.values():
            assert credential not in json.dumps(refused)

    assert world.store.fetch_state()[0] == board["revision"]
    assert board_over(world) == ["task-a", "task-b"]

    # The refusals did not wedge the session: the same board still accepts one creation.
    accepted = rpc("collab.owner.createTask", {**base, "task": task})
    assert accepted["ok"] is True, accepted
    assert accepted["result"]["revision"] == world.store.fetch_state()[0]
    assert board_over(world) == ["task-a", "task-b", "task-c"]
