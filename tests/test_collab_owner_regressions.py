"""Owner-side session lifecycle regressions.

Real temporary Git repositories and a real loopback listener on 127.0.0.1 are
used everywhere. Fake servers appear only where a failure or a blocked drain
cannot otherwise be produced deterministically (start/stop lifecycle faults).
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from collab_runtime import owner as owner_module
from collab_runtime.client import LoopbackSnapshotClient
from collab_runtime.coordinator import Coordinator, Snapshot
from collab_runtime.host import CollaborationHost
from collab_runtime.models import SessionState, SharedContext, Task
from collab_runtime.owner import OwnerError, OwnerSessionManager
from collab_runtime.store import GitStore
from collab_runtime.transport import LoopbackServer


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def is_sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def _symlinks_available() -> bool:
    """Symlink creation needs elevation on Windows; probe it once per session."""
    probe = tempfile.mkdtemp(prefix="imece-symlink-probe-")
    try:
        os.symlink("target", os.path.join(probe, "link"))
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root: Path, message: str = "change") -> str:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", message],
                   check=True)
    return git(root, "rev-parse", "HEAD")


def project(base: Path, name: str = "project") -> tuple[Path, str]:
    base.mkdir(parents=True, exist_ok=True)
    root = base / name
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "base"],
                   check=True)
    return root, git(root, "rev-parse", "HEAD")


def request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": "demo", "target_version": "v1", "goal": "A shared goal",
        "owner_id": "alice", "member_ids": ["alice", "bob"],
        "tasks": [{"id": "task-a", "owner": "bob", "goal": "Implement safely",
                   "scopes": ["src/"]}],
    }
    payload.update(overrides)
    return payload


def source_manifest(root: Path) -> dict[str, Any]:
    """Everything a read-only source binding must not disturb."""
    files: dict[str, Any] = {}
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


def configured(root: Path, private: Path, **overrides: Any) -> OwnerSessionManager:
    manager = OwnerSessionManager(private)
    preview = manager.preview_create(root, **request(**overrides))
    manager.create(preview["previewId"], root)
    return manager


class FailingStore:
    """Real GitStore whose `upsert_task` fails only after `fail_after` writes."""

    def __init__(self, inner: GitStore, fail_after: int) -> None:
        self._inner = inner
        self.fail_after = fail_after
        self.upsert_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def upsert_task(self, task: Task, *, expected_revision: str) -> str:
        self.upsert_calls += 1
        if self.upsert_calls > self.fail_after:
            raise OSError("publisher exploded after init_session")
        return self._inner.upsert_task(task, expected_revision=expected_revision)


class SwitchableStore:
    """Store factory that can be switched between real and failing publishes."""

    def __init__(self) -> None:
        self.fail_after: int | None = None
        self.instances: list[Any] = []

    def __call__(self, **kwargs: Any) -> Any:
        inner = GitStore(**kwargs)
        if self.fail_after is None:
            self.instances.append(inner)
            return inner
        wrapper = FailingStore(inner, self.fail_after)
        self.instances.append(wrapper)
        return wrapper


class FakeServer:
    """Loopback stand-in used only for failure/blocked-drain lifecycle cases."""

    def __init__(self, port: int = 0, *, start_error: Exception | None = None,
                 close_errors: int = 0, block_close: bool = True) -> None:
        self._port = port
        self._start_error = start_error
        self._close_errors = close_errors
        self._block_close = block_close
        self.started = False
        self.closed = False
        self.close_calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def start(self) -> "FakeServer":
        if self._start_error is not None:
            raise self._start_error
        self.started = True
        return self

    def close(self) -> None:
        self.close_calls += 1
        self.entered.set()
        if self._block_close:
            self.release.wait(15)
        if self._close_errors:
            self._close_errors -= 1
            raise OSError("listener could not be released")
        self.closed = True


class FakeCoordinator:
    def __init__(self, store: Any, *, session_id: str, owner_id: str,
                 member_credentials: dict[str, str]) -> None:
        self.store = store
        self.session_id = session_id
        self.owner_id = owner_id
        self.member_credentials = member_credentials
        self.closed = False

    def close(self) -> None:
        self.closed = True


def blocked_behind(queued: threading.Event, thread: threading.Thread) -> None:
    """Prove `thread` started and is still blocked behind an in-flight drain.

    The drain holds the manager's operation lock until its fake close is
    released, so an alive follower cannot have completed yet. No sleep and no
    assumption about Lock scheduling is involved.
    """
    assert queued.wait(3), "the follower never started"
    assert thread.is_alive()


def wait_for(predicate: Any, message: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(message)


def port_of(endpoint: str) -> int:
    return int(endpoint.rsplit(":", 1)[1])


# --------------------------------------------------------------------------
# 1. inert preview + non-mutating create
# --------------------------------------------------------------------------


def test_preview_writes_nothing_and_create_leaves_the_source_checkout_untouched(tmp_path):
    root, _ = project(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print(1)\n")
    git(root, "add", "-A")
    commit(root, "seed")
    head = git(root, "rev-parse", "HEAD")
    (root / "src" / "app.py").write_text("print(1)\nprint(2)  # unstaged WIP\n")
    (root / "notes.txt").write_text("uncommitted\n")
    git(root, "add", "notes.txt")
    before = source_manifest(root)
    assert before["status"]  # dirty index and worktree are part of the fixture

    private = tmp_path / "private"
    manager = OwnerSessionManager(private)
    preview = manager.preview_create(root, **request())

    assert set(preview) == {"previewId", "projectRoot", "sessionId", "targetVersion",
                            "baseCommit", "goal", "ownerId", "memberIds", "tasks",
                            "mode", "warnings"}
    assert preview["mode"] == "create" and preview["warnings"]
    assert preview["baseCommit"] == head
    assert not private.exists() and manager.status()["state"] == "unconfigured"
    assert source_manifest(root) == before

    result = manager.create(preview["previewId"], root)
    assert result["state"] == "configured"
    assert result["storePath"].startswith(str(private)) and result["hubPath"].startswith(str(private))
    assert result["sessionId"] == "demo" and result["baseCommit"] == head
    assert result["goal"] == "A shared goal" and result["memberIds"] == ["alice", "bob"]
    assert is_sha(result["revision"])
    task = result["tasks"][0]
    assert task["id"] == "task-a" and task["owner"] == "bob" and task["status"] == "queued"
    assert is_sha(task["contextRevision"])

    namespaces = list(private.iterdir())
    assert len(namespaces) == 1
    assert sorted(path.name for path in namespaces[0].iterdir()) == ["hub.git", "store.git"]
    assert source_manifest(root) == before

    # The published task carries the real initial goal revision, not the
    # preview placeholder, and it is an ancestor of the fetched head.
    store = GitStore(store=result["storePath"], remote=result["hubPath"])
    head_revision, state = store.fetch_state()
    assert state.tasks["task-a"].context_revision == task["contextRevision"]
    assert task["contextRevision"] != "0" * 40
    assert store.is_ancestor(task["contextRevision"], head_revision)
    assert state.context.goal == "A shared goal"


# --------------------------------------------------------------------------
# 2. every bad metadata field is rejected before any directory exists
# --------------------------------------------------------------------------


@pytest.mark.parametrize("overrides", [
    {"member_ids": ["alice", "alice"]},
    {"member_ids": ["bob", "bob"]},
    {"member_ids": []},
    {"member_ids": ["alice", "mallory"]},
    {"owner_id": "carol"},
    {"owner_id": "not a safe id"},
    {"session_id": "../escape"},
    {"session_id": ""},
    {"target_version": "x" * 5000},
    {"goal": "\x00control"},
    {"tasks": [{"id": "task-a", "owner": "mallory", "goal": "g", "scopes": ["src/"]}]},
    {"tasks": [{"id": "task-a", "owner": "bob", "goal": "g", "scopes": ["../escape/"]}]},
    {"tasks": [{"id": "task-a", "owner": "bob", "goal": "g", "scopes": ["/abs"]}]},
    {"tasks": [{"id": "task-a", "owner": "bob", "goal": "g", "scopes": ["src/"], "extra": 1}]},
    {"tasks": [{"id": "task-a", "owner": "bob", "goal": "g"}]},
    {"tasks": [{"id": "task-a", "owner": "bob", "goal": "g", "scopes": ["src/"], "status": "done"}]},
    {"tasks": [{"id": "t", "owner": "bob", "goal": "g", "scopes": ["a/"]},
               {"id": "t", "owner": "bob", "goal": "g", "scopes": ["b/"]}]},
    {"tasks": [{"id": f"t{index}", "owner": "bob", "goal": "g", "scopes": ["src/"]}
               for index in range(257)]},
    {"tasks": "not-a-list"},
])
def test_bad_metadata_is_refused_before_any_private_directory_exists(tmp_path, overrides):
    root, _ = project(tmp_path)
    private = tmp_path / "private"
    manager = OwnerSessionManager(private)
    with pytest.raises(OwnerError):
        manager.preview_create(root, **request(**overrides))
    assert not private.exists()
    assert manager.status()["state"] == "unconfigured"


def test_non_project_and_non_toplevel_roots_are_refused_without_writes(tmp_path):
    root, _ = project(tmp_path)
    nested = root / "pkg" / "deep"
    nested.mkdir(parents=True)
    private = tmp_path / "private"
    manager = OwnerSessionManager(private)
    with pytest.raises(OwnerError, match="invalid_project_root"):
        manager.preview_create(nested, **request())
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(OwnerError, match="invalid_project"):
        manager.preview_create(plain, **request())
    with pytest.raises(OwnerError, match="invalid_project"):
        manager.preview_create(tmp_path / "absent", **request())
    assert not private.exists()


# --------------------------------------------------------------------------
# 3. previews expire and are one-shot
# --------------------------------------------------------------------------


def test_expired_preview_is_refused_before_touching_the_private_root(tmp_path, monkeypatch):
    root, _ = project(tmp_path)
    private = tmp_path / "private"
    manager = OwnerSessionManager(private)
    clock = [1_000.0]
    monkeypatch.setattr(owner_module, "_monotonic", lambda: clock[0])

    preview = manager.preview_create(root, **request())
    clock[0] += 299.0
    assert manager.preview_create(root, **request())["previewId"] != preview["previewId"]

    clock[0] += 3.0  # the first preview is now past the 300s TTL
    with pytest.raises(OwnerError, match="preview_stale"):
        manager.create(preview["previewId"], root)
    assert not private.exists()
    assert manager.status()["state"] == "unconfigured"


def test_duplicate_create_is_refused_and_never_overwrites_the_existing_namespace(tmp_path):
    root, _ = project(tmp_path)
    private = tmp_path / "private"
    manager = OwnerSessionManager(private)
    preview = manager.preview_create(root, **request())
    first = manager.create(preview["previewId"], root)
    namespaces = sorted(path.name for path in private.iterdir())

    with pytest.raises(OwnerError):
        manager.create(preview["previewId"], root)

    assert sorted(path.name for path in private.iterdir()) == namespaces
    status = manager.status()
    assert status["state"] == "configured"
    assert status["storePath"] == first["storePath"] and status["hubPath"] == first["hubPath"]
    _revision, state = GitStore(store=first["storePath"], remote=first["hubPath"]).fetch_state()
    assert state.session_id == "demo" and sorted(state.tasks) == ["task-a"]


# --------------------------------------------------------------------------
# 4. private root hardening
# --------------------------------------------------------------------------


def test_private_root_rejects_symlinked_ancestor_permissive_root_and_loose_mode(tmp_path):
    root, _ = project(tmp_path)
    before = source_manifest(root)

    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    if _symlinks_available():
        link = tmp_path / "link"
        link.symlink_to(real)
        through_link = OwnerSessionManager(link / "private")
        preview = through_link.preview_create(root, **request())
        with pytest.raises(OwnerError, match="unsafe_private_root"):
            through_link.create(preview["previewId"], root)
        assert list(real.iterdir()) == []

    if os.name == "posix":
        # Mode-based refusals are a POSIX capability: Windows reports no usable
        # owner-mode triple (production gates the very same check), so a 0777
        # directory is not evidence of anything there.
        permissive = tmp_path / "permissive"
        permissive.mkdir(mode=0o777)
        wide = OwnerSessionManager(permissive)
        preview = wide.preview_create(root, **request())
        with pytest.raises(OwnerError, match="unsafe_private"):
            wide.create(preview["previewId"], root)
        assert list(permissive.iterdir()) == []

        loose = tmp_path / "loose"
        loose.mkdir(mode=0o755)
        narrow = OwnerSessionManager(loose)
        preview = narrow.preview_create(root, **request())
        with pytest.raises(OwnerError, match="unsafe_private"):
            narrow.create(preview["previewId"], root)
        assert list(loose.iterdir()) == []

    inside_project = OwnerSessionManager(root / "owners")
    with pytest.raises(OwnerError, match="unsafe_private_root"):
        inside_project.preview_create(root, **request())
    assert not (root / "owners").exists()
    assert source_manifest(root) == before


def test_occupied_namespace_is_refused_and_never_publishes_into_it(tmp_path, monkeypatch):
    root, _ = project(tmp_path)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(owner_module.uuid, "uuid4", lambda: "00000000-0000-0000-0000-00000000cafe")
    occupied = private / "00000000-0000-0000-0000-00000000cafe"
    occupied.mkdir()
    (occupied / "stray.txt").write_text("someone else owns this\n")

    manager = OwnerSessionManager(private)
    preview = manager.preview_create(root, **request())
    with pytest.raises(OwnerError):
        manager.create(preview["previewId"], root)

    assert sorted(path.name for path in occupied.iterdir()) == ["stray.txt"]
    assert (occupied / "stray.txt").read_text() == "someone else owns this\n"
    status = manager.status()
    assert status["state"] == "creation_failed" and status["retryRequired"] is True
    assert status["storePath"] is None and status["sessionId"] is None and status["tasks"] == []


# --------------------------------------------------------------------------
# 5. select_existing refusals and one honest binding
# --------------------------------------------------------------------------


def test_select_existing_rejects_mismatched_head_unusable_paths_and_urls(tmp_path):
    root, _ = project(tmp_path)
    baseline = configured(root, tmp_path / "private")
    live = baseline.status()

    moved, _ = project(tmp_path / "moved")
    commit(moved, "advanced")
    stale = OwnerSessionManager(tmp_path / "stale")
    with pytest.raises(OwnerError, match="source_head_mismatch"):
        stale.select_existing(moved, store_path=Path(live["storePath"]),
                              hub_path=Path(live["hubPath"]),
                              owner_id="alice", member_ids=["alice", "bob"])
    assert stale.status()["state"] == "unconfigured"

    cases = {
        "remote_url": ("https://example.invalid/hub.git", live["hubPath"]),
        "absent_store": (tmp_path / "absent.git", live["hubPath"]),
        "same_path": (live["storePath"], live["storePath"]),
        "hub_as_store": (live["hubPath"], live["hubPath"]),
        "source_checkout": (str(root), live["hubPath"]),
        "nested_in_hub": (str(Path(live["hubPath"]) / "nested"), live["hubPath"]),
    }
    for label, (store_path, hub_path) in cases.items():
        manager = OwnerSessionManager(tmp_path / f"private-{label}")
        with pytest.raises(OwnerError):
            manager.select_existing(root, store_path=Path(store_path),
                                    hub_path=Path(hub_path),
                                    owner_id="alice", member_ids=["alice", "bob"])
        status = manager.status()
        assert status["state"] == "unconfigured", label
        assert status["storePath"] is None and status["endpoint"] is None, label


def test_select_existing_binds_a_real_hub_only_to_a_matching_local_bare_pair(tmp_path):
    root, _ = project(tmp_path)
    created = configured(root, tmp_path / "private")

    selected = OwnerSessionManager(tmp_path / "other-private")
    result = selected.select_existing(root, store_path=Path(created.status()["storePath"]),
                                      hub_path=Path(created.status()["hubPath"]),
                                      owner_id="alice", member_ids=["alice", "bob"])
    assert result["state"] == "configured" and result["sessionId"] == "demo"
    assert is_sha(result["revision"])
    assert result["tasks"][0]["id"] == "task-a" and is_sha(result["tasks"][0]["contextRevision"])
    started = selected.start(root)
    assert started["endpoint"].startswith("http://127.0.0.1:")
    revealed = selected.reveal_member_once("bob")
    snapshot = LoopbackSnapshotClient(revealed["endpoint"],
                                      credential=revealed["credential"]).snapshot()
    assert snapshot.state.session_id == "demo"
    assert snapshot.state.base_commit == result["baseCommit"]
    selected.stop()


# --------------------------------------------------------------------------
# 6. partial creation failure
# --------------------------------------------------------------------------


def test_task_publish_failure_reports_owned_paths_without_auto_cleanup(tmp_path):
    root, _ = project(tmp_path)
    private = tmp_path / "private"
    before = source_manifest(root)
    factory = SwitchableStore()
    factory.fail_after = 1

    manager = OwnerSessionManager(private, store_factory=factory)
    preview = manager.preview_create(root, **request(tasks=[
        {"id": "task-a", "owner": "bob", "goal": "first", "scopes": ["src/"]},
        {"id": "task-b", "owner": "alice", "goal": "second", "scopes": ["docs/"]},
    ]))
    with pytest.raises(OwnerError) as failure:
        manager.create(preview["previewId"], root)
    assert failure.value.code == "creation_failed"
    assert factory.instances[0].upsert_calls == 2  # init_session and one task did land

    created = sorted(Path(path) for path in failure.value.receipt["createdPaths"])
    assert len(created) == 3 and all(path.exists() for path in created)
    status = manager.status()
    assert status["state"] == "creation_failed" and status["retryRequired"] is True
    assert sorted(status["createdPaths"]) == sorted(str(path) for path in created)
    assert status["storePath"] is None and status["sessionId"] is None
    assert status["endpoint"] is None and status["tasks"] == []
    hub, store_dir = created[1], created[2]
    assert hub.name == "hub.git" and store_dir.name == "store.git"
    assert all(git(path, "rev-parse", "--is-bare-repository") == "true"
               for path in (hub, store_dir))
    assert source_manifest(root) == before


def test_creation_failure_keeps_the_previous_config_honest_and_recovers_once(tmp_path):
    root, _ = project(tmp_path)
    factory = SwitchableStore()
    manager = OwnerSessionManager(tmp_path / "private", store_factory=factory)
    preview = manager.preview_create(root, **request())
    original = manager.create(preview["previewId"], root)
    manager.stop()
    assert manager.status()["state"] == "stopped"

    factory.fail_after = 0
    broken = manager.preview_create(root, **request(session_id="second"))
    with pytest.raises(OwnerError) as failure:
        manager.create(broken["previewId"], root)
    assert failure.value.code == "creation_failed"

    status = manager.status()
    assert status["state"] == "creation_failed"
    assert status["sessionId"] == original["sessionId"]      # previous config, honestly reported
    assert status["storePath"] == original["storePath"]
    assert status["endpoint"] is None and status["retryRequired"] is True
    assert all(Path(path).exists() for path in status["createdPaths"])
    assert original["storePath"] not in status["createdPaths"]

    factory.fail_after = None
    retry = manager.preview_create(root, **request(session_id="second"))
    recovered = manager.create(retry["previewId"], root)
    assert recovered["state"] == "configured" and recovered["sessionId"] == "second"
    assert recovered["storePath"] != original["storePath"]
    namespace = Path(recovered["storePath"]).parent
    assert namespace.exists()
    assert sorted(recovered["createdPaths"]) == sorted(
        [str(namespace), str(namespace / "hub.git"), str(namespace / "store.git")])
    assert manager.start(root)["state"] == "running"
    manager.stop()


def test_preflight_rejections_never_leave_artifacts(tmp_path):
    root, _ = project(tmp_path)
    other, _ = project(tmp_path / "other")
    private = tmp_path / "private"
    manager = OwnerSessionManager(private)
    for bad in ("unknown-preview", "", None, 17):
        with pytest.raises(OwnerError, match="preview_stale"):
            manager.create(bad, root)
    assert not private.exists()
    preview = manager.preview_create(root, **request())
    with pytest.raises(OwnerError, match="preview_stale"):
        manager.create(preview["previewId"], other)
    assert not private.exists()
    assert manager.status()["state"] == "unconfigured"


# --------------------------------------------------------------------------
# 7. stop/start lifecycle without orphan listeners
# --------------------------------------------------------------------------


def assert_no_orphan(manager: OwnerSessionManager, servers: list[FakeServer]) -> None:
    for server in servers:
        assert server.closed or manager._server is server, "a live listener lost its owner"
    final = manager.status()
    if final["state"] == "running":
        assert manager._server is not None
        assert final["endpoint"] == manager._server.base_url
    else:
        assert final["endpoint"] is None
        assert manager._server is None or manager._server.closed


def test_start_cannot_slip_in_while_a_stop_is_draining(tmp_path):
    root, _ = project(tmp_path)
    made: list[FakeServer] = []

    def factory(_coordinator: Any, *, port: int = 0) -> FakeServer:
        server = FakeServer(block_close=not made)  # only the drain target waits
        made.append(server)
        return server

    manager = OwnerSessionManager(tmp_path / "private", server_factory=factory)
    preview = manager.preview_create(root, **request())
    manager.create(preview["previewId"], root)
    manager.start(root)
    draining = made[0]

    stopper = threading.Thread(target=manager.stop)
    stopper.start()
    assert draining.entered.wait(3)

    # A new listener may not appear while a drain is in flight: the start
    # parks behind the stop that owns the operation lock.
    queued = threading.Event()
    starter = threading.Thread(target=lambda: (queued.set(), manager.start(root)))
    starter.start()
    blocked_behind(queued, starter)
    assert manager.status()["state"] == "stopping"
    assert len(made) == 1

    draining.release.set()
    stopper.join(5)
    assert not stopper.is_alive()
    starter.join(10)
    assert not starter.is_alive()
    assert manager.status()["state"] == "running"
    assert_no_orphan(manager, made)
    made[-1].release.set()
    assert manager.stop()["state"] == "stopped"
    assert_no_orphan(manager, made)


@pytest.mark.parametrize("followers", [("stop", "start"), ("start", "stop")])
def test_queued_stop_and_start_never_leave_an_unowned_listener(tmp_path, followers):
    """Both arrival orders behind a draining stop must stay orphan-free.

    Whichever follower wins the operation lock (no assumption is made about
    that), the manager may never end up reporting a dead endpoint while a
    listener it started is still live and unreferenced.
    """
    root, _ = project(tmp_path)
    made: list[FakeServer] = []

    def factory(_coordinator: Any, *, port: int = 0) -> FakeServer:
        server = FakeServer(block_close=not made)  # only the drain target waits
        made.append(server)
        return server

    manager = OwnerSessionManager(tmp_path / "private", server_factory=factory)
    preview = manager.preview_create(root, **request())
    manager.create(preview["previewId"], root)
    manager.start(root)
    draining = made[0]

    stop_one = threading.Thread(target=manager.stop)
    stop_one.start()
    assert draining.entered.wait(3)

    queued = {kind: threading.Event() for kind in followers}
    threads: list[threading.Thread] = []
    for kind in followers:
        action = manager.stop if kind == "stop" else lambda: manager.start(root)
        thread = threading.Thread(target=lambda a=action, k=kind: (queued[k].set(), a()))
        thread.start()
        blocked_behind(queued[kind], thread)
        threads.append(thread)
    assert len(made) == 1  # nothing new while the drain owns the operation lock

    draining.release.set()
    stop_one.join(5)
    assert not stop_one.is_alive()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive(), thread.name

    assert_no_orphan(manager, made)
    for server in made:
        server.release.set()
    if manager.status()["state"] == "running":
        manager.stop()
    assert manager.status()["state"] == "stopped"
    assert_no_orphan(manager, made)


def test_status_stays_fast_and_credential_free_while_a_stop_drains(tmp_path):
    root, _ = project(tmp_path)
    made: list[FakeServer] = []

    def factory(_coordinator: Any, *, port: int = 0) -> FakeServer:
        server = FakeServer(block_close=not made)  # only the drain target waits
        made.append(server)
        return server

    manager = OwnerSessionManager(tmp_path / "private", server_factory=factory)
    preview = manager.preview_create(root, **request())
    manager.create(preview["previewId"], root)
    manager.start(root)
    revealed = manager.reveal_member_once("bob")
    server = made[0]

    threading.Thread(target=manager.stop).start()
    assert server.entered.wait(3)

    began = time.monotonic()
    status = manager.status()
    elapsed = time.monotonic() - began
    assert status["state"] == "stopping"
    assert status["endpoint"] == server.base_url
    assert status["exportedMembers"] == ["bob"]
    assert revealed["credential"] not in json.dumps(status)
    assert elapsed < 1.0
    server.release.set()


# --------------------------------------------------------------------------
# 8. a failed stop keeps the epoch, endpoint and credential map
# --------------------------------------------------------------------------


def test_failed_stop_keeps_epoch_endpoint_and_tokens_until_a_successful_retry(tmp_path, monkeypatch):
    root, _ = project(tmp_path)
    manager = OwnerSessionManager(tmp_path / "private")
    preview = manager.preview_create(root, **request())
    manager.create(preview["previewId"], root)
    running = manager.start(root)
    old = manager.reveal_member_once("bob")
    port = port_of(running["endpoint"])

    original = manager._server.close
    attempts: list[int] = []

    def close_once() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("listener detail that must not surface")
        original()

    monkeypatch.setattr(manager._server, "close", close_once)
    with pytest.raises(OwnerError, match="cleanup_failed"):
        manager.stop()
    monkeypatch.undo()

    held = manager.status()
    assert held["state"] == "cleanup_failed" and held["retryRequired"] is True
    assert held["endpoint"] == running["endpoint"] and held["epoch"] == running["epoch"]
    assert held["exportedMembers"] == ["bob"]
    with pytest.raises(OwnerError, match="not_running_or_member"):
        manager.reveal_member_once("alice")
    with pytest.raises(OwnerError, match="not_configured"):
        manager.start(root)
    live = LoopbackSnapshotClient(old["endpoint"], credential=old["credential"]).snapshot()
    assert live.state.session_id == "demo"

    assert manager.stop()["state"] == "stopped"
    assert manager.status()["endpoint"] is None and manager.status()["exportedMembers"] == []

    restarted = manager.start(root, port=port)
    assert restarted["epoch"] == running["epoch"] + 1
    fresh = manager.reveal_member_once("bob")
    assert fresh["credential"] != old["credential"] and fresh["epoch"] == restarted["epoch"]
    with pytest.raises(Exception):
        LoopbackSnapshotClient(fresh["endpoint"], credential=old["credential"]).snapshot()
    again = LoopbackSnapshotClient(fresh["endpoint"], credential=fresh["credential"]).snapshot()
    assert again.state.session_id == "demo"
    manager.stop()


# --------------------------------------------------------------------------
# 9. start failures keep their resources owned for retry
# --------------------------------------------------------------------------


def test_start_bind_failure_returns_to_configured_and_can_retry(tmp_path):
    root, _ = project(tmp_path)
    manager = OwnerSessionManager(tmp_path / "private")
    preview = manager.preview_create(root, **request())
    manager.create(preview["previewId"], root)

    def explode(_coordinator: Any, *, port: int = 0) -> FakeServer:
        raise OSError("bind failed")

    manager._coordinator_factory = FakeCoordinator
    manager._server_factory = explode
    with pytest.raises(OwnerError, match="start_failed"):
        manager.start(root)
    status = manager.status()
    assert status["state"] == "configured" and status["endpoint"] is None
    assert status["retryRequired"] is False and status["epoch"] == 0

    manager._coordinator_factory = Coordinator
    manager._server_factory = LoopbackServer
    assert manager.start(root)["state"] == "running"
    manager.stop()


def test_start_that_cannot_release_its_listener_keeps_the_resource_owned(tmp_path):
    root, _ = project(tmp_path)
    manager = OwnerSessionManager(tmp_path / "private")
    preview = manager.preview_create(root, **request())
    manager.create(preview["previewId"], root)

    stuck = FakeServer(start_error=OSError("accept thread could not start"),
                       close_errors=1, block_close=False)
    manager._coordinator_factory = FakeCoordinator
    manager._server_factory = lambda _coordinator, *, port=0: stuck
    with pytest.raises(OwnerError, match="start_failed"):
        manager.start(root)

    status = manager.status()
    assert status["state"] == "cleanup_failed" and status["retryRequired"] is True
    assert status["endpoint"] is None
    assert stuck.close_calls >= 1 and stuck.closed is False
    assert manager._server is stuck, "an unreleased listener must stay owned"
    with pytest.raises(OwnerError, match="not_configured"):
        manager.start(root)

    assert manager.stop()["state"] == "stopped"   # the retried close now succeeds
    assert stuck.closed is True and manager.status()["endpoint"] is None

    manager._coordinator_factory = Coordinator
    manager._server_factory = LoopbackServer
    assert manager.start(root)["state"] == "running"
    manager.stop()


# --------------------------------------------------------------------------
# 10. credential hygiene
# --------------------------------------------------------------------------


def test_raw_credentials_appear_only_in_the_explicit_reveal_response(tmp_path):
    root, _ = project(tmp_path)
    manager = OwnerSessionManager(tmp_path / "private")
    preview = manager.preview_create(root, **request())
    created = manager.create(preview["previewId"], root)
    running = manager.start(root)
    revealed = manager.reveal_member_once("bob")
    credential = revealed["credential"]

    assert credential and len(credential) >= 32
    assert revealed["scope"] == "loopback-only" and revealed["memberId"] == "bob"
    assert revealed["taskIds"] == ["task-a"]
    assert set(revealed) == {"endpoint", "sessionId", "baseCommit", "targetVersion", "memberId",
                             "credential", "storePath", "hubPath", "taskIds", "epoch", "scope"}
    for dto in (preview, created, running, manager.status()):
        assert credential not in json.dumps(dto)
    assert credential not in repr(manager) and credential not in repr(manager._config)
    assert sorted(manager._credentials) == ["alice", "bob"]

    for call, kwargs in ((manager.reveal_member_once, {"member_id": "bob"}),
                         (manager.reveal_member_once, {"member_id": "mallory"}),
                         (manager.create, {"preview_id": "nope", "project_root": root}),
                         (manager.start, {"project_root": root})):
        with pytest.raises(OwnerError) as failure:
            call(**kwargs)
        assert credential not in str(failure.value)
        assert credential not in repr(failure.value)
        assert credential not in json.dumps(failure.value.receipt)

    port = port_of(running["endpoint"])
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest("POST", "/v1/snapshot")
        connection.putheader("Authorization", f"Bearer {credential}")
        connection.putheader("Connection", "close")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "2")
        connection.endheaders(b"{}")
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    assert response.status == 200
    assert credential.encode() not in raw
    assert credential not in json.dumps(json.loads(raw.decode("utf-8")))
    manager.stop()


# --------------------------------------------------------------------------
# 11-12. local collaboration previews
# --------------------------------------------------------------------------


def snapshot_for(base_commit: str) -> SessionState:
    task = Task("task-a", "bob", "Implement safely", ("src/",), "queued", "1" * 40)
    return SessionState("demo", "v1", base_commit, SharedContext("g", (), ()), {task.id: task})


def fake_host(tmp_path: Path, state: SessionState, recorder: dict[str, Any],
              *, gate: threading.Event | None = None) -> CollaborationHost:
    class FakeClient:
        def __init__(self, endpoint: str, *, credential: str) -> None:
            recorder.setdefault("credentials", []).append((endpoint, credential))

        def snapshot(self) -> Snapshot:
            recorder["snapshot_calls"] = recorder.get("snapshot_calls", 0) + 1
            if gate is not None:
                assert gate.wait(5)
            return Snapshot("1" * 40, state)

    return CollaborationHost(tmp_path / "cursors", head_reader=lambda _root: state.base_commit,
                              client_factory=FakeClient)


def test_local_preview_is_rejected_when_the_epoch_changes_while_the_fetch_is_paused(tmp_path):
    root, head = project(tmp_path)
    manager = OwnerSessionManager(tmp_path / "private")
    preview = manager.preview_create(root, **request())
    manager.create(preview["previewId"], root)
    first = manager.start(root)
    revealed = manager.reveal_member_once("bob")

    gate = threading.Event()
    recorder: dict[str, Any] = {}
    host = fake_host(tmp_path, snapshot_for(head), recorder, gate=gate)

    class CountingCandidates(dict):
        """Counts registrations so an empty registry means 'dropped', not 'never made'."""

        created = 0

        def __setitem__(self, key: Any, value: Any) -> None:
            CountingCandidates.created += 1
            super().__setitem__(key, value)

    host._candidates = CountingCandidates()

    outcome: dict[str, Any] = {}

    def fetch() -> None:
        try:
            outcome["result"] = manager.preview_local_collaboration(
                host, root, member_id="bob", task_id="task-a")
        except BaseException as error:  # noqa: BLE001 - captured for the assertion below
            outcome["error"] = error

    worker = threading.Thread(target=fetch)
    worker.start()
    wait_for(lambda: recorder.get("snapshot_calls", 0) == 1, "the host fetch never started")
    assert recorder["credentials"] == [(first["endpoint"], revealed["credential"])]
    assert host._candidates == {}

    manager.stop()
    restarted = manager.start(root)   # new epoch, new credentials, same configured session
    assert restarted["epoch"] == first["epoch"] + 1
    assert manager.status()["state"] == "running"

    gate.set()
    worker.join(5)
    assert not worker.is_alive()

    assert "result" not in outcome
    assert isinstance(outcome.get("error"), OwnerError)
    assert outcome["error"].code == "stale_local_preview"
    assert CountingCandidates.created == 1     # the host really registered a candidate
    assert host._candidates == {} and host._approved == {}   # and the manager dropped it
    manager.stop()


def test_local_preview_never_reaches_the_host_for_a_different_source_root(tmp_path):
    root, head = project(tmp_path)
    manager = OwnerSessionManager(tmp_path / "private")
    preview = manager.preview_create(root, **request())
    manager.create(preview["previewId"], root)
    manager.start(root)
    revealed = manager.reveal_member_once("bob")

    subprocess.run(["git", "clone", "-q", str(root), str(tmp_path / "clone")], check=True)
    clone = tmp_path / "clone"
    assert git(clone, "rev-parse", "HEAD") == head

    recorder: dict[str, Any] = {}
    host = fake_host(tmp_path, snapshot_for(head), recorder)

    with pytest.raises(OwnerError) as failure:
        manager.preview_local_collaboration(host, clone, member_id="bob", task_id="task-a")
    assert failure.value.code == "wrong_project"
    assert recorder == {} and host._candidates == {}

    for member_id, task_id in (("alice", "task-a"), ("bob", "task-zz"), ("carol", "task-a")):
        with pytest.raises(OwnerError) as failure:
            manager.preview_local_collaboration(host, root, member_id=member_id, task_id=task_id)
        assert failure.value.code in {"invalid_task_assignment", "not_running_or_member"}
    assert recorder == {} and host._candidates == {}

    accepted = manager.preview_local_collaboration(host, root, member_id="bob", task_id="task-a")
    assert accepted["epoch"] == manager.status()["epoch"]
    assert accepted["preview"]["memberId"] == "bob"
    assert accepted["preview"]["task"]["owner"] == "bob"
    assert len(host._candidates) == 1
    assert revealed["credential"] not in json.dumps(accepted)
    manager.stop()


# --------------------------------------------------------------------------
# 13. membership comes from owner-declared configuration
# --------------------------------------------------------------------------


def test_membership_is_owner_declared_and_not_inferred_from_remote_task_owners(tmp_path):
    root, _ = project(tmp_path)
    manager = configured(root, tmp_path / "private")
    status = manager.status()
    assert status["ownerId"] == "alice" and status["memberIds"] == ["alice", "bob"]
    assert [task["owner"] for task in status["tasks"]] == ["bob"]

    running = manager.start(root)
    assert running["memberIds"] == ["alice", "bob"]
    assert manager.reveal_member_once("alice")["taskIds"] == []   # declared member without a task
    assert manager.reveal_member_once("bob")["taskIds"] == ["task-a"]
    with pytest.raises(OwnerError, match="not_running_or_member"):
        manager.reveal_member_once("carol")
    assert sorted(manager.status()["exportedMembers"]) == ["alice", "bob"]
    manager.stop()

    stranger = configured(root, tmp_path / "stranger-private", session_id="other",
                          owner_id="carol", member_ids=["carol", "dave"],
                          tasks=[{"id": "task-z", "owner": "dave", "goal": "g", "scopes": ["src/"]}])
    live = stranger.status()
    hostile = OwnerSessionManager(tmp_path / "hostile")
    with pytest.raises(OwnerError, match="invalid_members"):
        hostile.select_existing(root, store_path=Path(live["storePath"]),
                                hub_path=Path(live["hubPath"]),
                                owner_id="alice", member_ids=["alice", "bob"])
    assert hostile.status()["state"] == "unconfigured"


# --------------------------------------------------------------------------
# 14. metadata paths stay private and outside the source checkout
# --------------------------------------------------------------------------


def test_configured_paths_are_private_and_source_side_effects_stay_zero(tmp_path):
    root, _ = project(tmp_path)
    before = source_manifest(root)
    manager = configured(root, tmp_path / "private")
    status = manager.status()
    private = (tmp_path / "private").resolve()

    assert Path(status["storePath"]).parent.parent == private
    assert Path(status["hubPath"]).parent.parent == private
    assert root not in Path(status["storePath"]).parents
    assert root not in Path(status["hubPath"]).parents
    assert status["epoch"] == 0 and status["endpoint"] is None
    assert is_sha(status["revision"])
    assert source_manifest(root) == before
    if os.name == "posix":
        # Owner uid + 0700 are POSIX capabilities; Windows exposes neither, so
        # only the platform-independent privacy claims above are asserted there.
        assert stat.S_IMODE(Path(private).stat().st_mode) == 0o700
        assert Path(status["storePath"]).stat().st_uid == os.getuid()