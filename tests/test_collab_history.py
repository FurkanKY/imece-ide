"""Bounded metadata replay tests for GitStore.replay_states (temp bare repos only).

Covers: ordered paging with has_more, empty page at head, deterministic
recreation of the published state sequence, malformed/foreign/too-old cursors
(monkeypatched tiny window for the cheap too-old case), strict limit
validation (bool rejected), corrupt historical session.json, identity
immutability across replayed states, and rejection of merge / noncontiguous
session history. No network, no user repos, no source checkout needed.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collab_runtime import store as store_module  # noqa: E402
from collab_runtime.errors import (  # noqa: E402
    GitOperationError,
    ReplayUnavailableError,
    ValidationError,
)
from collab_runtime.models import (  # noqa: E402
    build_context,
    build_initial_state,
    canonical_json_bytes,
)
from collab_runtime.store import STATE_PATH, GitStore  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadı")

SHA0 = "a" * 40


def _env():
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(args, cwd, check=True):
    cp = subprocess.run(["git", *args], cwd=str(cwd), env=_env(), capture_output=True, timeout=30)
    if check and cp.returncode != 0:
        raise AssertionError(f"git {args} failed: {cp.stderr.decode('utf-8', 'replace')}")
    return cp.stdout.decode("utf-8", "replace")


def _store(tmp_path, name, hub):
    path = GitStore.create_bare(tmp_path / name, what="store")
    return GitStore(store=path, remote=str(hub))


def _state(session_id="demo-1"):
    return build_initial_state(session_id=session_id, target_version="v0.1 demo", base_commit=SHA0)


def _history(hub, tmp_path, *, name="s.store.git", session_id="demo-1", commits=1):
    """Bare hub + one client store: the session starts from the empty initial
    state (base_commit 'a'*40, no source checkout) and receives `commits - 1`
    context updates. Returns (store, [revision, ...]) oldest first."""
    store = _store(tmp_path, name, hub)
    rev = store.init_session(_state(session_id=session_id))
    revs = [rev]
    for n in range(commits - 1):
        current = store.fetch_state()[1]
        context = build_context(goal=f"goal-{n}", decisions=[f"decision-{n}"], interfaces={})
        rev = store.publish(current.with_context(context), expected_revision=rev)
        revs.append(rev)
    return store, revs


def _raw_publish(store, payload, *, parent, message="imece-collab: raw"):
    """Publish raw session.json bytes through the store's own plumbing,
    bypassing schema validation (test-only corruption / rogue-state injection)."""
    commit = store._commit_payload(payload, path=STATE_PATH, parent=parent, message=message)
    if not store._cas_advance(commit):
        raise AssertionError("mirror CAS failed during raw publish")
    store._push_commit(commit, stale_message="unexpected stale mirror")
    return commit


def _merge_commit(store_repo, hub, first_parent, second_parent):
    """Push a real merge commit onto the session branch: the tree (state
    content) is unchanged, only the history shape is invalid for replay."""
    tree = _git(["rev-parse", f"{first_parent}^{{tree}}"], store_repo).strip()
    merge = _git(["commit-tree", tree, "-p", first_parent, "-p", second_parent, "-m", "merge"], store_repo).strip()
    _git(["push", hub, f"{merge}:refs/heads/imece-session"], store_repo)
    return merge


@pytest.fixture
def hub(tmp_path):
    return GitStore.create_bare(tmp_path / "hub.git", what="hub")


# ---------------- ordered paging / empty page at head ----------------


def test_replay_orders_newer_states_and_reports_more(hub, tmp_path):
    store, revs = _history(hub, tmp_path, commits=4)
    root, v1, v2, v3 = revs

    page = store.replay_states(root, limit=2)
    assert page.head_revision == v3
    assert page.after_revision == root
    assert page.previous_state.context.goal == ""  # the empty initial state
    assert [sha for sha, _ in page.states] == [v1, v2]
    assert page.states[0][1].context.goal == "goal-0"
    assert page.states[1][1].context.goal == "goal-1"
    assert page.has_more is True

    tail = store.replay_states(v2, limit=2)
    assert [sha for sha, _ in tail.states] == [v3]
    assert tail.previous_state.context.goal == "goal-1"
    assert tail.has_more is False


def test_replay_at_head_is_empty_and_reuses_head_state(hub, tmp_path):
    store, (root, v1) = _history(hub, tmp_path, commits=2)
    head, head_state = store.fetch_state()

    page = store.replay_states(v1)
    assert page.head_revision == head == v1
    assert page.after_revision == v1
    assert page.states == ()
    assert page.has_more is False
    assert page.previous_state == head_state


def test_replay_recreates_history_deterministically(hub, tmp_path):
    store, revs = _history(hub, tmp_path, commits=3)
    root = revs[0]

    first = store.replay_states(root)
    again = store.replay_states(root)
    assert first == again
    assert [sha for sha, _ in first.states] == revs[1:]
    assert first.previous_state.session_id == "demo-1"
    assert first.previous_state.base_commit == SHA0
    assert first.states[-1][1].context.goal == "goal-1"


# ---------------- unavailable cursors ----------------


def test_replay_rejects_malformed_and_foreign_cursors(hub, tmp_path):
    store, revs = _history(hub, tmp_path, commits=2)
    with pytest.raises(ValidationError):
        store.replay_states("deadbeef")  # not a full SHA (rejected before any git call)
    with pytest.raises(ValidationError):
        store.replay_states("A" * 40)  # uppercase rejected
    with pytest.raises(ReplayUnavailableError) as foreign:
        store.replay_states("b" * 40)  # well-formed but not in this history
    assert "fetch a new snapshot" in str(foreign.value)


def test_replay_too_old_cursor_beyond_tiny_window(hub, tmp_path, monkeypatch):
    store, revs = _history(hub, tmp_path, commits=5)
    monkeypatch.setattr(store_module, "REPLAY_WINDOW", 3)

    with pytest.raises(ReplayUnavailableError) as too_old:
        store.replay_states(revs[0])
    assert "fetch a new snapshot" in str(too_old.value)
    # Three transitions need four revisions; the oldest cursor is valid.
    page = store.replay_states(revs[1], limit=2)
    assert [sha for sha, _ in page.states] == revs[2:4]
    assert page.has_more is True
    tail = store.replay_states(revs[3], limit=2)
    assert [sha for sha, _ in tail.states] == revs[4:5]
    assert tail.has_more is False


# ---------------- strict argument validation ----------------


def test_replay_rejects_invalid_limit(hub, tmp_path):
    store, revs = _history(hub, tmp_path, commits=2)
    head = revs[-1]
    for bad in (0, -1, 33, True, False, "2", None, 2.0):
        with pytest.raises(ValidationError):
            store.replay_states(head, limit=bad)


def test_replay_argument_validation_precedes_git(hub, tmp_path, monkeypatch):
    store, (head,) = _history(hub, tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid replay arguments must not invoke Git")

    monkeypatch.setattr(store_module, "_run_git", forbidden)
    for cursor in ("short", "A" * 40, None, 42):
        with pytest.raises(ValidationError):
            store.replay_states(cursor)
    for limit in (True, 0, 33, "2"):
        with pytest.raises(ValidationError):
            store.replay_states(head, limit=limit)


def test_replay_reads_only_page_states_and_reuses_fetched_head(hub, tmp_path, monkeypatch):
    store, revs = _history(hub, tmp_path, commits=6)
    reads, enumerations = [], []
    real_read, real_git = store._read_state, store_module._run_git

    def read(revision):
        reads.append(revision)
        return real_read(revision)

    def run_git(args, **kwargs):
        if args[0] == "rev-list":
            enumerations.append(args)
        return real_git(args, **kwargs)

    monkeypatch.setattr(store, "_read_state", read)
    monkeypatch.setattr(store_module, "_run_git", run_git)
    store.replay_states(revs[0], limit=2)
    assert reads == [revs[-1], revs[0], revs[1], revs[2]]
    assert len(enumerations) == 1
    assert f"--max-count={store_module.REPLAY_WINDOW + 1}" in enumerations[0]
    reads.clear()
    store.replay_states(revs[-2], limit=2)
    assert reads == [revs[-1], revs[-2]]


# ---------------- corrupt history ----------------


def test_replay_rejects_corrupt_historical_state(hub, tmp_path):
    store, revs = _history(hub, tmp_path, commits=2)
    root, v1 = revs
    corrupt = _raw_publish(store, b"this is not json", parent=v1)
    head = _raw_publish(store, canonical_json_bytes(_state().to_dict()), parent=corrupt)

    with pytest.raises(ValidationError):
        store.replay_states(v1)  # the corrupt state is inside the returned page
    with pytest.raises(ValidationError):
        store.replay_states(corrupt)  # the corrupt state is the cursor itself


def test_replay_rejects_identity_change_in_history(hub, tmp_path):
    store, (root,) = _history(hub, tmp_path, commits=1)
    rogue = build_initial_state(session_id="rogue-9", target_version="v0.1 demo", base_commit=SHA0)
    head = _raw_publish(store, canonical_json_bytes(rogue.to_dict()), parent=root)

    with pytest.raises(ValidationError):
        store.replay_states(root)


# ---------------- malformed history shape ----------------


def test_replay_rejects_merge_history(hub, tmp_path):
    store, revs = _history(hub, tmp_path, commits=3)
    root, v1, v2 = revs
    merge = _merge_commit(store.store_path, str(hub), v2, v1)

    with pytest.raises(GitOperationError):
        store.replay_states(root)
    with pytest.raises(GitOperationError):
        store.replay_states(merge)  # corruption is reported even at the head cursor


def test_replay_rejects_noncontiguous_rev_list_output(hub, tmp_path, monkeypatch):
    store, revs = _history(hub, tmp_path, commits=2)
    head = revs[-1]
    real_run_git = store_module._run_git

    def fake_run_git(args, **kwargs):
        if args and args[0] == "rev-list":
            fabricated = f"{head} {'b' * 40}\n{'c' * 40} {'b' * 40}\n".encode()
            return subprocess.CompletedProcess(args, 0, fabricated, b"")
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(store_module, "_run_git", fake_run_git)
    with pytest.raises(GitOperationError):
        store.replay_states(head)
