"""Owner-side change history: a bounded, advisory DISPLAY cursor over metadata.

Real temporary Git repositories, real metadata stores and a real loopback
listener are used (the fixtures are imported from ``test_collab_product_
regressions``, never copied, so every listener is closed in their ``finally``
blocks). The history read itself is an in-process read of the private metadata
store: no external service, no model and no network is contacted, and the one
race case injects its interleaving at the store boundary -- it is NOT a socket
race and is never described as one.

The contract pinned here: pages are contiguous and bounded by the requested
limit, a cursor at the head is empty, a cursor outside the recent window is
refused instead of being deep-paged, limit/session/cursor/root/head/identity/
member gates all refuse with fixed codes before anything is published, a
history read publishes nothing and never touches the source checkout, the
native acknowledgement cursor files or the credential material, and both the
manager and the bridge report sanitized fixed messages rather than raw
exception text.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from collab_runtime.errors import ReplayUnavailableError, ValidationError
from collab_runtime.models import build_context
from collab_runtime.owner import OwnerError, OwnerSessionManager
from collab_runtime.store import REPLAY_WINDOW, SESSION_BRANCH, StateReplay
from webhost import state
from webhost.api import owner as owner_api  # registers the owner RPC handlers
from webhost.bridge import BridgeError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_collab_product_regressions import (  # noqa: E402  (imported fixtures, not copies)
    MEMBERS, SESSION, bridge_world, cas, configured, git, rpc, running, seeded_project,
    set_status, source_manifest, store_of,
)


SECRET_GOAL = "goal-secret-4b71"
SECRET_DECISION = "decision-secret-4b71"
SECRET_VALUE = "value-secret-4b71"
EVENT_KEYS = {"fromRevision", "toRevision", "metadataCheckpoint", "goalChanged", "decisionsChanged",
              "interfaces", "taskChanges", "taskChangeCount", "taskChangesTruncated",
              "affectedTaskIds", "affectedTaskCount", "affectedTasksTruncated", "contextChanged"}
PAGE_KEYS = {"sessionId", "baseCommit", "epoch", "headRevision", "lastRevision", "hasMore", "events"}


def metadata_revisions(world) -> list[str]:
    """Publish three real transitions (one context, then two task statuses)."""
    board = world.manager.product_snapshot(world.root)
    revisions = [board["revision"]]
    world.manager.update_product_context(
        world.root, context={"goal": SECRET_GOAL, "decisions": [SECRET_DECISION],
                             "interfaces": {"api": SECRET_VALUE}}, **cas(board))
    revisions.append(world.manager.product_snapshot(world.root)["revision"])
    for task_id, status, member_id in (("task-a", "done", None), ("task-b", "running", "carol")):
        set_status(world, status, task_id=task_id, member_id=member_id)
        revisions.append(world.manager.product_snapshot(world.root)["revision"])
    return revisions


@pytest.fixture
def history(running):
    """``running`` plus three published transitions and their revision chain."""
    world = running
    revisions = metadata_revisions(world)
    return SimpleNamespace(world=world, first=revisions[0], head=revisions[-1],
                           revisions=revisions, store=world.store)


def test_product_changes_are_contiguous_bounded_metadata_only(tmp_path):
    root = tmp_path / "project"; root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "base"], check=True)
    manager = OwnerSessionManager(tmp_path / "private")
    preview = manager.preview_create(root, session_id="demo", target_version="v1", goal="initial", owner_id="alice", member_ids=["alice", "bob"], tasks=[{"id":"one", "owner":"bob", "goal":"work", "scopes":["src/"]}])
    manager.create(preview["previewId"], root); manager.start(root)
    initial = manager.product_snapshot(root)
    manager.update_product_context(root, context={"goal":"secret goal", "decisions":["secret decision"], "interfaces":{"api":"secret value"}}, expected_revision=initial["revision"], expected_epoch=initial["epoch"], expected_session_id="demo")
    second = manager.product_snapshot(root)
    manager.update_product_task(root, task_id="one", status="done", expected_revision=second["revision"], expected_epoch=second["epoch"], expected_session_id="demo")
    page = manager.product_changes(root, expected_session_id="demo", after_revision=initial["revision"], limit=1)
    assert len(page["events"]) == 1 and page["hasMore"]
    assert page["lastRevision"] == second["revision"]
    next_page = manager.product_changes(root, expected_session_id="demo", after_revision=page["lastRevision"])
    assert len(next_page["events"]) == 1 and not next_page["hasMore"]
    assert page["events"][0]["interfaces"]["added"] == ["api"]
    assert "secret" not in str(page) + str(next_page)
    empty = manager.product_changes(root, expected_session_id="demo", after_revision=next_page["headRevision"])
    assert empty["events"] == []
    assert "credential" not in str(page) + str(next_page)
    for args in ({"expected_session_id": None, "after_revision": initial["revision"]}, {"expected_session_id": "demo", "after_revision": "bad"}, {"expected_session_id": "demo", "after_revision": initial["revision"], "limit": True}):
        with pytest.raises(OwnerError): manager.product_changes(root, **args)
    with pytest.raises(OwnerError, match="product_history_unavailable"):
        manager.product_changes(root, expected_session_id="demo", after_revision="f" * 40)
    manager.stop()


# --------------------------------------------------------------------------
# 1-2. pagination: contiguous pages, a bounded limit, an empty head cursor
# --------------------------------------------------------------------------


def test_pages_are_contiguous_and_carry_only_display_metadata(history):
    world, store = history.world, history.store
    cursor, pages, events = history.first, [], []
    for _ in range(4):
        page = world.manager.product_changes(world.root, expected_session_id=SESSION,
                                             after_revision=cursor, limit=1)
        assert set(page) == PAGE_KEYS
        assert page["sessionId"] == SESSION
        assert page["baseCommit"] == world.head
        assert page["epoch"] == world.manager.status()["epoch"]
        assert page["headRevision"] == history.head == store.fetch_state()[0]
        assert len(page["events"]) == 1
        event = page["events"][0]
        assert set(event) == EVENT_KEYS
        assert event["fromRevision"] == cursor and event["toRevision"] == page["lastRevision"]
        events.append(event)
        pages.append(page)
        cursor = page["lastRevision"]
        if not page["hasMore"]:
            break

    assert len(pages) == 3 and [page["hasMore"] for page in pages] == [True, True, False]
    assert cursor == history.head
    assert [event["fromRevision"] for event in events[1:]] == [event["toRevision"] for event in events[:-1]]
    assert [event["toRevision"] for event in events] == history.revisions[1:]
    assert events[0]["interfaces"]["added"] == ["api"] and events[0]["contextChanged"] is True
    assert events[1]["taskChanges"][0]["fields"] == ["status"]
    assert events[1]["taskChanges"][0]["taskId"] == "task-a"
    assert events[2]["taskChanges"][0]["taskId"] == "task-b"
    assert all(event["metadataCheckpoint"] is False for event in events)
    assert all(not event["taskChangesTruncated"] and not event["affectedTasksTruncated"]
               for event in events)
    blob = json.dumps(pages)
    assert "secret" not in blob and SECRET_VALUE not in blob


@pytest.mark.parametrize("limit,events,has_more", [
    (1, 1, True), (2, 2, True), (3, 3, False), (16, 3, False),
])
def test_one_page_is_bounded_by_the_limit_and_reports_the_remaining_history(
        history, limit, events, has_more):
    world = history.world
    page = world.manager.product_changes(world.root, expected_session_id=SESSION,
                                         after_revision=history.first, limit=limit)
    assert len(page["events"]) == events and page["hasMore"] is has_more
    assert page["headRevision"] == history.head
    assert page["lastRevision"] == history.revisions[events]
    for index, event in enumerate(page["events"]):
        assert event["fromRevision"] == history.revisions[index]
        assert event["toRevision"] == history.revisions[index + 1]

    at_head = world.manager.product_changes(world.root, expected_session_id=SESSION,
                                            after_revision=history.head)
    assert at_head["events"] == [] and at_head["hasMore"] is False
    assert at_head["lastRevision"] == at_head["headRevision"] == history.head
    assert world.manager.product_changes(world.root, expected_session_id=SESSION,
                                         after_revision=history.revisions[-1])["events"] == []


# --------------------------------------------------------------------------
# 3-4. argument gates refuse before any history is read or published
# --------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, 17, 32, 33, True, False, "2", None, 1.5, [1]],
                         ids=["zero", "negative", "over-sixteen", "thirty-two", "over-max", "bool-true",
                              "bool-false", "string", "none", "float", "list"])
def test_a_page_limit_outside_one_to_sixteen_is_refused_without_publishing(history, limit):
    world = history.world
    with pytest.raises(OwnerError) as failure:
        world.manager.product_changes(world.root, expected_session_id=SESSION,
                                      after_revision=history.first, limit=limit)
    assert failure.value.code == "product_invalid"
    assert world.store.fetch_state()[0] == history.head
    assert world.manager.status()["state"] == "running"


@pytest.mark.parametrize("kwargs,code", [
    ({"expected_session_id": None}, "session_identity_mismatch"),
    ({"expected_session_id": ""}, "session_identity_mismatch"),
    ({"expected_session_id": 7}, "session_identity_mismatch"),
    ({"expected_session_id": "other"}, "session_identity_mismatch"),
    ({"expected_session_id": "../escape"}, "product_invalid"),
    ({"after_revision": "bad"}, "product_invalid"),
    ({"after_revision": "z" * 40}, "product_invalid"),
    ({"after_revision": "a" * 41}, "product_invalid"),
    ({"after_revision": "A" * 40}, "product_invalid"),
    ({"after_revision": 12345}, "product_invalid"),
    ({"after_revision": "f" * 40}, "product_history_unavailable"),
], ids=["session-none", "session-empty", "session-int", "session-other", "session-unsafe",
        "cursor-short", "cursor-non-hex", "cursor-long", "cursor-uppercase", "cursor-int",
        "cursor-unknown"])
def test_session_and_cursor_arguments_are_gated_with_fixed_codes(history, kwargs, code):
    world = history.world
    args = {"expected_session_id": SESSION, "after_revision": history.first}
    args.update(kwargs)
    with pytest.raises(OwnerError) as failure:
        world.manager.product_changes(world.root, **args)
    assert failure.value.code == code
    assert str(failure.value) == code and "Traceback" not in repr(failure.value)
    assert world.store.fetch_state()[0] == history.head
    assert world.manager.status()["state"] == "running"


def test_a_cursor_outside_the_recent_window_is_refused_instead_of_deep_paged(configured):
    """A real 68-revision session: the first cursor leaves the bounded window."""
    world = configured
    store = store_of(world.manager)
    board = world.manager.product_snapshot(world.root)
    first, revision = board["revision"], board["revision"]
    for index in range(REPLAY_WINDOW + 4):
        revision = store.update_context(
            build_context(goal=f"moved {index}", decisions=[], interfaces={}),
            expected_revision=revision)
    assert revision == store.fetch_state()[0]
    assert world.manager.product_snapshot(world.root)["revision"] == revision

    with pytest.raises(OwnerError) as failure:
        world.manager.product_changes(world.root, expected_session_id=SESSION, after_revision=first)
    assert failure.value.code == "product_history_unavailable"
    served = world.manager.product_changes(world.root, expected_session_id=SESSION,
                                           after_revision=revision)
    assert served["events"] == [] and served["lastRevision"] == revision
    # The refused cursor is still a real, readable ancestor of the session head.
    assert store.is_ancestor(first, revision) and first != revision
    assert world.manager.status()["state"] == "configured"


# --------------------------------------------------------------------------
# 5-7. project, head, identity and membership gates
# --------------------------------------------------------------------------


def test_history_refuses_a_foreign_absent_or_moved_project(history, tmp_path):
    world = history.world
    other, _head = seeded_project(tmp_path / "side", "other")
    plain = tmp_path / "plain"
    plain.mkdir()
    for root, code in ((other, "wrong_project"), (plain, "invalid_project"),
                       (tmp_path / "absent", "invalid_project")):
        with pytest.raises(OwnerError) as failure:
            world.manager.product_changes(root, expected_session_id=SESSION,
                                          after_revision=history.first)
        assert failure.value.code == code
    assert world.store.fetch_state()[0] == history.head

    subprocess.run(["git", "-C", str(world.root), "-c", "user.name=test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "moved"],
                   check=True)
    with pytest.raises(OwnerError) as failure:
        world.manager.product_changes(world.root, expected_session_id=SESSION,
                                      after_revision=history.first)
    assert failure.value.code == "source_head_mismatch"
    assert world.store.fetch_state()[0] == history.head


@pytest.mark.parametrize("kind", ["foreign-identity", "non-member-owner"],
                         ids=["foreign-identity", "non-member-owner"])
def test_a_replayed_state_that_leaves_the_session_is_refused(history, monkeypatch, kind):
    """A synthetic replay page stands in for a corrupt or hand-edited store."""
    world = history.world
    revision, live = world.store.fetch_state()
    live_task = live.tasks["task-a"]
    if kind == "foreign-identity":
        previous = type(live)(session_id="other", target_version=live.target_version,
                              base_commit=live.base_commit, context=live.context, tasks=live.tasks)
    else:
        intruder = type(live_task)(id="task-a", owner="mallory", goal=live_task.goal,
                                   scopes=live_task.scopes, status=live_task.status,
                                   context_revision=live_task.context_revision)
        previous = type(live)(session_id=live.session_id, target_version=live.target_version,
                              base_commit=live.base_commit, context=live.context,
                              tasks={"task-a": intruder})
    monkeypatch.setattr(world.store, "replay_states", lambda after_revision, **_kwargs: StateReplay(
        head_revision=revision, after_revision=after_revision, previous_state=previous,
        states=(), has_more=False))
    with pytest.raises(OwnerError) as failure:
        world.manager.product_changes(world.root, expected_session_id=SESSION,
                                      after_revision=history.first)
    assert failure.value.code == "product_invalid"
    assert world.store.fetch_state()[0] == history.head


def test_a_published_non_member_owner_is_refused_by_every_product_read(history):
    world = history.world
    revision, live = world.store.fetch_state()
    task = live.tasks["task-a"]
    world.store.upsert_task(type(task)(id="task-a", owner="mallory", goal=task.goal,
                                       scopes=task.scopes, status=task.status,
                                       context_revision=task.context_revision),
                            expected_revision=revision)
    for call in (lambda: world.manager.product_snapshot(world.root),
                 lambda: world.manager.product_changes(world.root, expected_session_id=SESSION,
                                                      after_revision=history.first)):
        with pytest.raises(OwnerError) as failure:
            call()
        assert failure.value.code == "invalid_members"
    assert world.manager.status()["state"] == "running"


# --------------------------------------------------------------------------
# 8-9. an advisory display cursor: no writes, no source, no native cursors
# --------------------------------------------------------------------------


def test_history_is_advisory_and_publishes_nothing_anywhere(history, tmp_path):
    world = history.world
    store_path = world.manager.status()["storePath"]
    cursor_root = tmp_path / "native-cursors"
    cursor_root.mkdir(mode=0o700)
    native = cursor_root / ("0" * 64 + ".json")
    native.write_text('{"ackedRevision": "%s"}\n' % history.revisions[1], encoding="utf-8")
    native_bytes, native_info = native.read_bytes(), native.stat()
    before_source = source_manifest(world.root)
    before_refs = git(store_path, "show-ref")
    before_count = git(store_path, "rev-list", "--count", SESSION_BRANCH)

    seen, cursor = [], history.first
    while True:
        page = world.manager.product_changes(world.root, expected_session_id=SESSION,
                                             after_revision=cursor, limit=1)
        seen.extend(page["events"])
        cursor = page["lastRevision"]
        if not page["hasMore"]:
            break
    for _ in range(2):  # a repeated walk is equally inert
        replayed = world.manager.product_changes(world.root, expected_session_id=SESSION,
                                                 after_revision=history.first, limit=16)
        assert len(replayed["events"]) == 3

    assert len(seen) == 3 and cursor == history.head
    assert git(store_path, "rev-list", "--count", SESSION_BRANCH) == before_count
    assert git(store_path, "show-ref") == before_refs
    assert git(store_path, "rev-parse", SESSION_BRANCH) == history.head
    assert source_manifest(world.root) == before_source
    assert (world.root / ".imece").exists() is False

    # The native acknowledgement/reset cursor files are owned by the host and
    # are never read, rewritten, created or removed by a history read.
    assert sorted(path.name for path in cursor_root.iterdir()) == [native.name]
    assert native.read_bytes() == native_bytes
    assert native.stat().st_mtime_ns == native_info.st_mtime_ns
    assert native.stat().st_size == native_info.st_size
    assert world.manager.status()["state"] == "running"


def test_history_payloads_never_carry_credentials_hashes_or_private_paths(history):
    world = history.world
    blob = json.dumps([
        world.manager.product_changes(world.root, expected_session_id=SESSION,
                                      after_revision=history.first, limit=16),
        world.manager.product_changes(world.root, expected_session_id=SESSION,
                                      after_revision=history.head),
    ])
    assert "credential" not in blob and "secret" not in blob
    assert str(world.private) not in blob and str(world.manager.status()["storePath"]) not in blob
    assert sorted(world.manager._credentials) == MEMBERS
    for credential in world.manager._credentials.values():
        assert credential not in blob
        assert hashlib.sha256(credential.encode("ascii")).hexdigest() not in blob


# --------------------------------------------------------------------------
# 10-11. races and failures keep fixed, sanitized codes
# --------------------------------------------------------------------------


def test_a_replay_head_that_moves_mid_read_is_refused_instead_of_a_mixed_page(history, monkeypatch):
    """The interleaving is injected at the store boundary (not a socket race)."""
    world, store = history.world, history.store
    original = store.replay_states

    def moving(after_revision, **kwargs):
        revision, live = store.fetch_state()
        store.update_context(build_context(goal="moved during the replay", decisions=[],
                                           interfaces={}), expected_revision=revision)
        return original(after_revision, **kwargs)

    monkeypatch.setattr(store, "replay_states", moving)
    with pytest.raises(OwnerError) as failure:
        world.manager.product_changes(world.root, expected_session_id=SESSION,
                                      after_revision=history.first, limit=16)
    assert failure.value.code == "product_stale"

    monkeypatch.setattr(store, "replay_states", original)
    moved = world.manager.product_changes(world.root, expected_session_id=SESSION,
                                          after_revision=history.first, limit=16)
    assert len(moved["events"]) == 4 and moved["lastRevision"] == moved["headRevision"]
    assert moved["events"][0]["goalChanged"] is True
    assert store.fetch_state()[0] == world.manager.product_snapshot(world.root)["revision"]
    assert world.manager.status()["state"] == "running"


@pytest.mark.parametrize("raised,code", [
    (OSError("git died in /home/someuser/private/store"), "product_read_failed"),
    (RuntimeError("unexpected replay failure"), "product_read_failed"),
    (ReplayUnavailableError("the requested revision is not in the recent session history"),
     "product_history_unavailable"),
    (ValidationError("session state exceeds the 65536-byte limit"), "product_invalid"),
], ids=["git-failure", "unexpected", "unavailable", "invalid"])
def test_injected_history_failures_keep_fixed_sanitized_codes(history, monkeypatch, raised, code):
    world = history.world
    original = world.store.replay_states

    def boom(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(world.store, "replay_states", boom)
    with pytest.raises(OwnerError) as failure:
        world.manager.product_changes(world.root, expected_session_id=SESSION,
                                      after_revision=history.first)
    leaked = str(failure.value) + repr(failure.value) + json.dumps(failure.value.receipt)
    assert failure.value.code == code and str(failure.value) == code
    assert leaked == f"{code}OwnerError({code!r}){{}}" and failure.value.receipt == {}
    assert "Traceback" not in leaked and "git died" not in leaked
    assert "private/store" not in leaked and "unexpected replay failure" not in leaked
    assert world.store.fetch_state()[0] == history.head
    monkeypatch.setattr(world.store, "replay_states", original)
    assert world.manager.product_changes(world.root, expected_session_id=SESSION,
                                         after_revision=history.first)["events"]


# --------------------------------------------------------------------------
# 12-14. the bridge surface
# --------------------------------------------------------------------------


CHANGES_BASE = {"expectedSessionId": SESSION, "afterRevision": "a" * 40}


@pytest.mark.parametrize("params", [
    {},
    {"afterRevision": "a" * 40},
    {"expectedSessionId": SESSION},
    CHANGES_BASE | {"unexpected": 1},
    CHANGES_BASE | {"expectedSessionId": "../escape"},
    CHANGES_BASE | {"expectedSessionId": "s" * 129},
    CHANGES_BASE | {"expectedSessionId": ""},
    CHANGES_BASE | {"expectedSessionId": 7},
    CHANGES_BASE | {"afterRevision": "z" * 40},
    CHANGES_BASE | {"afterRevision": "a" * 41},
    CHANGES_BASE | {"afterRevision": "A" * 40},
    CHANGES_BASE | {"afterRevision": 1},
    CHANGES_BASE | {"afterRevision": ""},
    CHANGES_BASE | {"limit": 0},
    CHANGES_BASE | {"limit": 17},
    CHANGES_BASE | {"limit": True},
    CHANGES_BASE | {"limit": "2"},
    CHANGES_BASE | {"limit": None},
    CHANGES_BASE | {"limit": 1.0},
], ids=["empty", "no-session", "no-cursor", "unknown-key", "session-traversal", "session-long",
        "session-empty", "session-int", "cursor-non-hex", "cursor-long", "cursor-uppercase",
        "cursor-int", "cursor-empty", "limit-zero", "limit-over", "limit-bool", "limit-string",
        "limit-none", "limit-float"])
def test_the_changes_bridge_refuses_unknown_oversized_and_mistyped_params(params):
    with pytest.raises(BridgeError) as failure:
        owner_api._product_changes(params, None)
    assert failure.value.code == "owner_invalid"
    assert "Traceback" not in str(failure.value)


def test_valid_changes_params_pass_validation_and_reach_the_project_lookup():
    """Control for the matrix above: only the project lookup may refuse these."""
    with pytest.raises(BridgeError) as failure:
        owner_api._product_changes(dict(CHANGES_BASE), None)
    assert failure.value.code == "no_project"


def test_bridge_history_reads_are_generation_checked_and_fail_closed_on_a_wrong_root(
        bridge_world, monkeypatch, tmp_path):
    world = bridge_world
    revisions = metadata_revisions(world)
    head = world.store.fetch_state()[0]
    generation = world.generation

    served = rpc("collab.owner.changes", {"expectedSessionId": SESSION,
                                          "afterRevision": revisions[0], "limit": 16})
    assert served["ok"] is True, served
    page = served["result"]
    assert set(page) == PAGE_KEYS and len(page["events"]) == 3
    assert page["headRevision"] == page["lastRevision"] == head
    assert "credential" not in json.dumps(page) and "secret" not in json.dumps(page)

    # A newer project generation invalidates only the still-pending read.
    generation.advance = 2
    generation.reset()  # the next call is the handler's capture
    stale = rpc("collab.owner.changes", {"expectedSessionId": SESSION,
                                         "afterRevision": revisions[0]})
    assert stale["ok"] is False, stale
    assert stale["error"]["code"] == "owner_stale"
    assert "Traceback" not in json.dumps(stale) and "credential" not in json.dumps(stale)
    assert world.store.fetch_state()[0] == head

    # A read aimed at a root this session never configured fails closed.
    generation.advance = 0
    other, _other_head = seeded_project(tmp_path / "elsewhere", "other")
    monkeypatch.setattr(state, "get_project", lambda: SimpleNamespace(root=str(other)))
    generation.reset()
    wrong_root = rpc("collab.owner.changes", {"expectedSessionId": SESSION,
                                              "afterRevision": revisions[0]})
    assert wrong_root["ok"] is False, wrong_root
    assert wrong_root["error"]["code"] == "owner_wrong_project"
    assert wrong_root["error"]["message"] == owner_api._ERROR_MESSAGES["wrong_project"]

    monkeypatch.setattr(state, "get_project", lambda: SimpleNamespace(root=str(world.root)))
    assert rpc("collab.owner.changes", {"expectedSessionId": SESSION,
                                        "afterRevision": revisions[0]})["ok"] is True
    assert world.store.fetch_state()[0] == head
    assert world.manager.status()["state"] == "running"


def test_a_failing_bridge_history_read_reports_a_fixed_translated_message(bridge_world):
    world = bridge_world
    revisions = metadata_revisions(world)
    revision = revisions[0]

    unavailable = rpc("collab.owner.changes", {"expectedSessionId": SESSION,
                                               "afterRevision": "f" * 40})
    assert unavailable["ok"] is False, unavailable
    assert unavailable["error"]["code"] == "owner_product_history_unavailable"
    assert unavailable["error"]["message"] == owner_api._ERROR_MESSAGES["product_history_unavailable"]

    for params, code in (({"expectedSessionId": SESSION, "afterRevision": revision, "limit": 17},
                          "owner_invalid"),
                         ({"expectedSessionId": "other", "afterRevision": revision},
                          "owner_session_identity_mismatch")):
        refused = rpc("collab.owner.changes", params)
        assert refused["ok"] is False, refused
        assert refused["error"]["code"] == code
        message = refused["error"]["message"]
        assert message and "Traceback" not in message and "/" not in message
        assert str(world.private) not in message and "secret" not in message

    served = rpc("collab.owner.changes", {"expectedSessionId": SESSION, "afterRevision": revision})
    assert served["ok"] is True and len(served["result"]["events"]) == 3
    for credential in world.manager._credentials.values():
        assert credential not in json.dumps(served)
        assert hashlib.sha256(credential.encode("ascii")).hexdigest() not in json.dumps(served)
