"""Explicit historical retry admission and fresh execution identity."""
from types import SimpleNamespace

import pytest

from run_runtime.events import RunEventType
from run_runtime.service import RunRuntime
from run_runtime.store import RunStore
from webhost import state
from webhost.api import run as run_api
from webhost.bridge import BridgeError
from webhost.run_registry import RunRegistry, RunSlot

_drain_resources = run_api._drain_collaboration_resources
_real_workspace_factory = run_api.engine_factory.create_pipeline_workspace


@pytest.fixture
def retry_setup(tmp_path, monkeypatch):
    root = str(tmp_path.resolve())
    db = tmp_path / "retry.sqlite3"
    runtime = RunRuntime(RunStore(db))
    task = runtime.create_task(project_root=root, prompt="x" * 9000)
    source = runtime.create_run(task_id=task.task_id, routing={"agent_provider": "p"})
    runtime.record(run_id=source.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record(run_id=source.run_id, type=RunEventType.RUN_FAILED,
                   payload={"error_code": "fixture"})
    runtime = RunRuntime(RunStore(db))
    monkeypatch.setattr(state, "_active", None)
    state.set_project(root)
    monkeypatch.setattr(state, "_run_runtime", runtime)
    registry = RunRegistry()
    monkeypatch.setattr(run_api, "_run_registry", registry)
    monkeypatch.setattr(run_api, "_active", {"worker": None, "engine": "agent",
        "run_id": None, "workspace": None, "coordinator": None, "proposals": [], "agent_project_root": root})
    monkeypatch.setattr(run_api, "_drain_collaboration_resources", lambda: None)
    monkeypatch.setattr(run_api, "_orphaned_retry_workspaces", [])
    monkeypatch.setattr(run_api, "_draining_workers", [])
    monkeypatch.setattr(run_api, "_active_canonical_run_blocks_start", lambda: False)
    monkeypatch.setattr(run_api, "_delivery_is_busy", lambda: False)
    monkeypatch.setattr(run_api, "_run_registry", registry)
    monkeypatch.setattr(run_api.engine_factory, "role_supported", lambda provider: (True, None))
    monkeypatch.setattr(run_api.engine_factory, "_repo_root_for", lambda path: path)
    monkeypatch.setattr(run_api, "_validate_mentions", lambda _proj, _raw: ([], []))
    monkeypatch.setattr(run_api, "_require_agent_project", lambda _proj: None)
    monkeypatch.setattr(run_api, "_require_project", lambda: SimpleNamespace(root=root))
    workspaces = []
    def create_workspace(path, run_id):
        workspace = SimpleNamespace(dispose=lambda: None)
        workspaces.append((path, run_id, workspace))
        return workspace
    monkeypatch.setattr(run_api.engine_factory, "create_pipeline_workspace", create_workspace)
    monkeypatch.setattr(run_api, "build_agent_ports", lambda *args: object())
    monkeypatch.setattr(run_api, "_AgentWorker", lambda *args: SimpleNamespace(
        isRunning=lambda: False, agent_started=False))
    monkeypatch.setattr(run_api, "_wire_agent_worker", lambda *args, **kwargs: None)
    context = SimpleNamespace(_bridge=SimpleNamespace(emit_event=lambda *args: None))
    return root, runtime, task, source, registry, workspaces, context


def test_restart_retry_preserves_task_and_uses_fresh_workspace(retry_setup, monkeypatch):
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    monkeypatch.setattr(run_api.engine_factory, "create_pipeline_workspace",
                        lambda path, run_id: workspaces.append((path, run_id, object())) or workspaces[-1][2])
    result = run_api._start({"retryOfRunId": source.run_id}, ctx)
    retried = runtime.get_run(result["runId"])
    assert retried.run_id != source.run_id
    assert retried.task_id == task.task_id
    assert retried.attempt == 2
    assert retried.retry_of_run_id == source.run_id
    assert retried.routing == {"agent_provider": "p"}
    slot = registry.get(retried.run_id)
    assert slot.task == task.prompt and len(slot.task) == 9000
    assert slot.pinned_paths == []
    assert workspaces == [(root, retried.run_id, slot.workspace)]
    assert runtime.store.get_task(task.task_id).prompt == task.prompt
    assert len(runtime.store.list_runs(task_id=task.task_id)) == 2
    with pytest.raises(BridgeError):
        run_api._start({"retryOfRunId": source.run_id}, ctx)
    assert len(workspaces) == 1


def test_retry_uses_real_isolated_git_worktree_and_preserves_source(retry_setup, tmp_path, monkeypatch):
    import subprocess
    import engine_factory

    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    tracked = repo / "tracked.txt"
    tracked.write_text("committed\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-q", "-m", "fixture")
    tracked.write_text("source dirty\n", encoding="utf-8")
    with runtime.store._session() as connection:
        connection.execute("UPDATE tasks SET project_root=? WHERE task_id=?", (str(repo.resolve()), task.task_id))
    monkeypatch.setattr(run_api, "_require_project", lambda: SimpleNamespace(root=str(repo.resolve())))
    monkeypatch.setattr(run_api.engine_factory, "_repo_root_for", lambda path: path)
    monkeypatch.setattr(engine_factory, "workspaces_dir", lambda: tmp_path / "real-workspaces")
    monkeypatch.setattr(run_api, "_run_registry", registry)
    real_workspace_factory = _real_workspace_factory
    monkeypatch.setattr(run_api.engine_factory, "create_pipeline_workspace",
                        lambda path, run_id: real_workspace_factory(path, run_id))
    result = run_api._start({"retryOfRunId": source.run_id}, ctx)
    slot = registry.get(result["runId"])
    try:
        assert slot.workspace.root != repo
        assert (slot.workspace.root / "tracked.txt").read_text(encoding="utf-8") == "source dirty\n"
        assert tracked.read_text(encoding="utf-8") == "source dirty\n"
        assert slot.proposals == [] and slot.pinned_paths == []
        assert runtime.get_run(result["runId"]).task_id == task.task_id
    finally:
        slot.workspace.dispose()
        slot.workspace = None


@pytest.mark.parametrize("case", ["payload", "wrong_root", "owned", "capacity"])
def test_retry_rejection_does_not_create_run_or_workspace(retry_setup, monkeypatch, case):
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    params = {"retryOfRunId": source.run_id}
    if case == "payload":
        params["providerId"] = "other"
    elif case == "wrong_root":
        monkeypatch.setattr(run_api, "_require_project", lambda: SimpleNamespace(root=root + "/other"))
    elif case == "owned":
        registry.add(RunSlot(source.run_id, task.task_id, root, "p", SimpleNamespace(), task=task.prompt))
    else:
        assert registry.reserve(root)
        assert registry.reserve(root)
    with pytest.raises(BridgeError):
        run_api._start(params, ctx)
    assert len(runtime.store.list_runs(task_id=task.task_id)) == 1
    assert workspaces == []


def test_history_retry_availability_fails_closed_when_source_has_old_proposal(retry_setup):
    from webhost.run_history import get_history, list_history

    root, runtime, task, source, _registry, _workspaces, _ctx = retry_setup
    runtime.record(run_id=source.run_id, type=RunEventType.PROPOSAL_READY,
                   payload={"proposals": [{"path": "kept.ts"}]})
    runtime.record(run_id=source.run_id, type=RunEventType.RUN_FAILED,
                   payload={"error_code": "fixture"})
    assert list_history(runtime, root)[0]["retryAvailable"] is False
    assert get_history(runtime, root, source.run_id)["retryAvailable"] is False


def test_retry_payload_scalar_is_rejected_as_bridge_error(retry_setup):
    _root, runtime, task, source, _registry, workspaces, ctx = retry_setup
    with pytest.raises(BridgeError) as caught:
        run_api._start({"retryOfRunId": []}, ctx)
    assert caught.value.code == "invalid_retry"
    assert len(runtime.store.list_runs(task_id=task.task_id)) == 1
    assert workspaces == []


def test_retry_workspace_dispose_failure_after_runtime_acquisition_is_drained(retry_setup, monkeypatch):
    from pathlib import Path
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    class DisposableWorkspace:
        def dispose(self): raise RuntimeError("injected disposal failure")
    workspace = DisposableWorkspace()
    monkeypatch.setattr(run_api.engine_factory, "create_pipeline_workspace", lambda *_args: workspace)
    runtime_calls = {"count": 0}
    def runtime_fails_after_preflight():
        runtime_calls["count"] += 1
        if runtime_calls["count"] >= 2:
            raise RuntimeError("runtime unavailable after workspace creation")
        return runtime
    monkeypatch.setattr(run_api.state, "get_run_runtime", runtime_fails_after_preflight)
    from run_runtime.errors import RunStoreError
    monkeypatch.setattr(run_api.AgentRunCoordinator, "start",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RunStoreError("conflict")))
    with pytest.raises(BridgeError) as caught:
        run_api._start({"retryOfRunId": source.run_id}, ctx)
    assert runtime_calls["count"] == 1
    assert caught.value.code == "retry_uncertain"
    assert (str(Path(root).resolve()), workspace) in run_api._orphaned_retry_workspaces
    assert len(runtime.store.list_runs(task_id=task.task_id)) == 1
    with pytest.raises(BridgeError) as blocked:
        run_api._start({"retryOfRunId": source.run_id}, ctx)
    assert blocked.value.code == "collab_cleanup_failed"


def test_orphaned_retry_cleanup_releases_its_reservation(retry_setup):
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    class Workspace:
        def dispose(self): pass
    assert registry.reserve(root)
    workspace = Workspace()
    run_api._orphaned_retry_workspaces.append((root, workspace))
    _drain_resources()
    assert not run_api._orphaned_retry_workspaces
    assert registry.reserve(root)
    assert registry.reserve(root)


def test_retry_workspace_creation_failure_does_not_consume_attempt(retry_setup, monkeypatch):
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    monkeypatch.setattr(run_api.engine_factory, "create_pipeline_workspace",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("git fixture failure")))
    with pytest.raises(BridgeError) as caught:
        run_api._start({"retryOfRunId": source.run_id}, ctx)
    assert caught.value.code == "worker_start_failed"
    assert len(runtime.store.list_runs(task_id=task.task_id)) == 1
    assert registry.reserve(root)
    assert registry.reserve(root)
    assert not registry.reserve(root)


def test_retry_reuses_preflight_runtime_for_workspace_admission(retry_setup, monkeypatch):
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    class DisposableWorkspace:
        disposed = False
        def dispose(self): self.disposed = True
    workspace = DisposableWorkspace()
    monkeypatch.setattr(run_api.engine_factory, "create_pipeline_workspace", lambda *_args: workspace)
    runtime_calls = {"count": 0}
    def runtime_available_for_preflight():
        runtime_calls["count"] += 1
        if runtime_calls["count"] >= 3:
            raise RuntimeError("runtime unavailable after workspace creation")
        return runtime
    monkeypatch.setattr(run_api.state, "get_run_runtime", runtime_available_for_preflight)
    result = run_api._start({"retryOfRunId": source.run_id}, ctx)
    assert runtime_calls["count"] == 1
    assert not workspace.disposed
    assert len(runtime.store.list_runs(task_id=task.task_id)) == 2
    assert registry.get(result["runId"]).workspace is workspace
    assert registry.reserve(root)
    assert not registry.reserve(root)


def test_retry_lifecycle_event_failure_settles_canonical_attempt(retry_setup, monkeypatch):
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    class DisposableWorkspace:
        def __init__(self): self.disposed = False
        def dispose(self): self.disposed = True
    workspace = DisposableWorkspace()
    monkeypatch.setattr(run_api.engine_factory, "create_pipeline_workspace", lambda *_args: workspace)
    original_record = runtime.record
    calls = {"started": 0}
    def fail_started(**kwargs):
        if kwargs.get("type") == RunEventType.RUN_STARTED:
            calls["started"] += 1
            raise RuntimeError("injected started event failure")
        return original_record(**kwargs)
    monkeypatch.setattr(runtime, "record", fail_started)
    with pytest.raises(BridgeError):
        run_api._start({"retryOfRunId": source.run_id}, ctx)
    attempts = runtime.store.list_runs(task_id=task.task_id)
    retry = next(item for item in attempts if item.run_id != source.run_id)
    assert retry.status.value == "failed"
    assert registry.get(retry.run_id) is not None
    assert registry.get(retry.run_id).phase == "failed"
    assert workspace.disposed
    assert len(registry.open_slots(root)) == 0
    assert registry.reserve(root)
    assert registry.reserve(root)
    assert not registry.reserve(root)


def test_retry_insert_failure_with_cleanup_failure_owns_planned_run_slot(retry_setup, monkeypatch):
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    class DisposableWorkspace:
        def dispose(self): raise RuntimeError("injected cleanup failure")
    workspace = DisposableWorkspace()
    monkeypatch.setattr(run_api.engine_factory, "create_pipeline_workspace", lambda *_args: workspace)
    original_start = run_api.AgentRunCoordinator.start
    def lose_race(*args, **kwargs):
        runtime.store.create_retry_run(source_run_id=source.run_id, project_root=root,
                                       run_id="actual-racing-retry")
        return original_start(*args, **kwargs)
    monkeypatch.setattr(run_api.AgentRunCoordinator, "start", lose_race)
    monkeypatch.setattr(run_api, "new_run_id", lambda: "planned-losing-retry")
    monkeypatch.setattr(run_api.state, "get_run_runtime", lambda: runtime)
    monkeypatch.setattr("run_runtime.store.new_run_id", lambda: "actual-racing-retry")
    with pytest.raises(BridgeError) as caught:
        run_api._start({"retryOfRunId": source.run_id}, ctx)
    assert caught.value.code == "retry_uncertain"
    retry = max(runtime.store.list_runs(task_id=task.task_id), key=lambda item: item.attempt)
    assert retry.run_id == "actual-racing-retry"
    assert registry.slots() == ()
    assert retry.status.value == "created"
    assert len(registry.open_slots(root)) == 0
    # Failed cleanup is held by the process drain owner but cannot claim the
    # other process's canonical retry ID or free this project's admission slot.
    assert registry.reserve(root)
    assert not registry.reserve(root)


def test_retry_ports_failure_and_failed_settlement_retains_uncertain_slot(retry_setup, monkeypatch):
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    class DisposableWorkspace:
        def dispose(self): pass
    workspace = DisposableWorkspace()
    monkeypatch.setattr(run_api.engine_factory, "create_pipeline_workspace", lambda *_args: workspace)
    monkeypatch.setattr(run_api, "build_agent_ports", lambda *_args: (_ for _ in ()).throw(RuntimeError("ports failed")))
    original_record = runtime.record
    def fail_terminal(**kwargs):
        if kwargs.get("type") == RunEventType.RUN_FAILED:
            raise RuntimeError("terminal persistence failed")
        return original_record(**kwargs)
    monkeypatch.setattr(runtime, "record", fail_terminal)
    with pytest.raises(BridgeError):
        run_api._start({"retryOfRunId": source.run_id}, ctx)
    retry = max(runtime.store.list_runs(task_id=task.task_id), key=lambda item: item.attempt)
    slot = registry.get(retry.run_id)
    assert slot is not None and slot.phase == "uncertain" and slot.workspace is None
    assert retry.status.value == "running"
    assert len(registry.open_slots(root)) == 1
    assert registry.reserve(root)
    assert not registry.reserve(root)


def test_retry_ports_failure_settles_terminal_and_releases_capacity(retry_setup, monkeypatch):
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    monkeypatch.setattr(run_api, "build_agent_ports", lambda *_args: (_ for _ in ()).throw(RuntimeError("ports unavailable")))
    with pytest.raises(BridgeError):
        run_api._start({"retryOfRunId": source.run_id}, ctx)
    retry = max(runtime.store.list_runs(task_id=task.task_id), key=lambda item: item.attempt)
    slot = registry.get(retry.run_id)
    assert retry.status.value == "failed" and retry.attempt == 2
    assert slot is not None and slot.workspace is None and slot.phase == "failed"
    assert len(registry.open_slots(root)) == 0
    assert registry.reserve(root)
    assert registry.reserve(root)
    assert not registry.reserve(root)


def test_retry_terminal_settlement_failure_keeps_uncertain_capacity(retry_setup, monkeypatch):
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    class DisposableWorkspace:
        def dispose(self): raise RuntimeError("injected cleanup failure")
    workspace = DisposableWorkspace()
    monkeypatch.setattr(run_api.engine_factory, "create_pipeline_workspace", lambda *_args: workspace)
    original_record = runtime.record
    def fail_lifecycle_and_terminal(**kwargs):
        if kwargs.get("type") in {RunEventType.RUN_STARTED, RunEventType.RUN_FAILED}:
            raise RuntimeError("injected lifecycle persistence failure")
        return original_record(**kwargs)
    monkeypatch.setattr(runtime, "record", fail_lifecycle_and_terminal)
    with pytest.raises(BridgeError):
        run_api._start({"retryOfRunId": source.run_id}, ctx)
    retry = max(runtime.store.list_runs(task_id=task.task_id), key=lambda item: item.attempt)
    slot = registry.get(retry.run_id)
    assert slot is not None and slot.run_id != source.run_id and slot.workspace is workspace
    assert slot.phase == "uncertain"
    assert retry.status.value == "created"
    assert len(registry.open_slots(root)) == 1
    assert registry.reserve(root)
    assert not registry.reserve(root)
    with pytest.raises(BridgeError) as caught:
        run_api._start({"retryOfRunId": source.run_id}, ctx)
    assert caught.value.code == "retry_unavailable"


def _spawn_retry_child(db_path, source_run_id, project_root, connection):
    connection.send("ready")
    if connection.recv() != "go":
        connection.close()
        return
    try:
        record = RunRuntime(RunStore(db_path)).create_retry_run(
            source_run_id=source_run_id, project_root=project_root,
        )
        connection.send(("ok", record.run_id))
    except Exception as exc:
        connection.send(("error", type(exc).__name__))
    finally:
        connection.close()


def test_atomic_retry_creation_serializes_independent_connections(retry_setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _root, _runtime, task, source, _registry, _workspaces, _ctx = retry_setup
    barrier = Barrier(2)
    def attempt():
        local = RunRuntime(RunStore(_runtime.store._db_path))
        barrier.wait(timeout=5)
        try:
            return local.create_retry_run(source_run_id=source.run_id, project_root=task.project_root).run_id
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(_runtime.store.list_runs(task_id=task.task_id)) == 2


def test_atomic_retry_creation_serializes_spawned_processes(retry_setup):
    import multiprocessing

    _root, runtime, task, source, _registry, _workspaces, _ctx = retry_setup
    context = multiprocessing.get_context("spawn")
    parent_pipes = []
    children = []
    try:
        for _ in range(2):
            parent, child = context.Pipe()
            process = context.Process(target=_spawn_retry_child,
                args=(str(runtime.store.db_path), source.run_id, task.project_root, child))
            process.start()
            child.close()
            parent_pipes.append(parent)
            children.append(process)
        for pipe in parent_pipes:
            assert pipe.poll(10) and pipe.recv() == "ready"
        for pipe in parent_pipes:
            pipe.send("go")
        results = []
        for pipe in parent_pipes:
            assert pipe.poll(20), "spawned retry contender did not finish"
            results.append(pipe.recv())
        assert sum(result[0] == "ok" for result in results) == 1, results
        for process in children:
            process.join(timeout=10)
        assert all(process.exitcode == 0 for process in children)
        assert len(runtime.store.list_runs(task_id=task.task_id)) == 2
    finally:
        for process in children:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        for pipe in parent_pipes:
            pipe.close()


@pytest.mark.parametrize("expected", [
    {"expected_prompt": "changed"}, {"expected_provider_id": "other"},
])
def test_atomic_retry_identity_rejection_leaves_no_orphan(retry_setup, expected):
    from run_runtime.errors import RunStoreError
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    with pytest.raises(RunStoreError):
        runtime.create_retry_run(source_run_id=source.run_id, project_root=root, **expected)
    assert len(runtime.store.list_runs(task_id=task.task_id)) == 1


def test_atomic_retry_conflict_releases_bridge_reservation(retry_setup, monkeypatch):
    root, runtime, task, source, registry, workspaces, ctx = retry_setup
    class DisposableWorkspace:
        def dispose(self):
            pass
    monkeypatch.setattr(run_api.engine_factory, "create_pipeline_workspace", lambda *_args: DisposableWorkspace())
    original_start = run_api.AgentRunCoordinator.start
    def racing_start(*args, **kwargs):
        runtime.create_retry_run(source_run_id=source.run_id, project_root=root)
        return original_start(*args, **kwargs)
    monkeypatch.setattr(run_api.AgentRunCoordinator, "start", racing_start)
    with pytest.raises(BridgeError) as caught:
        run_api._start({"retryOfRunId": source.run_id}, ctx)
    assert caught.value.code == "retry_unavailable"
    assert workspaces == []  # this fixture supplies its own disposable workspace
    assert registry.reserve(root)
    assert registry.reserve(root)
    assert not registry.reserve(root)
