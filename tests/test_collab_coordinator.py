"""Bounded tests for collab_runtime.coordinator (temp bare hub/store only).

Covers the first coordinator contract: authentication before any git or state
I/O, owner/member and assignee permission denials, preserved task provenance,
detached immutable snapshots, configuration validation (owner present, distinct
credentials, safe member ids, 32-256 URL-safe ASCII, at most 256 principals),
pinned session identity that fails closed on a later rogue hub state, ordered
payload-free replay events with paging/cursor progress (including a semantically
unchanged commit and a removed task), and three real deterministic races driven
by threading events (never sleeps). No source checkout, no network, no user
repositories. GitStore primitive history coverage stays in
tests/test_collab_history.py.
"""

import hashlib
import shutil
import sys
import threading
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collab_runtime import store as store_module  # noqa: E402
from collab_runtime.coordinator import Coordinator  # noqa: E402
from collab_runtime.errors import (  # noqa: E402
    AccessDeniedError,
    ReplayUnavailableError,
    StaleRevisionError,
    ValidationError,
)
from collab_runtime.models import (  # noqa: E402
    SessionState,
    SharedContext,
    build_context,
    build_initial_state,
    build_task,
    canonical_json,
    canonical_json_bytes,
)
from collab_runtime.store import STATE_PATH, GitStore  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadı")

SHA0 = "a" * 40
SESSION_ID = "demo-1"
ALICE_TOKEN = "A" * 40
BOB_TOKEN = "B" * 40
CAROL_TOKEN = "C" * 40
MALLORY_TOKEN = "M" * 40
MEMBERS = {"alice": ALICE_TOKEN, "bob": BOB_TOKEN, "carol": CAROL_TOKEN}
CREDENTIALS = (ALICE_TOKEN, BOB_TOKEN, CAROL_TOKEN, MALLORY_TOKEN)

# Credentials that can never match a configured member hash.
MALFORMED_CREDENTIALS = ("", "short", "A" * 31, "A" * 257, ALICE_TOKEN + " ", "é" * 40, ALICE_TOKEN + "\n")
WRONG_TYPE_CREDENTIALS = (None, 42, 3.5, True, b"A" * 40, [ALICE_TOKEN], {"token": ALICE_TOKEN})
UNKNOWN_CREDENTIALS = (MALLORY_TOKEN, "z" * 40, ALICE_TOKEN + "Z", ALICE_TOKEN.lower())


def _store(tmp_path, name, hub):
    path = GitStore.create_bare(tmp_path / name, what="store")
    return GitStore(store=path, remote=str(hub))


def _client(hub, tmp_path, name):
    """A second bare store that joins the already-published session."""
    store = _store(tmp_path, name, hub)
    assert store.fetch_state()[0] is not None
    return store


def _raw_publish(store, payload, *, parent, message="imece-collab: raw"):
    """Push raw session.json bytes through the store's own plumbing, bypassing
    schema validation (test-only rogue-state injection on a temp hub)."""
    commit = store._commit_payload(payload, path=STATE_PATH, parent=parent, message=message)
    assert store._cas_advance(commit), "mirror CAS failed during raw publish"
    store._push_commit(commit, stale_message="unexpected stale mirror")
    return commit


def _ctx(goal="second goal", decisions=("d1",), interfaces=None):
    return build_context(goal=goal, decisions=list(decisions), interfaces=dict(interfaces or {}))


def _seed(hub, tmp_path, name="coord.store.git"):
    """Temp bare hub + one client store seeded from base 'a'*40 (no source
    checkout): initial state, one context publish, then two trusted tasks."""
    store = _store(tmp_path, name, hub)
    revision = store.init_session(
        build_initial_state(session_id=SESSION_ID, target_version="v0.1 demo", base_commit=SHA0)
    )
    revisions = [revision]
    state = store.fetch_state()[1].with_context(
        build_context(goal="shared goal", decisions=["keep it small"], interfaces={"api": "REST"})
    )
    revision = store.publish(state, expected_revision=revision)
    revisions.append(revision)
    for task_id, owner in (("t-ui", "bob"), ("t-api", "carol")):
        revision = store.upsert_task(
            build_task(
                task_id=task_id, owner=owner, goal=f"goal for {task_id}",
                scopes=[f"src/{task_id}/"], status="queued", context_revision=revision,
            ),
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
    coordinator = Coordinator(store, session_id=SESSION_ID, owner_id="alice", member_credentials=MEMBERS)
    return SimpleNamespace(
        hub=hub, store=store, coordinator=coordinator, revisions=revisions, head=revisions[-1],
    )


def _forbid_git(monkeypatch):
    """Make ANY git invocation fail loudly (recorded), so 'before any git' is
    provable rather than merely unobserved."""
    seen: list = []

    def forbidden(args, **kwargs):
        seen.append(list(args))
        raise AssertionError(f"git must not run here: {list(args)}")

    monkeypatch.setattr(store_module, "_run_git", forbidden)
    return seen


# ---------------- trusted configuration validation (no git) ----------------


def test_config_validation_rejects_bad_membership_before_any_git(wired, monkeypatch):
    store = wired.store
    seen = _forbid_git(monkeypatch)

    def build(**overrides):
        return Coordinator(store, **{**{
            "session_id": SESSION_ID, "owner_id": "alice", "member_credentials": MEMBERS,
        }, **overrides})

    for bad_session in ("", "bad id", "-leading", 42, None, "s" * 129):
        with pytest.raises(ValidationError):
            build(session_id=bad_session)
    for bad_owner in ("", "bad owner", 42, None):
        with pytest.raises(ValidationError):
            build(owner_id=bad_owner)
    too_many = {f"m{index:03d}": f"{index:032d}" for index in range(257)}
    for bad_members in ({}, [], "members", None, too_many):
        with pytest.raises(ValidationError):
            build(member_credentials=bad_members)
    # the owner must appear in the mapping, and credentials must be distinct
    with pytest.raises(ValidationError):
        build(member_credentials={"bob": BOB_TOKEN, "carol": CAROL_TOKEN})
    with pytest.raises(ValidationError):
        build(member_credentials={"alice": ALICE_TOKEN, "bob": ALICE_TOKEN})
    for bad_id in ("", "bad id", "-dash", 42, None, "m" * 129):
        with pytest.raises(ValidationError):
            build(member_credentials={"alice": ALICE_TOKEN, bad_id: "t" * 40})
    for bad_credential in ("", "short", "t" * 31, "t" * 257, "has space" + "t" * 24, "é" * 40,
                           None, 42, b"t" * 40, ["t" * 40], 3.5):
        with pytest.raises(ValidationError):
            build(member_credentials={"alice": ALICE_TOKEN, "bob": bad_credential})
    assert seen == []


def test_config_accepts_maximum_members_and_token_length_boundaries(wired):
    members = {"alice": "a" * 32}
    for index in range(1, 255):
        members[f"m{index:03d}"] = f"{index:032d}"
    members["longest"] = "z" * 256
    assert len(members) == 256

    coordinator = Coordinator(wired.store, session_id=SESSION_ID, owner_id="alice",
                              member_credentials=members)
    assert coordinator.snapshot("z" * 256).state.session_id == SESSION_ID
    assert coordinator.snapshot("a" * 32).revision == wired.head
    for out_of_range in ("z" * 257, "a" * 31, "a" * 33):
        with pytest.raises(AccessDeniedError):
            coordinator.snapshot(out_of_range)


def test_constructor_rejects_a_foreign_session_id(wired):
    for bad_session in ("other-1", "", "bad id", 42, None):
        with pytest.raises(ValidationError):
            Coordinator(wired.store, session_id=bad_session, owner_id="alice", member_credentials=MEMBERS)
    with pytest.raises(ValidationError):
        Coordinator(wired.store, session_id=SESSION_ID, owner_id="mallory", member_credentials=MEMBERS)


# ---------------- detached snapshots / configuration copy ----------------


def test_snapshots_are_detached_immutable_and_equal_for_owner_and_member(wired):
    coordinator, store = wired.coordinator, wired.store
    raw_head, raw_state = store.fetch_state()

    owner_view = coordinator.snapshot(ALICE_TOKEN)
    member_view = coordinator.snapshot(BOB_TOKEN)

    assert owner_view.revision == member_view.revision == raw_head
    assert owner_view.state == raw_state == member_view.state
    assert owner_view.state is not raw_state
    assert owner_view.state.tasks is not raw_state.tasks
    assert isinstance(owner_view.state.tasks, MappingProxyType)
    with pytest.raises(TypeError):
        owner_view.state.tasks["t-ui"] = raw_state.tasks["t-ui"]
    with pytest.raises(AttributeError):
        owner_view.state.tasks.clear()
    with pytest.raises(AttributeError):
        owner_view.revision = "b" * 40

    # a later hub write cannot retroactively change an earlier snapshot
    revision = coordinator.update_context(ALICE_TOKEN, _ctx(goal="owner goal two"), expected_revision=owner_view.revision)
    assert owner_view.state.context.goal == "shared goal"
    assert owner_view.revision == raw_head
    fresh = coordinator.snapshot(ALICE_TOKEN)
    assert fresh.revision == revision
    assert fresh.state.context.goal == "owner goal two"

    assert owner_view.to_dict() == {"revision": raw_head, "state": raw_state.to_dict()}
    assert canonical_json(owner_view.to_dict())


def test_config_mapping_mutation_neither_grants_nor_revokes_access(wired):
    config = dict(MEMBERS)
    guarded = Coordinator(wired.store, session_id=SESSION_ID, owner_id="alice", member_credentials=config)

    config["mallory"] = MALLORY_TOKEN
    config["alice"] = MALLORY_TOKEN   # would demote the owner if read live
    config.pop("carol")               # would revoke carol if read live
    config.clear()

    with pytest.raises(AccessDeniedError):
        guarded.snapshot(MALLORY_TOKEN)
    for credential in (ALICE_TOKEN, BOB_TOKEN, CAROL_TOKEN):
        assert guarded.snapshot(credential).state.session_id == SESSION_ID
    head = guarded.snapshot(ALICE_TOKEN).revision
    assert guarded.update_context(ALICE_TOKEN, _ctx(goal="still the owner"), expected_revision=head)
    with pytest.raises(AccessDeniedError):
        guarded.update_context(BOB_TOKEN, _ctx(goal="member write"), expected_revision=head)


# ---------------- authentication before any I/O, and secret hygiene ----------------


def test_invalid_credentials_are_denied_before_any_git_on_every_method(wired, monkeypatch):
    coordinator = wired.coordinator
    seen = _forbid_git(monkeypatch)

    operations = {
        "snapshot": lambda cred: coordinator.snapshot(cred),
        "update_context": lambda cred: coordinator.update_context(
            cred, _ctx(), expected_revision=wired.head),
        "update_task_status": lambda cred: coordinator.update_task_status(
            cred, task_id="t-ui", status="running", expected_revision=wired.head),
        "replay": lambda cred: coordinator.replay(cred, after_revision=wired.head, limit=8),
    }
    for credential in MALFORMED_CREDENTIALS + WRONG_TYPE_CREDENTIALS + UNKNOWN_CREDENTIALS:
        for name, call in operations.items():
            with pytest.raises(AccessDeniedError, match="access denied"):
                call(credential)
    # a valid member is still refused the owner-only write without touching the hub
    with pytest.raises(AccessDeniedError, match="access denied"):
        coordinator.update_context(BOB_TOKEN, _ctx(), expected_revision=wired.head)
    assert seen == []


def test_credentials_never_appear_in_errors_receipts_repr_or_retained_fields(wired):
    coordinator = wired.coordinator
    with pytest.raises(AccessDeniedError) as denied:
        coordinator.snapshot(MALLORY_TOKEN)

    serialized = canonical_json({
        "message": str(denied.value),
        "repr": repr(denied.value),
        "coordinator": repr(coordinator),
        "snapshot": coordinator.snapshot(ALICE_TOKEN).to_dict(),
        "page": coordinator.replay(ALICE_TOKEN, after_revision=wired.head).to_dict(),
    })
    for token in CREDENTIALS:
        assert token not in serialized
        assert token not in repr(denied.value)

    hashes = coordinator._credential_hashes
    assert set(hashes) == set(MEMBERS)
    assert all(isinstance(value, bytes) and len(value) == 32 for value in hashes.values())
    assert hashes["alice"] == hashlib.sha256(ALICE_TOKEN.encode("ascii")).digest()
    for value in vars(coordinator).values():
        rendered = repr(value)
        for token in CREDENTIALS:
            assert token not in rendered


# ---------------- owner / assignee permissions and preserved provenance ----------------


def test_owner_context_update_publishes_and_preserves_tasks(wired):
    coordinator, store = wired.coordinator, wired.store
    before = store.fetch_state()[1]

    replacement = _ctx(goal="owner goal two", decisions=("d1", "d2"), interfaces={"api": "REST"})
    revision = coordinator.update_context(ALICE_TOKEN, replacement, expected_revision=wired.head)

    head, state = store.fetch_state()
    assert head == revision != wired.head
    assert state.context == replacement
    assert state.tasks == before.tasks
    assert state.session_id == before.session_id
    assert state.base_commit == before.base_commit == SHA0
    assert state.target_version == before.target_version == "v0.1 demo"
    assert coordinator.snapshot(ALICE_TOKEN).revision == revision

    # the plain validated-dict form is accepted too
    from_dict = coordinator.update_context(
        ALICE_TOKEN, {"goal": "from a dict", "decisions": [], "interfaces": {}}, expected_revision=revision)
    assert store.fetch_state()[1].context.goal == "from a dict"
    assert from_dict != revision


def test_assignee_and_owner_status_updates_preserve_task_provenance(wired):
    coordinator, store = wired.coordinator, wired.store
    api_before = store.fetch_state()[1].tasks["t-api"]
    before = store.fetch_state()[1].tasks["t-ui"]

    revision = coordinator.update_task_status(BOB_TOKEN, task_id="t-ui", status="running",
                                             expected_revision=wired.head)
    tasks = store.fetch_state()[1].tasks
    after = tasks["t-ui"]
    assert after.status == "running"
    assert (after.owner, after.goal, after.scopes, after.context_revision) == (
        before.owner, before.goal, before.scopes, before.context_revision)
    assert after.context_revision == before.context_revision != wired.head
    assert tasks["t-api"] == api_before

    # the owner may move any task, including a member's
    revision = coordinator.update_context(ALICE_TOKEN, _ctx(goal="moved on"), expected_revision=revision)
    revision = coordinator.update_task_status(ALICE_TOKEN, task_id="t-api", status="done",
                                              expected_revision=revision)
    tasks = store.fetch_state()[1].tasks
    assert tasks["t-api"].status == "done" and tasks["t-ui"].status == "running"
    # a status change never re-acknowledges the newer context
    assert tasks["t-api"].context_revision == api_before.context_revision
    assert tasks["t-ui"].context_revision == after.context_revision

    # a carol-owned task is carol's to move
    assert coordinator.update_task_status(CAROL_TOKEN, task_id="t-api", status="waiting",
                                          expected_revision=revision)


def test_status_update_denied_for_non_assignee_and_leaves_hub_unchanged(wired):
    coordinator, store = wired.coordinator, wired.store
    with pytest.raises(AccessDeniedError, match="access denied"):
        coordinator.update_task_status(CAROL_TOKEN, task_id="t-ui", status="done",
                                       expected_revision=wired.head)
    with pytest.raises(AccessDeniedError, match="access denied"):
        coordinator.update_task_status(BOB_TOKEN, task_id="t-api", status="done",
                                       expected_revision=wired.head)
    assert store.remote_head() == wired.head
    tasks = store.fetch_state()[1].tasks
    assert tasks["t-ui"].status == "queued" and tasks["t-api"].status == "queued"


def test_invalid_status_task_id_and_missing_task_are_typed_and_never_move_the_hub(wired):
    coordinator, store = wired.coordinator, wired.store
    for bad_status in ("paused", "", "Running", 5, None, True, b"running", ["done"]):
        with pytest.raises(ValidationError):
            coordinator.update_task_status(BOB_TOKEN, task_id="t-ui", status=bad_status,
                                           expected_revision=wired.head)
    for bad_id in ("", "bad id", "t-ui!", 42, None, "t" * 129):
        with pytest.raises(ValidationError):
            coordinator.update_task_status(BOB_TOKEN, task_id=bad_id, status="running",
                                           expected_revision=wired.head)
    with pytest.raises(ValidationError, match="existing task") as missing:
        coordinator.update_task_status(BOB_TOKEN, task_id="t-absent", status="running",
                                       expected_revision=wired.head)
    assert store.remote_head() == wired.head
    assert store.fetch_state()[1].tasks["t-ui"].status == "queued"


def test_stale_expected_revision_is_rejected_without_moving_the_hub(wired):
    coordinator, store = wired.coordinator, wired.store
    stale = wired.revisions[0]
    with pytest.raises(StaleRevisionError) as context_stale:
        coordinator.update_context(ALICE_TOKEN, _ctx(goal="stale"), expected_revision=stale)
    assert "snapshot" in str(context_stale.value)
    with pytest.raises(StaleRevisionError):
        coordinator.update_task_status(BOB_TOKEN, task_id="t-ui", status="running",
                                       expected_revision=stale)
    for bad in ("deadbeef", "A" * 40, 42, None, ""):
        with pytest.raises(ValidationError):
            coordinator.update_context(ALICE_TOKEN, _ctx(), expected_revision=bad)
    assert store.remote_head() == wired.head
    assert store.fetch_state()[1].context.goal == "shared goal"


def test_expected_revision_is_required_on_every_write_and_replay(wired):
    coordinator = wired.coordinator
    with pytest.raises(TypeError):
        coordinator.update_context(ALICE_TOKEN, _ctx())
    with pytest.raises(TypeError):
        coordinator.update_context(ALICE_TOKEN, _ctx(), wired.head)
    with pytest.raises(TypeError):
        coordinator.update_task_status(ALICE_TOKEN, task_id="t-ui", status="done")
    with pytest.raises(TypeError):
        coordinator.update_task_status(ALICE_TOKEN, "t-ui", "done", wired.head)
    with pytest.raises(TypeError):
        coordinator.replay(ALICE_TOKEN)


def test_invalid_context_payloads_are_typed_and_never_move_the_hub(wired):
    coordinator, store = wired.coordinator, wired.store
    payloads = (
        None, "context", 42, [],
        {"goal": "g", "decisions": []},
        {"goal": "g", "decisions": [], "interfaces": {}, "extra": 1},
        {"goal": 5, "decisions": [], "interfaces": {}},
        {"goal": "g", "decisions": "d", "interfaces": {}},
        {"goal": "g", "decisions": [], "interfaces": [["api", "REST"]]},
        {"goal": "g" * 5000, "decisions": [], "interfaces": {}},
        SharedContext(goal=123, decisions=(), interfaces=()),
        SharedContext(goal="g", decisions="d", interfaces=()),
        SharedContext(goal="g", decisions=["d"], interfaces=()),
        SharedContext(goal="g", decisions=(), interfaces=[("api", "REST")]),
        SharedContext(goal="g", decisions=(), interfaces=("ab",)),
        SharedContext(goal="g", decisions=(), interfaces=(("api", "a"), ("api", "b"))),
        SharedContext(goal="g", decisions=(), interfaces=(("api", "REST"), "junk")),
    )
    for payload in payloads:
        with pytest.raises(ValidationError):
            coordinator.update_context(ALICE_TOKEN, payload, expected_revision=wired.head)
    assert store.remote_head() == wired.head
    assert store.fetch_state()[1].context.goal == "shared goal"


def test_status_update_rejects_foreign_task_context_provenance(wired):
    store = wired.store
    current = store.fetch_state()[1]
    task = current.tasks["t-ui"]
    foreign = build_task(
        task_id=task.id, owner=task.owner, goal=task.goal,
        scopes=list(task.scopes), status=task.status, context_revision="b" * 40,
    )
    # A trusted legacy publisher can bypass upsert_task's ancestry guard.
    head = store.publish(current.with_task(foreign), expected_revision=wired.head)
    with pytest.raises(ValidationError, match="context_revision"):
        wired.coordinator.update_task_status(
            BOB_TOKEN, task_id="t-ui", status="running", expected_revision=head,
        )
    assert store.remote_head() == head
    assert store.fetch_state()[1].tasks["t-ui"] == foreign


# ---------------- pinned session identity fails closed ----------------


def test_rogue_but_well_shaped_hub_state_fails_closed_on_every_method(wired):
    store = wired.store
    rogue = build_initial_state(session_id="rogue-9", target_version="v0.1 demo", base_commit=SHA0)
    rogue = rogue.with_context(build_context(goal="rogue", decisions=[], interfaces={}))
    rogue_commit = _raw_publish(store, canonical_json_bytes(rogue.to_dict()), parent=wired.head)
    assert store.remote_head() == rogue_commit

    with pytest.raises(ValidationError):
        wired.coordinator.snapshot(ALICE_TOKEN)
    with pytest.raises(ValidationError):
        wired.coordinator.snapshot(BOB_TOKEN)
    with pytest.raises(ValidationError):
        wired.coordinator.update_context(ALICE_TOKEN, _ctx(), expected_revision=wired.head)
    with pytest.raises(ValidationError):
        wired.coordinator.update_task_status(ALICE_TOKEN, task_id="t-ui", status="running",
                                            expected_revision=wired.head)
    with pytest.raises(ValidationError):
        wired.coordinator.replay(ALICE_TOKEN, after_revision=wired.head)
    # the rogue head is authoritative and untouched: nothing was published
    assert store.remote_head() == rogue_commit


# ---------------- ordered, payload-free replay events ----------------


def _three_updates(wired):
    coordinator = wired.coordinator
    first = coordinator.update_context(ALICE_TOKEN, _ctx(goal="goal two"), expected_revision=wired.head)
    second = coordinator.update_task_status(BOB_TOKEN, task_id="t-ui", status="running",
                                            expected_revision=first)
    third = coordinator.update_task_status(CAROL_TOKEN, task_id="t-api", status="done",
                                           expected_revision=second)
    return [first, second, third]


def test_replay_events_are_ordered_summaries_without_payloads(wired):
    coordinator = wired.coordinator
    first, second, third = _three_updates(wired)

    page = coordinator.replay(CAROL_TOKEN, after_revision=wired.head)
    assert page.head_revision == page.next_revision == third
    assert page.has_more is False
    assert [event.revision for event in page.events] == [first, second, third]
    assert [event.previous_revision for event in page.events] == [wired.head, first, second]

    context_event, bob_event, carol_event = page.events
    assert (context_event.context_changed, context_event.changed_task_ids) == (True, ())
    assert (bob_event.context_changed, bob_event.changed_task_ids) == (False, ("t-ui",))
    assert (carol_event.context_changed, carol_event.changed_task_ids) == (False, ("t-api",))
    assert isinstance(context_event.changed_task_ids, tuple)

    assert set(page.to_dict()) == {"head_revision", "next_revision", "events", "has_more"}
    assert set(context_event.to_dict()) == {
        "previous_revision", "revision", "context_changed", "changed_task_ids"}
    blob = canonical_json(page.to_dict())
    for hidden in ("src/t-ui/", "src/t-api/", "goal for t-ui", "goal for t-api", "goal two",
                   "keep it small", "REST", *CREDENTIALS):
        assert hidden not in blob


def test_replay_pagination_reports_next_revision_and_an_empty_page_at_head(wired):
    coordinator = wired.coordinator
    first, second, third = _three_updates(wired)

    page = coordinator.replay(ALICE_TOKEN, after_revision=wired.head, limit=1)
    assert [event.revision for event in page.events] == [first]
    assert page.next_revision == first
    assert page.has_more is True

    middle = coordinator.replay(BOB_TOKEN, after_revision=page.next_revision, limit=1)
    assert [event.revision for event in middle.events] == [second]
    assert middle.next_revision == second
    assert middle.has_more is True

    tail = coordinator.replay(ALICE_TOKEN, after_revision=middle.next_revision, limit=1)
    assert [event.revision for event in tail.events] == [third]
    assert tail.head_revision == tail.next_revision == third
    assert tail.has_more is False

    at_head = coordinator.replay(CAROL_TOKEN, after_revision=third)
    assert at_head.events == () and at_head.has_more is False
    assert at_head.head_revision == at_head.next_revision == third

    whole = coordinator.replay(ALICE_TOKEN, after_revision=wired.head)
    assert [event.to_dict() for event in whole.events] == [
        event.to_dict() for page_step in (page, middle, tail) for event in page_step.events]


def test_replay_survives_module_recreation_with_the_same_configuration(wired):
    first, _second, _third = _three_updates(wired)
    coordinator = wired.coordinator

    original = coordinator.replay(ALICE_TOKEN, after_revision=wired.head)
    reborn = Coordinator(wired.store, session_id=SESSION_ID, owner_id="alice",
                         member_credentials=MEMBERS)
    assert reborn.replay(ALICE_TOKEN, after_revision=wired.head) == original
    assert reborn.replay(ALICE_TOKEN, after_revision=wired.head).to_dict() == original.to_dict()

    member_only = Coordinator(wired.store, session_id=SESSION_ID, owner_id="alice",
                              member_credentials={"alice": ALICE_TOKEN, "bob": BOB_TOKEN})
    assert member_only.replay(BOB_TOKEN, after_revision=wired.head) == original
    assert member_only.snapshot(BOB_TOKEN).revision == coordinator.snapshot(CAROL_TOKEN).revision
    with pytest.raises(AccessDeniedError):
        member_only.snapshot(CAROL_TOKEN)


def test_semantically_unchanged_commit_still_advances_the_cursor(wired):
    store, coordinator = wired.store, wired.coordinator
    unchanged = store.fetch_state()[1]
    no_op = store.publish(unchanged, expected_revision=wired.head)
    assert no_op != wired.head  # a distinct child commit, identical metadata

    page = coordinator.replay(ALICE_TOKEN, after_revision=wired.head)
    assert [event.revision for event in page.events] == [no_op]
    assert page.events[0].previous_revision == wired.head
    assert page.events[0].context_changed is False
    assert page.events[0].changed_task_ids == ()
    assert page.next_revision == no_op == page.head_revision
    assert page.has_more is False


def test_removed_task_appears_in_changed_task_ids(wired):
    store, coordinator = wired.store, wired.coordinator
    state = store.fetch_state()[1]
    remaining = {task_id: task for task_id, task in state.tasks.items() if task_id != "t-api"}
    trimmed = SessionState(state.session_id, state.target_version, state.base_commit,
                            state.context, remaining)
    revision = store.publish(trimmed, expected_revision=wired.head)

    page = coordinator.replay(CAROL_TOKEN, after_revision=wired.head)
    assert [event.revision for event in page.events] == [revision]
    assert page.events[0].changed_task_ids == ("t-api",)
    assert page.events[0].context_changed is False
    assert set(coordinator.snapshot(ALICE_TOKEN).state.tasks) == {"t-ui"}


def test_replay_after_a_snapshot_reports_updates_published_since(hub, tmp_path, wired):
    coordinator = wired.coordinator
    snapshot = coordinator.snapshot(BOB_TOKEN)
    rival = _client(hub, tmp_path, "rival.store.git")

    winner = rival.update_context(_ctx(goal="rival goal"), expected_revision=snapshot.revision)
    page = coordinator.replay(CAROL_TOKEN, after_revision=snapshot.revision)
    assert page.head_revision == winner
    assert [event.revision for event in page.events] == [winner]
    assert page.events[0].previous_revision == snapshot.revision
    assert page.events[0].context_changed is True
    assert page.next_revision == winner

    owner_revision = coordinator.update_context(
        ALICE_TOKEN, _ctx(goal="owner after rival"), expected_revision=winner)
    following = coordinator.replay(ALICE_TOKEN, after_revision=page.next_revision)
    assert [event.revision for event in following.events] == [owner_revision]
    assert following.has_more is False


def test_replay_surfaces_cursor_and_limit_validation_errors(wired):
    coordinator = wired.coordinator
    for bad_cursor in ("deadbeef", "A" * 40, "", 42, None):
        with pytest.raises(ValidationError):
            coordinator.replay(ALICE_TOKEN, after_revision=bad_cursor)
    with pytest.raises(ReplayUnavailableError, match="fetch a new snapshot"):
        coordinator.replay(ALICE_TOKEN, after_revision="b" * 40)  # well-formed, foreign
    for bad_limit in (0, -1, 33, True, False, "2", None, 2.0):
        with pytest.raises(ValidationError):
            coordinator.replay(ALICE_TOKEN, after_revision=wired.head, limit=bad_limit)


# ---------------- real deterministic races (threading events, no sleeps) ----------------


def test_concurrent_writes_on_one_coordinator_leave_exactly_one_winner(wired):
    coordinator, store = wired.coordinator, wired.store
    start = threading.Barrier(2, timeout=30)
    outcomes: dict = {}

    def write(name, status):
        try:
            start.wait()
            outcomes[name] = ("ok", coordinator.update_task_status(
                ALICE_TOKEN, task_id="t-ui", status=status, expected_revision=wired.head))
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcomes[name] = ("error", exc)

    threads = [threading.Thread(target=write, args=payload, daemon=True, name=payload[0])
               for payload in (("running", "running"), ("done", "done"))]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join(timeout=60)
    finally:
        start.abort()
    assert all(not thread.is_alive() for thread in threads)

    assert sorted(kind for kind, _ in outcomes.values()) == ["error", "ok"]
    failures = [payload for kind, payload in outcomes.values() if kind == "error"]
    assert isinstance(failures[0], StaleRevisionError)
    winner = next(payload for kind, payload in outcomes.values() if kind == "ok")

    assert store.remote_head() == winner
    assert store.fetch_state()[1].tasks["t-ui"].status in {"running", "done"}
    page = coordinator.replay(ALICE_TOKEN, after_revision=wired.head)
    assert [event.revision for event in page.events] == [winner]  # no phantom loser event
    assert page.has_more is False


def test_external_reassignment_during_publication_wins_and_leaves_no_phantom_event(
    hub, tmp_path, wired, monkeypatch
):
    coordinator, store = wired.coordinator, wired.store
    rival = _client(hub, tmp_path, "rival.store.git")
    real_run_git = store_module._run_git
    parked, published = threading.Event(), threading.Event()

    def park_the_coordinator(args, *, cwd, stdin=None, env=None, check=True):
        if args and args[0] == "push" and Path(cwd) == store.store_path:
            parked.set()
            assert published.wait(timeout=30), "the rival publication never completed"
        return real_run_git(args, cwd=cwd, stdin=stdin, env=env, check=check)

    monkeypatch.setattr(store_module, "_run_git", park_the_coordinator)
    outcome: dict = {}

    def write():
        try:
            outcome["revision"] = coordinator.update_task_status(
                BOB_TOKEN, task_id="t-ui", status="running", expected_revision=wired.head)
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcome["error"] = exc

    thread = threading.Thread(target=write, daemon=True, name="coordinator-write")
    thread.start()
    try:
        assert parked.wait(timeout=30), "the coordinator never reached its push"
        reassigned = build_task(
            task_id="t-ui", owner="carol", goal="goal for t-ui", scopes=["src/t-ui/"],
            status="queued", context_revision=wired.head,
        )
        winner = rival.upsert_task(reassigned, expected_revision=wired.head)
    finally:
        published.set()
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert "revision" not in outcome
    assert isinstance(outcome.get("error"), StaleRevisionError)
    assert store.remote_head() == winner
    assert store._local_head() == wired.head  # mirror rolled back before any refetch

    page = coordinator.replay(ALICE_TOKEN, after_revision=wired.head)
    assert [event.revision for event in page.events] == [winner]
    assert page.events[0].changed_task_ids == ("t-ui",)
    assert page.events[0].context_changed is False
    task = coordinator.snapshot(CAROL_TOKEN).state.tasks["t-ui"]
    assert task.owner == "carol" and task.status == "queued"


def test_replay_page_stays_pinned_to_its_head_while_the_hub_advances(hub, tmp_path, wired, monkeypatch):
    coordinator, store = wired.coordinator, wired.store
    rival = _client(hub, tmp_path, "rival.store.git")
    cursor = wired.revisions[1]
    real_run_git = store_module._run_git
    at_rev_list, published = threading.Event(), threading.Event()

    def park_the_rev_list(args, *, cwd, stdin=None, env=None, check=True):
        if args and args[0] == "rev-list" and Path(cwd) == store.store_path:
            at_rev_list.set()
            assert published.wait(timeout=30), "the rival publication never completed"
        return real_run_git(args, cwd=cwd, stdin=stdin, env=env, check=check)

    monkeypatch.setattr(store_module, "_run_git", park_the_rev_list)
    outcome: dict = {}

    def read():
        try:
            outcome["page"] = coordinator.replay(ALICE_TOKEN, after_revision=cursor)
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcome["error"] = exc

    thread = threading.Thread(target=read, daemon=True, name="coordinator-replay")
    thread.start()
    try:
        assert at_rev_list.wait(timeout=30), "the coordinator never enumerated history"
        winner = rival.update_context(_ctx(goal="rival during replay"), expected_revision=wired.head)
    finally:
        published.set()
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert "error" not in outcome, outcome.get("error")
    page = outcome["page"]
    assert page.head_revision == wired.head                      # pinned before the rival write
    assert [event.revision for event in page.events] == wired.revisions[2:]
    assert page.next_revision == wired.head
    assert page.has_more is False

    # the concurrent update is served by the NEXT call, never smuggled into the page
    following = coordinator.replay(ALICE_TOKEN, after_revision=page.next_revision)
    assert following.head_revision == winner
    assert [event.revision for event in following.events] == [winner]
    assert following.events[0].context_changed is True
    assert following.has_more is False
