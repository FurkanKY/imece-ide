"""Durable ownership is granted by an OS lease AND a canonical idle seal."""
import os
import sys
from pathlib import Path
import subprocess

import pytest

from run_runtime.service import RunRuntime
from run_runtime.store import RunStore
from workspace.ownership import OwnershipError, WorkspaceLease, WorkspaceOwnership, continuation_available
from workspace.worktree import GitWorktreeWorkspace

_STRICT_NOFOLLOW = os.name == "nt" or (os.scandir in os.supports_fd and os.open in os.supports_dir_fd)


@pytest.fixture
def owned(tmp_path):
    if not _STRICT_NOFOLLOW:
        pytest.skip("Strict no-follow workspace sealing unavailable; restart is fail-closed")
    source = tmp_path / "source"
    source.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(source), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (source / "a.txt").write_text("original\n")
    git("add", ".")
    git("commit", "-m", "base")
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(source), prompt="Change a file")
    run = runtime.create_run(task_id=task.task_id, routing={"agent_provider": "p"})
    runtime.record(run_id=run.run_id, type="run.started", payload={})
    workspace = GitWorktreeWorkspace.create(source_root=source, run_id=run.run_id,
                                             base_dir=tmp_path / "workspaces")
    owner = WorkspaceOwnership.attach(runtime, run.run_id, workspace)
    yield source, runtime, run.run_id, workspace, owner
    if owner.lease.fd is not None:
        owner.lease.close()
    if workspace._worktree_dir.exists():
        workspace.ownership = None
        workspace.dispose()


def settle(runtime, run_id, kind="run.waiting_user"):
    runtime.record(run_id=run_id, type=kind, payload={})


def test_worktree_porcelain_z_parser_matches_paths_not_substrings(tmp_path):
    from workspace.ownership import _parse_worktree_porcelain_z, _worktree_path_matches
    raw = "worktree /tmp/project with spaces/Ω\0HEAD abc\0\0worktree /tmp/project with spaces/Ω-extra\0HEAD def\0\0"
    paths = _parse_worktree_porcelain_z(raw)
    assert len(paths) == 2
    assert _worktree_path_matches(paths[0], Path("/tmp/project with spaces/Ω"))
    assert not _worktree_path_matches(paths[1], Path("/tmp/project with spaces/Ω"))


@pytest.mark.parametrize("receipt,allowed", [(False, False), (True, True)])
def test_acp_workspace_seal_requires_latest_execution_receipt(tmp_path, receipt, allowed):
    source = tmp_path / "source"
    source.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(source), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (source / "a.txt").write_text("original\n")
    git("add", ".")
    git("commit", "-m", "base")
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(source), prompt="ACP")
    run = runtime.create_run(task_id=task.task_id, routing={"agent_provider": "claude"})
    runtime.record(run_id=run.run_id, type="run.started", payload={})
    workspace = GitWorktreeWorkspace.create(source_root=source, run_id=run.run_id,
                                             base_dir=tmp_path / "workspaces")
    owner = WorkspaceOwnership.attach(runtime, run.run_id, workspace)
    runtime.record(run_id=run.run_id, type="execution.started", execution_id="e1",
                   payload={"transport": "acp", "task": "ACP"})
    runtime.record(run_id=run.run_id, type="execution.failed", execution_id="e1",
                   payload={"transport": "acp", "error_type": "test", "message": "done",
                            "producer_quiescent": receipt})
    settle(runtime, run.run_id)
    if allowed:
        owner.seal()
        assert workspace.ownership.descriptor["state"] == "quiescent"
    else:
        with pytest.raises(OwnershipError, match="ACP execution"):
            owner.seal()
    owner.lease.close()
    workspace.ownership = None
    workspace.dispose()


@pytest.mark.parametrize("producer", ["tool", "verification"])
@pytest.mark.parametrize("tamper", ["missing", "false", "execution"])
def test_cancelled_producer_without_matching_positive_receipt_cannot_seal(owned, producer, tamper):
    _source, runtime, run_id, _workspace, owner = owned
    if producer == "tool":
        runtime.record(run_id=run_id, type="tool.started", execution_id="exec-1",
                       payload={"call_id": "call-1", "tool_name": "run_process"})
        terminal = "tool.failed"
        payload = {"call_id": "call-1", "tool_name": "run_process", "error_type": "ProcessCancelledError"}
        if tamper != "missing":
            payload["metadata"] = {"producer_quiescent": tamper == "execution"}
        runtime.record(run_id=run_id, type=terminal,
                       execution_id="exec-other" if tamper == "execution" else "exec-1", payload=payload)
    else:
        runtime.record(run_id=run_id, type="verification.check_started", execution_id="verify-1",
                       payload={"verification_id": "verify-1", "check_id": "check-1"})
        payload = {"verification_id": "verify-1", "check_id": "check-1"}
        if tamper != "missing":
            payload["producer_quiescent"] = tamper == "execution"
        runtime.record(run_id=run_id, type="verification.check_interrupted",
                       execution_id="verify-other" if tamper == "execution" else "verify-1", payload=payload)
    settle(runtime, run_id, "run.cancelled")
    with pytest.raises(OwnershipError, match="receipt|quiescence"):
        owner.seal()
    assert owner.descriptor["state"] == "busy"


def test_descriptor_reopens_but_live_owner_is_exclusive(owned):
    source, runtime, run_id, workspace, owner = owned
    settle(runtime, run_id)
    owner.seal()
    reopened = RunRuntime(RunStore(runtime.store.db_path))
    assert continuation_available(reopened, str(source), run_id)
    with pytest.raises(OwnershipError):
        WorkspaceOwnership.adopt(reopened, run_id, str(source))
    owner.stash()
    adopted = WorkspaceOwnership.adopt(reopened, run_id, str(source))
    try:
        assert adopted.workspace.root == workspace.root
        assert adopted.workspace.snapshot.snapshot_commit == workspace.snapshot.snapshot_commit
        adopted.busy()
        assert not continuation_available(reopened, str(source), run_id)
        assert reopened.get_run(run_id).workspace_snapshot["state"] == "busy"
    finally:
        adopted.lease.close()


@pytest.mark.parametrize("tamper", ["content", "source", "wrong_project", "symlink", "unsealed"])
def test_adoption_fails_closed(owned, tamper, tmp_path):
    source, runtime, run_id, workspace, owner = owned
    settle(runtime, run_id)
    if tamper != "unsealed":
        owner.seal()
    owner.lease.close()  # Simulated producer exit; free lock is NOT authority.
    project = str(source)
    if tamper == "content":
        (workspace.root / "a.txt").write_text("tampered\n")
    elif tamper == "source":
        (source / "a.txt").write_text("user changed\n")
    elif tamper == "wrong_project":
        project = str(tmp_path)
    elif tamper == "symlink":
        target = workspace.root / "a.txt"
        target.unlink()
        try:
            target.symlink_to(source / "a.txt")
        except OSError:
            pytest.skip("symlink creation unavailable")
    with pytest.raises(OwnershipError):
        WorkspaceOwnership.adopt(runtime, run_id, project)
    # A rejected claim must not leak the exclusive lease.
    probe = WorkspaceLease.acquire(runtime.store.db_path, run_id)
    probe.close()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows handle-backed restart acceptance")
def test_windows_native_workspace_seal_stash_and_adopt(owned):
    source, runtime, run_id, workspace, owner = owned
    settle(runtime, run_id)
    owner.stash()
    assert runtime.get_run(run_id).workspace_snapshot["state"] == "quiescent"
    adopted = WorkspaceOwnership.adopt(runtime, run_id, str(source))
    assert adopted.workspace.root == workspace.root
    assert adopted.descriptor["state"] == "busy"
    adopted.lease.close()
    adopted.workspace.ownership = None
    adopted.workspace.dispose()


def test_disposal_revokes_descriptor_and_releases_lease(owned):
    source, runtime, run_id, workspace, owner = owned
    settle(runtime, run_id)
    owner.seal()
    workspace.dispose()
    assert runtime.get_run(run_id).workspace_snapshot["state"] == "disposed"
    assert owner.lease.fd is None
    assert not continuation_available(runtime, str(source), run_id)


@pytest.mark.parametrize("case", ["wrong_project", "capacity", "legacy_busy"])
def test_restart_bridge_admission_does_not_claim_on_rejection(owned, monkeypatch, tmp_path, case):
    from types import SimpleNamespace
    from webhost import state
    from webhost.api import run as run_api
    from webhost.bridge import BridgeError
    from webhost.run_registry import RunRegistry
    from project import Project
    from run_runtime.models import RunStatus
    source, runtime, run_id, workspace, owner = owned
    settle(runtime, run_id)
    owner.stash()
    before = runtime.get_run(run_id).last_event_seq
    registry = RunRegistry()
    monkeypatch.setattr(run_api, "_run_registry", registry)
    monkeypatch.setattr(state, "_run_runtime", runtime)
    monkeypatch.setattr(state, "_active", Project(str(source)))
    monkeypatch.setattr(run_api, "_active", {"engine": "legacy", "coordinator": None})
    monkeypatch.setattr(run_api, "_delivery_is_busy", lambda: False)
    if case == "wrong_project":
        monkeypatch.setattr(state, "_active", Project(str(tmp_path)))
    elif case == "capacity":
        assert registry.reserve(str(source)) and registry.reserve(str(source))
    else:
        run_api._active["coordinator"] = SimpleNamespace(get_run=lambda: SimpleNamespace(status=RunStatus.RUNNING))
    with pytest.raises(BridgeError):
        run_api._restart({"runId": run_id}, SimpleNamespace())
    assert runtime.get_run(run_id).last_event_seq == before
    if case != "capacity":
        assert registry.reserve(str(source)) and registry.reserve(str(source))


def test_sealing_without_strict_capability_never_authorizes_restart(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(os, "supports_fd", set())
    owner = WorkspaceOwnership(SimpleNamespace(get_run=lambda _id: SimpleNamespace(status=SimpleNamespace(value="waiting_user"))),
        "run-test", SimpleNamespace(root=tmp_path, _disposed=False), SimpleNamespace(fd=1), {"state": "busy"})
    with pytest.raises(OwnershipError):
        owner.seal()
    assert owner.descriptor["state"] == "busy"


def test_process_receipts_cannot_launder_reused_call_id_across_executions(owned):
    _source, runtime, run_id, _workspace, owner = owned
    for execution_id in ("exec-old", "exec-new"):
        runtime.record(run_id=run_id, type="tool.started", execution_id=execution_id,
                       payload={"call_id": "reused", "tool_name": "run_process"})
    runtime.record(run_id=run_id, type="tool.completed", execution_id="exec-new",
                   payload={"call_id": "reused", "tool_name": "run_process",
                            "metadata": {"producer_quiescent": True}})
    settle(runtime, run_id)
    with pytest.raises(OwnershipError, match="completion"):
        owner.seal()


def test_duplicate_outstanding_process_start_is_rejected(owned):
    _source, runtime, run_id, _workspace, owner = owned
    for _ in range(2):
        runtime.record(run_id=run_id, type="tool.started", execution_id="exec-1",
                       payload={"call_id": "duplicate", "tool_name": "run_process"})
    settle(runtime, run_id)
    with pytest.raises(OwnershipError, match="Duplicate"):
        owner.seal()


def test_check_receipt_cannot_launder_reused_check_id_across_executions(owned):
    _source, runtime, run_id, _workspace, owner = owned
    for execution_id in ("verify-old", "verify-new"):
        runtime.record(run_id=run_id, type="verification.check_started", execution_id=execution_id,
                       payload={"verification_id": "verification", "check_id": "reused"})
    runtime.record(run_id=run_id, type="verification.check_completed", execution_id="verify-new",
                   payload={"verification_id": "verification", "check_id": "reused",
                            "producer_quiescent": True})
    settle(runtime, run_id)
    with pytest.raises(OwnershipError, match="completion"):
        owner.seal()


def test_duplicate_outstanding_check_start_is_rejected(owned):
    _source, runtime, run_id, _workspace, owner = owned
    for _ in range(2):
        runtime.record(run_id=run_id, type="verification.check_started", execution_id="verify-1",
                       payload={"verification_id": "verification", "check_id": "duplicate"})
    settle(runtime, run_id)
    with pytest.raises(OwnershipError, match="Duplicate"):
        owner.seal()


def test_builtin_pure_repository_tools_can_seal_without_subprocess_receipts(owned):
    source, runtime, run_id, _workspace, owner = owned
    for call_id, tool_name in (("repo", "repo_map"), ("search", "search_code"), ("write", "write_file")):
        runtime.record(run_id=run_id, type="tool.started", execution_id="native-1",
                       payload={"call_id": call_id, "tool_name": tool_name})
        runtime.record(run_id=run_id, type="tool.completed", execution_id="native-1",
                       payload={"call_id": call_id, "tool_name": tool_name, "metadata": {}})
    settle(runtime, run_id)
    owner.seal()
    assert owner.descriptor["state"] == "quiescent"
    assert continuation_available(runtime, str(source), run_id)


def test_subreaper_proves_detached_process_completion_for_workspace_seal(owned):
    import sys
    import psutil
    from process_runtime import ProcessRequest, ProcessRunner
    source, runtime, run_id, workspace, owner = owned
    script = ("import subprocess, sys; from pathlib import Path; "
              "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.25)'], "
              "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True); "
              "Path('child.pid').write_text(str(p.pid))")
    runtime.record(run_id=run_id, type="tool.started", payload={"call_id": "p1", "tool_name": "run_process"})
    result = ProcessRunner().run(workspace, ProcessRequest(argv=(sys.executable, "-c", script), timeout_ms=5000))
    assert result.exit_code == 0 and result.producer_quiescent is True
    child_pid = int((workspace.root / "child.pid").read_text())
    assert not psutil.pid_exists(child_pid)
    runtime.record(run_id=run_id, type="tool.completed", payload={
        "call_id": "p1", "tool_name": "run_process",
        "metadata": {"producer_quiescent": result.producer_quiescent},
    })
    settle(runtime, run_id)
    owner.seal()
    assert owner.descriptor["state"] == "quiescent"
    assert continuation_available(runtime, str(source), run_id)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux subreaper lease handoff")
def test_supervisor_keeps_workspace_lease_until_process_tree_is_empty(owned):
    import sys
    import threading
    import time
    from process_runtime import ProcessRequest, ProcessRunner
    source, runtime, run_id, workspace, owner = owned
    marker = workspace.root / "producer.pid"
    result = {}
    def execute():
        result["value"] = ProcessRunner().run(workspace, ProcessRequest((sys.executable, "-c",
            "import os, pathlib, time; pathlib.Path('producer.pid').write_text(str(os.getpid())); time.sleep(.5)"), timeout_ms=5000))
    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert marker.exists()
    owner.lease.close()
    with pytest.raises(OwnershipError):
        WorkspaceLease.acquire(runtime.store.db_path, run_id)
    thread.join(timeout=5)
    assert not thread.is_alive() and result["value"].producer_quiescent is True
    probe = WorkspaceLease.acquire(runtime.store.db_path, run_id)
    probe.close()


def _lease_child(db, run_id, pipe):
    lease = WorkspaceLease.acquire(db, run_id)
    pipe.send("locked")
    pipe.recv()
    lease.close()


def test_process_exit_releases_kernel_lease_but_not_busy_authority(owned):
    import multiprocessing
    source, runtime, run_id, workspace, owner = owned
    owner.lease.close()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_lease_child, args=(str(runtime.store.db_path), run_id, child))
    process.start()
    child.close()
    try:
        assert parent.poll(15) and parent.recv() == "locked"
        with pytest.raises(OwnershipError):
            WorkspaceLease.acquire(runtime.store.db_path, run_id)
        process.terminate()
        process.join(10)
        assert not process.is_alive()
        lease = WorkspaceLease.acquire(runtime.store.db_path, run_id)
        lease.close()
        assert not continuation_available(runtime, str(source), run_id)
        with pytest.raises(OwnershipError):
            WorkspaceOwnership.adopt(runtime, run_id, str(source))
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        parent.close()
