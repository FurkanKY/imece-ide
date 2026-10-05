"""Two-client offline tests for the collab_runtime foundation (temp bare repos only).

Covers: init/join equality, guarded publication, stale rejection + recovery,
a true simultaneous publication race (monkeypatched _run_git around push,
client selected by cwd), updates landing mid-fetch, transport-failure
rollback, immutable fields, strict schema1 validation, advisory overlaps,
source-checkout immutability, and GIT_* environment scrubbing. No network,
no user repos, no model providers.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collab_runtime.errors import (  # noqa: E402
    CollabError,
    GitOperationError,
    ProjectStateError,
    SessionAlreadyExistsError,
    SessionNotFoundError,
    StaleRevisionError,
    ValidationError,
)
from collab_runtime.models import (  # noqa: E402
    MAX_JSON_BYTES,
    SessionState,
    Task,
    build_context,
    build_initial_state,
    build_task,
    canonical_json,
    canonical_json_bytes,
    compute_overlaps,
    context_hash_of,
    parse_json_bytes,
    parse_state_dict,
    scope_path,
    task_conflicts,
)
from collab_runtime.store import GitStore  # noqa: E402

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


def _make_source(tmp_path, name="src"):
    root = tmp_path / name
    root.mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@example.com"], root)
    (root / "foo.py").write_text("A\n", encoding="utf-8")
    (root / ".gitignore").write_text(".imece/\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "initial"], root)
    return root


def _store(tmp_path, name, hub):
    path = GitStore.create_bare(tmp_path / name, what="store")
    return GitStore(store=path, remote=str(hub))


def _state(session_id="demo-1", base_commit=SHA0):
    return build_initial_state(session_id=session_id, target_version="v0.1 demo", base_commit=base_commit)


def _context(goal="g", decisions=("d1",), interfaces={"A": "x"}):
    return build_context(goal=goal, decisions=list(decisions), interfaces=dict(interfaces))


def _task(tid="t-ui", *, context_revision=SHA0, status="queued", scopes=("src/ui/",), owner="alice"):
    return build_task(task_id=tid, owner=owner, goal="do ui", scopes=list(scopes), status=status,
                      context_revision=context_revision)


@pytest.fixture
def source(tmp_path):
    return _make_source(tmp_path)


@pytest.fixture
def hub(tmp_path):
    return GitStore.create_bare(tmp_path / "hub.git", what="hub")


def _bootstrap(hub, source, tmp_path, name):
    """Create a client store: initialize the session if absent, else join it
    (second client fetches the authoritative head instead of re-initializing)."""
    store = _store(tmp_path, name, hub)
    if store.remote_head() is None:
        return store, store.init_session(_state(base_commit=GitStore.project_head(source)))
    return store, store.fetch_state()[0]


# ---------------- init / join ----------------


def test_init_then_join_share_revision_base_and_context_hash(hub, source, tmp_path):
    a = _store(tmp_path, "a.store.git", hub)
    b = _store(tmp_path, "b.store.git", hub)
    base = GitStore.project_head(source)
    rev = a.init_session(_state(base_commit=base))

    rev_a, state_a = a.fetch_state()
    rev_b, state_b = b.fetch_state()
    assert rev_a == rev_b == rev
    assert state_a == state_b
    assert state_a.base_commit == state_b.base_commit == base
    assert state_a.context_hash == state_b.context_hash


def test_empty_hub_reports_not_found_then_already_exists(hub, tmp_path, source):
    store = _store(tmp_path, "s.git", hub)
    with pytest.raises(SessionNotFoundError):
        store.fetch_state()
    base = GitStore.project_head(source)
    rev = store.init_session(_state(base_commit=base))
    with pytest.raises(SessionAlreadyExistsError):
        store.init_session(_state(base_commit=base))
    assert store.fetch_state()[0] == rev


# ---------------- guarded publication / stale / recovery ----------------


def test_stale_expected_revision_rejected_then_recovery(hub, source, tmp_path):
    a, rev0 = _bootstrap(hub, source, tmp_path, "a.store.git")
    b, _ = _bootstrap(hub, source, tmp_path, "b.store.git")

    rev1 = a.update_context(_context(goal="new goal"), expected_revision=rev0)
    with pytest.raises(StaleRevisionError) as excinfo:
        b.update_context(_context(goal="conflicting"), expected_revision=rev0)
    assert "sync and retry" in str(excinfo.value)

    # Hub still carries A's update; nothing was lost.
    rev_b, state_b = b.fetch_state()
    assert rev_b == rev1 and state_b.context.goal == "new goal"
    # After syncing, B can publish again.
    rev2 = b.update_context(_context(goal="B after sync"), expected_revision=rev1)
    assert a.fetch_state()[0] == rev2


def test_expected_revision_is_required_and_validated(hub, source, tmp_path):
    a, rev = _bootstrap(hub, source, tmp_path, "a.store.git")
    with pytest.raises(TypeError):
        a.update_context(_context())  # missing keyword-only expected_revision
    with pytest.raises(TypeError):
        a.upsert_task(_task(context_revision=rev))  # same
    with pytest.raises(ValidationError):
        a.update_context(_context(), expected_revision="deadbeef")
    with pytest.raises(ValidationError):
        a.upsert_task(_task(context_revision=rev), expected_revision=rev.upper())


# ---------------- true publication race (monkeypatched push, cwd-selected) ----------------


def test_racing_publication_loser_rejected_and_mirror_rolled_back(hub, source, tmp_path, monkeypatch):
    a, rev0 = _bootstrap(hub, source, tmp_path, "a.store.git")
    b, _ = _bootstrap(hub, source, tmp_path, "b.store.git")
    assert b.fetch_state()[0] == rev0

    import collab_runtime.store as store_mod

    real_run_git = store_mod._run_git
    a_push_entered, b_published = threading.Event(), threading.Event()

    def run_git_with_parked_push(args, *, cwd, stdin=None, env=None, check=True):
        if args[0] == "push" and Path(cwd) == a.store_path:
            a_push_entered.set()
            assert b_published.wait(timeout=30)
        return real_run_git(args, cwd=cwd, stdin=stdin, env=env, check=check)

    monkeypatch.setattr(store_mod, "_run_git", run_git_with_parked_push)
    outcome: dict = {}

    def run_loser():
        try:
            outcome["loser"] = a.update_context(_context(goal="A wins"), expected_revision=rev0)
        except CollabError as exc:
            outcome["loser"] = exc

    thread = threading.Thread(target=run_loser)
    thread.start()
    assert a_push_entered.wait(timeout=30)

    # B completes a full publication while A sits inside its push dispatch.
    winner_rev = b.update_context(_context(goal="B wins"), expected_revision=rev0)
    b_published.set()
    thread.join(timeout=30)

    # The loser's push was rejected...
    assert isinstance(outcome["loser"], StaleRevisionError)
    # ...and its mirror was CAS-restored BEFORE any refetch: the rejected
    # commit never looks like a locally committed current pointer.
    assert a._local_head() == rev0
    rev, state = a.fetch_state()
    assert rev == winner_rev and state.context.goal == "B wins"
    assert b.fetch_state()[0] == winner_rev


def test_transport_failure_leaves_hub_and_mirror_unchanged(hub, source, tmp_path, monkeypatch):
    a, rev0 = _bootstrap(hub, source, tmp_path, "a.store.git")
    b, _ = _bootstrap(hub, source, tmp_path, "b.store.git")

    import collab_runtime.store as store_mod

    real_run_git = store_mod._run_git

    def broken_transport(args, *, cwd, stdin=None, env=None, check=True):
        if args[0] == "push" and Path(cwd) == a.store_path:
            return subprocess.CompletedProcess(
                args, returncode=1, stdout=b"", stderr=b"fatal: the remote end hung up unexpectedly"
            )
        return real_run_git(args, cwd=cwd, stdin=stdin, env=env, check=check)

    monkeypatch.setattr(store_mod, "_run_git", broken_transport)
    with pytest.raises(GitOperationError):
        a.update_context(_context(goal="must not land"), expected_revision=rev0)

    # Mirror restored to the prior head; hub still at the old revision.
    assert a._local_head() == rev0
    assert a.remote_head() == rev0
    rev_b, state_b = b.fetch_state()
    assert rev_b == rev0 and state_b.context.goal == ""


# ---------------- update landing mid-fetch (temp-ref read) ----------------


def test_fetch_tolerates_update_between_discovery_and_fetch(hub, source, tmp_path, monkeypatch):
    a, rev0 = _bootstrap(hub, source, tmp_path, "a.store.git")
    b, _ = _bootstrap(hub, source, tmp_path, "b.store.git")

    import collab_runtime.store as store_mod

    real_run_git = store_mod._run_git
    a_fetch_entered, b_published = threading.Event(), threading.Event()

    def run_git_with_parked_fetch(args, *, cwd, stdin=None, env=None, check=True):
        if args[0] == "fetch" and Path(cwd) == a.store_path:
            a_fetch_entered.set()
            assert b_published.wait(timeout=30)
        return real_run_git(args, cwd=cwd, stdin=stdin, env=env, check=check)

    monkeypatch.setattr(store_mod, "_run_git", run_git_with_parked_fetch)
    result: dict = {}

    def run_reader():
        try:
            result["fetched"] = a.fetch_state()
        except CollabError as exc:
            result["error"] = exc

    thread = threading.Thread(target=run_reader)
    thread.start()
    assert a_fetch_entered.wait(timeout=30)
    rev1 = b.update_context(_context(goal="mid-flight update"), expected_revision=rev0)
    b_published.set()
    thread.join(timeout=30)

    # No spurious GitOperationError: A observes the authoritative post-update
    # state because the read is taken from the ACTUAL fetched object.
    assert "error" not in result
    rev, state = result["fetched"]
    assert rev == rev1 and state.context.goal == "mid-flight update"
    assert a.remote_head() == rev1
    # Temporary fetch refs are always cleaned up.
    leftover = _git(["for-each-ref", "--format=%(refname)", "refs/imece-mirror/"], a.store_path).strip()
    assert leftover == ""


# ---------------- immutable fields + direct-dataclass defense ----------------


def test_publish_refuses_immutable_field_changes(hub, source, tmp_path):
    a, rev = _bootstrap(hub, source, tmp_path, "a.store.git")
    _, current = a.fetch_state()
    drifted_version = SessionState(current.session_id, "v2.0", current.base_commit, current.context, dict(current.tasks))
    rebased = SessionState(current.session_id, current.target_version, "b" * 40, current.context, dict(current.tasks))
    for mutated in (drifted_version, rebased):
        with pytest.raises(ValidationError):
            a.publish(mutated, expected_revision=rev)
    assert a.fetch_state() == (rev, current)  # hub unchanged


def test_invalid_direct_dataclass_never_reaches_the_hub(hub, source, tmp_path):
    a, rev = _bootstrap(hub, source, tmp_path, "a.store.git")
    _, current = a.fetch_state()
    poisoned = SessionState(
        current.session_id, current.target_version, current.base_commit, current.context,
        {"t1": Task("t1", "alice", "g", ("x.py",), "bogus-status", rev)},
    )
    with pytest.raises(ValidationError):
        a.publish(poisoned, expected_revision=rev)
    assert a.fetch_state() == (rev, current)
    assert a._local_head() == rev  # mirror untouched by the failed commit build


# ---------------- schema1 validation ----------------


def _mutate(fn):
    state = _state().to_dict()
    fn(state)
    with pytest.raises(ValidationError):
        parse_state_dict(state)


def test_state_validation_rejects_bad_fields():
    _mutate(lambda d: d.update(session_id="../evil"))
    _mutate(lambda d: d.update(session_id=""))
    _mutate(lambda d: d.update(session_id=5))
    _mutate(lambda d: d.update(base_commit="A" * 40))
    _mutate(lambda d: d.update(base_commit="a" * 39))
    _mutate(lambda d: d.update(target_version="x" * 201))
    _mutate(lambda d: d.update(schema=2))
    _mutate(lambda d: d.update(schema=True))  # bool is not schema int 1
    _mutate(lambda d: d.update(extra="nope"))  # unknown top-level field
    _mutate(lambda d: d["context"].update(extra="nope"))
    _mutate(lambda d: d["context"].update(goal="x" * 4001))
    _mutate(lambda d: d["context"].update(decisions=["d"] * 65))
    _mutate(lambda d: d["context"].update(decisions="not-a-list"))
    _mutate(lambda d: d["context"].update(interfaces={"bad key!": "v"}))
    _mutate(lambda d: d["context"].update(interfaces=["not", "a", "dict"]))
    _mutate(lambda d: d.update(tasks=[]))  # tasks must be an object
    _mutate(lambda d: d.pop("context"))  # missing required field
    _mutate(lambda d: d.update(tasks={"t1": {"owner": "a b", "goal": "g", "scopes": ["x.py"],
                                             "status": "queued", "context_revision": SHA0}}))
    _mutate(lambda d: d.update(tasks={"t1": {"owner": "a", "goal": "g", "scopes": ["x.py"],
                                             "status": "paused", "context_revision": SHA0}}))
    _mutate(lambda d: d.update(tasks={"t1": {"owner": "a", "goal": "g", "scopes": ["x.py"],
                                             "status": "queued", "context_revision": "z" * 40}}))
    _mutate(lambda d: d.update(tasks={"t1": {"owner": "a", "goal": "g", "scopes": ["x.py"],
                                             "status": "queued", "context_revision": SHA0, "extra": 1}}))

    def _many_tasks(d):
        d["tasks"] = {
            f"t{i}": {"owner": "a", "goal": "g", "scopes": ["x.py"], "status": "queued",
                      "context_revision": SHA0}
            for i in range(257)
        }
    _mutate(_many_tasks)


def test_parser_type_guards_reject_wrong_json_types():
    _mutate(lambda d: d.update(context=None))
    _mutate(lambda d: d.update(context=5))
    _mutate(lambda d: d.update(tasks=None))
    _mutate(lambda d: d.update(tasks=5))
    _mutate(lambda d: d["context"].update(interfaces=None))
    _mutate(lambda d: d["context"].update(interfaces=5))
    _mutate(lambda d: d["context"].update(decisions=None))
    _mutate(lambda d: d.update(tasks={"t1": None}))
    _mutate(lambda d: d.update(tasks={"t1": {"owner": "a", "goal": "g", "scopes": None,
                                             "status": "queued", "context_revision": SHA0}}))


def test_ids_and_shas_reject_newline_suffix():
    with pytest.raises(ValidationError):
        build_initial_state(session_id="demo-1\n", target_version="v", base_commit=SHA0)
    with pytest.raises(ValidationError):
        build_initial_state(session_id="demo-1", target_version="v", base_commit=SHA0 + "\n")
    with pytest.raises(ValidationError):
        build_task(task_id="t\n", owner="a", goal="g", scopes=["x.py"], status="queued",
                   context_revision=SHA0)
    with pytest.raises(ValidationError):
        build_task(task_id="t", owner="a", goal="g", scopes=["x.py"], status="queued",
                   context_revision=SHA0 + "\n")


def test_json_hostile_input_becomes_validation_error():
    with pytest.raises(ValidationError):
        parse_json_bytes(b'{"schema":NaN}', what="state")
    with pytest.raises(ValidationError):
        parse_json_bytes(b'{"schema":1,"x":Infinity}', what="state")
    with pytest.raises(ValidationError):
        parse_json_bytes(b'{"a":"\\ud800"}', what="state")  # unpaired surrogate escape
    old_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(max(old_limit, 100_000))
        with pytest.raises(ValidationError):
            parse_json_bytes(b"[" * 20000 + b"]" * 20000, what="state")
    finally:
        sys.setrecursionlimit(old_limit)


def test_json_maximum_depth_boundary_and_string_escaping():
    from collab_runtime.models import MAX_JSON_DEPTH

    # Quotes and brackets within the string (with escaped quotes/backslashes)
    # must not contribute to structural nesting.
    for content in ('{[ "quote" ]} \\ end', '\\"[{}]'):
        string_value = json.dumps(content).encode("utf-8")
        at_limit = b"[" * MAX_JSON_DEPTH + string_value + b"]" * MAX_JSON_DEPTH
        assert parse_json_bytes(at_limit, what="depth probe")
    with pytest.raises(ValidationError):
        parse_json_bytes(
            b"[" * (MAX_JSON_DEPTH + 1) + b"0" + b"]" * (MAX_JSON_DEPTH + 1),
            what="depth probe",
        )


def test_builders_validate_raw_types_without_coercion():
    with pytest.raises(ValidationError):
        build_context(goal="g", decisions="abc", interfaces={})
    with pytest.raises(ValidationError):
        build_context(goal="g", decisions=5, interfaces={})
    with pytest.raises(ValidationError):
        build_context(goal="g", decisions=[], interfaces=[("A", "x")])
    with pytest.raises(ValidationError):
        build_context(goal="g", decisions=[], interfaces="AB")
    with pytest.raises(ValidationError):
        build_task(task_id="t", owner="a", goal="g", scopes="src/x.py", status="queued",
                   context_revision=SHA0)
    with pytest.raises(ValidationError):
        build_task(task_id="t", owner="a", goal="g", scopes=5, status="queued",
                   context_revision=SHA0)


@pytest.mark.parametrize("bad", [
    "../x", "/abs/path", "C:\\tmp\\x", "src/*.py", "src/[a-b].py", "src/a?.py",
    "a/../../b", ".git/config", ".GIT/config", "", ".", "x?y", "x" * 513, "\x00x",
])
def test_scope_validation_rejects_unsafe(bad):
    with pytest.raises(ValidationError):
        scope_path(bad)


@pytest.mark.parametrize("good", [
    "src/main.py", "src/ui/", "README.md", "a/b/c/", "ns/file.txt",
    "dir.with.dots/", "under_score-1/", "UPPER/File.PY",
])
def test_scope_validation_accepts_safe(good):
    assert scope_path(good) == good


def test_duplicate_json_keys_and_oversize_input_rejected():
    with pytest.raises(ValidationError):
        parse_json_bytes(b'{"schema":1,"schema":2}', what="state")
    with pytest.raises(ValidationError):
        parse_json_bytes(b'{"schema":1} trailing', what="state")
    with pytest.raises(ValidationError):
        parse_json_bytes(b"\xff\xfe not utf8", what="state")
    with pytest.raises(ValidationError):
        parse_json_bytes(b"x" * (MAX_JSON_BYTES + 1), what="state")


def test_oversize_state_rejected_before_commit_and_hub_unchanged(hub, source, tmp_path):
    a, rev = _bootstrap(hub, source, tmp_path, "a.store.git")
    big = build_context(goal="", decisions=["d" * 1800] * 64, interfaces={})
    _, current = a.fetch_state()
    assert len(canonical_json_bytes(current.with_context(big).to_dict())) > MAX_JSON_BYTES
    with pytest.raises(ValidationError):
        a.update_context(big, expected_revision=rev)
    assert a.fetch_state()[1].context.goal == ""  # hub unchanged


def test_canonical_json_deterministic_and_state_roundtrips():
    c1 = {"interfaces": {"B": "2", "A": "1"}, "goal": "g", "decisions": ["d"]}
    c2 = {"goal": "g", "decisions": ["d"], "interfaces": {"A": "1", "B": "2"}}
    assert canonical_json(c1) == canonical_json(c2)
    assert context_hash_of(c1) == context_hash_of(c2)

    state = _state().with_task(_task(context_revision=SHA0))
    reordered = dict(reversed(list(state.to_dict().items())))
    assert canonical_json(reordered) == canonical_json(state.to_dict())
    assert parse_state_dict(reordered) == parse_state_dict(state.to_dict()) == state


# ---------------- advisory overlaps ----------------


def test_active_task_overlaps_are_advisory_and_deterministic(hub, source, tmp_path):
    a, rev = _bootstrap(hub, source, tmp_path, "a.store.git")
    tasks = [
        _task("t-ui", context_revision=rev, scopes=("src/ui/",)),
        _task("t-uifile", context_revision=rev, scopes=("src/ui/main.py",), owner="bob"),
        _task("t-api", context_revision=rev, scopes=("src/api/",), owner="carol"),
        _task("t-done", context_revision=rev, status="done", scopes=("src/ui/",), owner="dan"),
    ]
    current = rev
    for task in tasks:
        current = a.upsert_task(task, expected_revision=current)

    _, state = a.fetch_state()
    # A "done" task never overlaps; the directory/file pair shares the dir prefix.
    assert compute_overlaps(state.tasks) == [{"tasks": ["t-ui", "t-uifile"], "shared": ["src/ui/"]}]
    conflicts = task_conflicts(state.tasks)
    assert conflicts["t-ui"] == ["t-uifile"]
    assert conflicts["t-done"] == [] and conflicts["t-api"] == []
    # Task keeps the revision it was assigned at.
    assert state.tasks["t-ui"].context_revision == rev


# ---------------- source checkout immutability ----------------


def test_source_checkout_head_index_refs_status_untouched(hub, source, tmp_path):
    (source / "staged.py").write_text("S\n", encoding="utf-8")
    _git(["add", "staged.py"], source)
    (source / "foo.py").write_text("B2\n", encoding="utf-8")
    (source / "untracked.py").write_text("U\n", encoding="utf-8")
    imece = source / ".imece"
    imece.mkdir()
    (imece / "note.txt").write_text("ignored\n", encoding="utf-8")

    before_head = _git(["rev-parse", "HEAD"], source).strip()
    before_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], source).strip()
    before_status = _git(["status", "--porcelain=v1"], source)
    before_config = _git(["config", "--local", "--list"], source)
    before_refs = _git(["for-each-ref", "--format=%(refname) %(objectname)"], source).splitlines()

    hub2 = GitStore.create_bare(tmp_path / "hub2.git", what="hub")
    store_path = GitStore.ensure_outside_project(tmp_path / "store2.git", source, what="store")
    GitStore.create_bare(store_path, what="store")
    store = GitStore(store=store_path, remote=str(hub2))

    base = GitStore.project_head(source)
    assert base == before_head  # dirty contents are never read or uploaded
    rev = store.init_session(_state(base_commit=base))
    rev = store.update_context(_context(goal="x"), expected_revision=rev)
    store.upsert_task(_task(context_revision=rev), expected_revision=rev)
    store.fetch_state()

    assert _git(["rev-parse", "HEAD"], source).strip() == before_head
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], source).strip() == before_branch
    assert _git(["status", "--porcelain=v1"], source) == before_status
    assert _git(["config", "--local", "--list"], source) == before_config
    assert _git(["for-each-ref", "--format=%(refname) %(objectname)"], source).splitlines() == before_refs


def test_store_paths_inside_project_are_refused(source):
    with pytest.raises(ValidationError):
        GitStore.ensure_outside_project(source / "nested" / "store.git", source, what="store")
    with pytest.raises(ValidationError):
        GitStore.ensure_outside_project(source, source, what="hub")


def test_project_head_requires_committed_repo(tmp_path):
    empty = tmp_path / "notrepo"
    empty.mkdir()
    with pytest.raises(ProjectStateError):
        GitStore.project_head(empty)
    no_commit = tmp_path / "nocommit"
    no_commit.mkdir()
    _git(["init", "-q"], no_commit)
    with pytest.raises(ProjectStateError):
        GitStore.project_head(no_commit)


# ---------------- hub/store placement + remote validation ----------------


def test_create_bare_refuses_nonempty_and_files(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "x").write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError):
        GitStore.create_bare(occupied, what="hub")
    filepath = tmp_path / "afile"
    filepath.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError):
        GitStore.create_bare(filepath, what="hub")
    nested = tmp_path / "deep" / "hub.git"
    assert GitStore.create_bare(nested, what="hub").is_dir()


def test_only_local_bare_remotes_accepted(tmp_path, source):
    normal = tmp_path / "normal-repo"
    normal.mkdir()
    _git(["init", "-q"], normal)
    store_path = GitStore.create_bare(tmp_path / "s.git", what="store")
    with pytest.raises(ValidationError):
        GitStore(store=source, remote=str(source))  # non-bare store
    with pytest.raises(ValidationError):
        GitStore(store=store_path, remote=str(normal))  # non-bare remote
    for bad_remote in ("ssh://git@host/repo.git", "https://host/repo.git",
                       "git://host/repo.git", "git@host:repo.git", "host:repo", ""):
        with pytest.raises(ValidationError):
            GitStore(store=store_path, remote=bad_remote)  # URL/scp-like rejected


def test_store_and_hub_must_be_distinct_paths(tmp_path, source):
    hub = GitStore.create_bare(tmp_path / "hub.git", what="hub")
    # Aliasing store == hub is rejected in the constructor BEFORE anything is
    # written: sharing one path would let publication CAS the authoritative
    # hub ref directly and bypass remote rejection (lost updates).
    with pytest.raises(ValidationError):
        GitStore(store=hub, remote=str(hub))
    with pytest.raises(ValidationError):
        GitStore(store=hub, remote=str(tmp_path / "." / "hub.git"))  # resolves to the same dir
    assert not (hub / "refs" / "heads" / "imece-session").exists()
    GitStore.require_bare(hub, "hub")  # hub still valid and untouched


def test_require_bare_rejects_nested_dirs_inside_a_bare_repo(tmp_path):
    hub = GitStore.create_bare(tmp_path / "hub.git", what="hub")
    (hub / "nesteddir").mkdir()
    for candidate in (hub / "nesteddir", hub / "objects"):
        # Whether or not Git discovery resolves these into hub.git, the path
        # must be rejected: it is not the bare repository root itself.
        with pytest.raises(ValidationError):
            GitStore.require_bare(candidate, "store")
    GitStore.require_bare(hub, "store")  # the exact root is accepted


# ---------------- GIT_* environment scrubbing ----------------


def test_git_env_overrides_are_scrubbed(source, tmp_path, monkeypatch):
    expected_head = _git(["rev-parse", "HEAD"], source).strip()  # captured pre-poison
    decoy = GitStore.create_bare(tmp_path / "decoy.git", what="hub")
    monkeypatch.setenv("GIT_DIR", str(decoy))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(decoy / "objects"))
    monkeypatch.setenv("GIT_TRACE", "1")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(decoy))
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/nonexistent")

    # HEAD must still resolve against the SOURCE repo, not the decoy.
    assert GitStore.project_head(source) == expected_head

    # A full metadata flow works under the poisoned environment and writes
    # the intended hub: default hash (not the poisoned sha256), no borrowed
    # templates or hooks.
    hub3 = GitStore.create_bare(tmp_path / "hub3.git", what="hub")
    store = GitStore(store=GitStore.create_bare(tmp_path / "s3.git", what="store"), remote=str(hub3))
    rev = store.init_session(_state(session_id="scrub", base_commit=expected_head))
    assert store.fetch_state()[0] == rev
    assert (hub3 / "refs" / "heads" / "imece-session").exists()
    hooks = hub3 / "hooks"
    assert not hooks.exists() or not any(hooks.iterdir())


# ---------------- store regressions (slice 2 helpers) ----------------


def test_task_context_revision_must_be_published_history(hub, source, tmp_path):
    store, rev = _bootstrap(hub, source, tmp_path, "a.store.git")
    # A revision outside this session's published history is rejected.
    with pytest.raises(ValidationError):
        store.upsert_task(_task(context_revision="f" * 40), expected_revision=rev)
    # The init revision itself (the head) is accepted.
    rev1 = store.upsert_task(_task(context_revision=rev), expected_revision=rev)
    assert store.fetch_state()[0] == rev1


def test_context_hash_at_reports_only_real_provenance(hub, source, tmp_path):
    store, rev0 = _bootstrap(hub, source, tmp_path, "a.store.git")
    rev1 = store.upsert_task(_task(context_revision=rev0), expected_revision=rev0)
    rev2 = store.update_context(_context(goal="changed goal"), expected_revision=rev1)

    empty_hash = build_context(goal="", decisions=[], interfaces={}).content_hash
    # Provenance content hash: the context AT each historical revision.
    assert store.context_hash_at(rev0, head=rev2) == empty_hash
    assert store.context_hash_at(rev1, head=rev2) == empty_hash
    assert store.context_hash_at(rev2, head=rev2) == _context(goal="changed goal").content_hash
    # Missing/foreign provenance is None — never silently valid evidence.
    assert store.context_hash_at("f" * 40, head=rev2) is None
    assert store.context_hash_at("zzz", head=rev2) is None


def test_context_hash_at_rejects_oversize_history_blob(hub, source, tmp_path):
    """A >64KiB session.json planted directly in store history is rejected by
    the same bounded reader used for the live state — never streamed."""
    from collab_runtime.models import MAX_JSON_BYTES

    store, _rev = _bootstrap(hub, source, tmp_path, "a.store.git")
    huge = b'{"schema":1,"goal":"' + b"x" * (MAX_JSON_BYTES + 1) + b'"}'
    env = _env()
    cp = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(store.store_path),
        input=huge, env=env, capture_output=True, timeout=30,
    )
    assert cp.returncode == 0
    blob = cp.stdout.decode("ascii").strip()
    cp = subprocess.run(
        ["git", "mktree"], cwd=str(store.store_path), env=env,
        input=f"100644 blob {blob}\tsession.json\n".encode(), capture_output=True, timeout=30,
    )
    assert cp.returncode == 0
    tree = cp.stdout.decode("ascii").strip()
    commit = _git(["commit-tree", tree, "-m", "oversize"], store.store_path).strip()
    assert store.context_hash_at(commit, head=commit) is None


def test_validate_remote_windows_drive_paths(monkeypatch, tmp_path):
    import collab_runtime.store as store_mod

    # On Windows, a real absolute drive path is a LOCAL path, not scp-like:
    # the scp-like rejection is skipped (the path then fails its normal
    # bare-repository existence check instead).
    monkeypatch.setattr(store_mod, "_is_windows", lambda: True)
    with pytest.raises(ValidationError, match="must be a directory"):
        store_mod.validate_remote(r"C:\repos\hub.git")
    # Protocol and scp-like remotes stay rejected on Windows too.
    for bad in ("ssh://host/repo.git", "https://host/repo.git", "git@host:repo.git"):
        with pytest.raises(ValidationError, match="only local directory hub paths"):
            store_mod.validate_remote(bad)

    # On POSIX, drive paths keep being rejected as scp-like.
    monkeypatch.setattr(store_mod, "_is_windows", lambda: False)
    with pytest.raises(ValidationError, match="only local directory hub paths"):
        store_mod.validate_remote(r"C:\repos\hub.git")
