"""Real-subprocess integration coverage for the Worker's ACP permission
policy (acp_runtime.permission_policy.WorktreeEditAcpPermissionPolicy),
using a fake ACP agent that -- unlike tests/fixtures/acp_fake_agent.py's own
"permission" mode -- only writes to disk if the permission response it
receives actually granted the request. This is what reproduces (and now
covers) the first real end-to-end run's bug: a client that always cancels
every permission request means the Worker can never actually change
anything, and the pipeline settles as `run.completed {"reason":
"no_changes"}` with no proposal at all.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from acp_runtime.client import AcpClientRuntime
from acp_runtime.models import AcpClientLimits
from change_runtime.git import GitWorktreeChangeProvider
from executor_runtime.acp_worker import AcpWorkerAttemptAdapter, AcpWorkerLaunchProfile
from fix_runtime.models import FixTrigger, FixTriggerKind, FixWorkerRequest
from run_runtime import RunEventType, RunRuntime, RunStore
from verification_runtime.models import (
    VerificationCheckResult,
    VerificationReport,
    VerificationStatus,
)
from workspace.worktree import GitWorktreeWorkspace

_FAKE_AGENT = str(Path(__file__).resolve().parent / "fixtures" / "acp_permission_worker_agent.py")


def _git_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (source / "known.txt").write_text("source content\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return source


def _process():
    from process_runtime.models import ProcessResult

    return ProcessResult(
        argv=("true",), cwd=".", exit_code=1, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
        stdout_bytes=0, stderr_bytes=0,
    )


def _trigger() -> FixTrigger:
    report = VerificationReport(
        verification_id="initial-verification",
        plan_id="plan-1",
        results=(VerificationCheckResult("check-1", "Check", VerificationStatus.FAIL, _process()),),
        duration_ms=1,
    )
    return FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=report)


def _request() -> FixWorkerRequest:
    return FixWorkerRequest(
        task="do the thing", trigger=_trigger(), attempt_index=1, rendered_input="perform the task",
    )


def _runtime(tmp_path: Path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _adapter(tmp_path: Path, source: Path, mode: str, run_id_suffix: str, *, env: dict[str, str] | None = None):
    runtime, run = _runtime(tmp_path)
    workspace = GitWorktreeWorkspace.create(
        source_root=source, run_id=f"acp-permcheck-{run_id_suffix}", base_dir=tmp_path / "workspaces"
    )
    adapter = AcpWorkerAttemptAdapter(
        runtime, run.run_id,
        AcpWorkerLaunchProfile(command=sys.executable, args=(_FAKE_AGENT, mode), env=dict(env or {})),
        AcpClientRuntime(),
        limits=AcpClientLimits(prompt_timeout_ms=10_000),
    )
    return adapter, runtime, run, workspace


def _permission_events(runtime, run_id):
    events = runtime.events(run_id, limit=100).events
    requested = [e for e in events if e.type == RunEventType.PERMISSION_REQUESTED]
    resolved = [e for e in events if e.type == RunEventType.PERMISSION_RESOLVED]
    return requested, resolved


def test_edit_inside_worktree_is_granted_and_file_is_written(tmp_path):
    source = _git_source(tmp_path)
    adapter, runtime, run, workspace = _adapter(tmp_path, source, "edit", "edit")
    try:
        result = adapter.run(workspace, _request(), execution_id="execution-1")

        assert result.execution_id == "execution-1"
        assert (workspace.root / "target.txt").read_text(encoding="utf-8") == (
            "written by permission-aware fake agent\n"
        )
        requested, resolved = _permission_events(runtime, run.run_id)
        assert len(requested) == 1
        assert requested[0].payload["title"] == "Edit target.txt"
        assert len(resolved) == 1
        assert resolved[0].payload["outcome"] == "selected:allow_once"
        assert "worktree" in resolved[0].payload["reason"]

        # The change now actually shows up as a real proposal-worthy diff --
        # this is the exact thing the deny-all bug made impossible.
        change = GitWorktreeChangeProvider().capture(workspace)
        assert "target.txt" in change.changed_paths
        assert change.diff
    finally:
        workspace.dispose()


def test_execute_tool_call_is_always_rejected(tmp_path):
    source = _git_source(tmp_path)
    adapter, runtime, run, workspace = _adapter(tmp_path, source, "execute", "execute")
    try:
        adapter.run(workspace, _request(), execution_id="execution-1")

        assert not (workspace.root / "executed.txt").exists()
        _requested, resolved = _permission_events(runtime, run.run_id)
        assert len(resolved) == 1
        assert resolved[0].payload["outcome"] in ("selected:reject_once", "cancelled")
        assert "execute" in resolved[0].payload["reason"]
    finally:
        workspace.dispose()


def test_edit_outside_worktree_is_rejected(tmp_path):
    source = _git_source(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_target = outside_dir / "escape.txt"
    adapter, runtime, run, workspace = _adapter(
        tmp_path, source, "edit_outside", "outside",
        env={"ACP_FAKE_AGENT_OUTSIDE_PATH": str(outside_target)},
    )
    try:
        adapter.run(workspace, _request(), execution_id="execution-1")

        assert not outside_target.exists()
        _requested, resolved = _permission_events(runtime, run.run_id)
        assert len(resolved) == 1
        assert resolved[0].payload["outcome"] in ("selected:reject_once", "cancelled")
        assert "outside" in resolved[0].payload["reason"]
    finally:
        workspace.dispose()


def test_symlink_escape_from_inside_worktree_is_rejected(tmp_path):
    if sys.platform.startswith("win"):
        pytest.skip("symlink semantics differ on Windows")

    source = _git_source(tmp_path)
    outside_dir = tmp_path / "outside2"
    outside_dir.mkdir()
    outside_target = outside_dir / "escape2.txt"
    adapter, runtime, run, workspace = _adapter(
        tmp_path, source, "edit_symlink", "symlink",
        env={"ACP_FAKE_AGENT_TARGET": "escape_link.txt"},
    )
    try:
        (workspace.root / "escape_link.txt").symlink_to(outside_target)

        adapter.run(workspace, _request(), execution_id="execution-1")

        assert not outside_target.exists()
        _requested, resolved = _permission_events(runtime, run.run_id)
        assert len(resolved) == 1
        assert resolved[0].payload["outcome"] in ("selected:reject_once", "cancelled")
        assert "outside" in resolved[0].payload["reason"]
    finally:
        workspace.dispose()
