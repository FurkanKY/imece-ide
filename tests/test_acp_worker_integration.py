"""Integration coverage for ACP Worker composition with FixLoopRunner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from acp_runtime.errors import AcpProtocolError
from acp_runtime.client import AcpClientRuntime
from acp_runtime.models import AcpClientLimits, AcpRunResult
from change_runtime.models import WorkspaceChangeSet
from executor_runtime.acp_worker import AcpWorkerAttemptAdapter, AcpWorkerLaunchProfile
from fix_runtime.models import FixLoopRequest, FixLoopStatus, FixTrigger, FixTriggerKind, FixWorkerRequest
from fix_runtime.runner import FixLoopRunner
from process_runtime.models import ProcessRequest, ProcessResult
from review_runtime.models import ReviewReport, ReviewVerdict
from run_runtime import RunEventSpec, RunEventType, RunRuntime, RunStore
from verification_runtime.models import (
    VerificationCheck,
    VerificationCheckResult,
    VerificationPlan,
    VerificationReport,
    VerificationStatus,
)
from workspace.worktree import GitWorktreeWorkspace

_FAKE_AGENT = str(Path(__file__).resolve().parent / "fixtures" / "acp_fake_agent.py")


def _worktree(root: Path) -> GitWorktreeWorkspace:
    workspace = object.__new__(GitWorktreeWorkspace)
    workspace._root = root
    return workspace


def _runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _process(exit_code=1):
    return ProcessResult(
        argv=("true",), cwd=".", exit_code=exit_code, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
        stdout_bytes=0, stderr_bytes=0,
    )


def _trigger():
    report = VerificationReport(
        verification_id="initial-verification",
        plan_id="plan-1",
        results=(VerificationCheckResult("check-1", "Check", VerificationStatus.FAIL, _process()),),
        duration_ms=1,
    )
    return FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=report)


def _plan():
    return VerificationPlan(
        "plan-1", (VerificationCheck("check-1", "Check", ProcessRequest(("true",))),)
    )


class _ChangeProvider:
    def capture(self, workspace):
        marker = workspace.root / "changed.txt"
        if marker.exists():
            return WorkspaceChangeSet(diff=marker.read_text(encoding="utf-8"), changed_paths=("changed.txt",))
        return WorkspaceChangeSet(diff="", changed_paths=())


class _AcpClient:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    async def run(self, launch, request, *, limits=None, event_sink=None):
        self.calls.append((launch, request, limits, event_sink))
        if self.error is not None:
            raise self.error
        Path(request.cwd, "changed.txt").write_text("ACP change\n", encoding="utf-8")
        return AcpRunResult(
            session_id="session-1", stop_reason="completed", update_count=0,
            update_chars=0, permission_request_count=0,
            session_close_supported=True, session_close_succeeded=True,
        )


class _Verification:
    def __init__(self, runtime, run_id):
        self.runtime = runtime
        self.run_id = run_id

    def run(self, workspace, plan, *, verification_id):
        report = VerificationReport(
            verification_id=verification_id, plan_id=plan.plan_id,
            results=(VerificationCheckResult("check-1", "Check", VerificationStatus.PASS, _process(0)),),
            duration_ms=1,
        )
        self.runtime.record_many(
            run_id=self.run_id,
            specs=(
                RunEventSpec(
                    type=RunEventType.VERIFICATION_STARTED,
                    payload={"verification_id": verification_id, "plan_id": plan.plan_id, "check_count": 1},
                    correlation_id=verification_id, source="verification",
                ),
                RunEventSpec(
                    type=RunEventType.VERIFICATION_COMPLETED,
                    payload={
                        "verification_id": verification_id, "plan_id": plan.plan_id,
                        "status": "pass", "duration_ms": 1,
                        "counts": {"pass": 1, "fail": 0, "timeout": 0, "error": 0, "total": 1},
                    },
                    correlation_id=verification_id, source="verification",
                ),
            ),
        )
        return report


class _Reviewer:
    def __init__(self, runtime, run_id):
        self.runtime = runtime
        self.run_id = run_id

    def run(self, workspace, request, *, review_id):
        report = ReviewReport(
            review_id=review_id, verdict=ReviewVerdict.APPROVED, summary="approved", findings=(),
            repository_fingerprint="a" * 64, diff_sha256=request.diff_sha256,
            verification_id=request.verification_report.verification_id,
            verification_status="pass",
        )
        self.runtime.record_many(
            run_id=self.run_id,
            specs=(
                RunEventSpec(
                    type=RunEventType.REVIEW_STARTED, payload={"review_id": review_id},
                    correlation_id=review_id, source="reviewer",
                ),
                RunEventSpec(
                    type=RunEventType.REVIEW_COMPLETED,
                    payload={
                        "review_id": review_id, "verdict": "APPROVED", "note": "approved",
                        "summary": "approved", "findings": [],
                        "repository_fingerprint": "a" * 64,
                        "diff_sha256": request.diff_sha256,
                        "verification_id": request.verification_report.verification_id,
                        "verification_status": "pass",
                    },
                    correlation_id=review_id, source="reviewer",
                ),
            ),
        )
        return report


def _runner(tmp_path, client):
    runtime, run = _runtime(tmp_path)
    adapter = AcpWorkerAttemptAdapter(
        runtime, run.run_id,
        AcpWorkerLaunchProfile(command=str(tmp_path / "agent")),
        client,
    )
    (tmp_path / "agent").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "agent").chmod(0o755)
    runner = FixLoopRunner(
        runtime,
        worker=adapter,
        verification=_Verification(runtime, run.run_id),
        reviewer=_Reviewer(runtime, run.run_id),
        change_provider=_ChangeProvider(),
    )
    return runner, runtime, run


def _request():
    return FixLoopRequest(
        task="fix the file", trigger=_trigger(), verification_plan=_plan(), max_fix_attempts=1,
    )


def test_fixloop_order_is_fix_attempt_started_then_acp_lifecycle_then_completed(tmp_path):
    runner, runtime, run = _runner(tmp_path, _AcpClient())

    report = runner.run(run.run_id, _worktree(tmp_path), _request())

    assert report.status is FixLoopStatus.COMPLETED
    types = [event.type for event in runtime.events(run.run_id, limit=100).events]
    assert types.index(RunEventType.FIX_ATTEMPT_STARTED) < types.index(RunEventType.EXECUTION_STARTED)
    assert types.index(RunEventType.EXECUTION_STARTED) < types.index(RunEventType.EXECUTION_COMPLETED)
    assert types.index(RunEventType.EXECUTION_COMPLETED) < types.index(RunEventType.FIX_ATTEMPT_COMPLETED)


def test_fixloop_accepts_acp_worker_attempt_result_without_modification(tmp_path):
    runner, runtime, run = _runner(tmp_path, _AcpClient())

    report = runner.run(run.run_id, _worktree(tmp_path), _request())

    assert report.final_execution_id
    assert report.status is FixLoopStatus.COMPLETED
    execution_events = [
        event for event in runtime.events(run.run_id, limit=100).events
        if event.type in {RunEventType.EXECUTION_STARTED, RunEventType.EXECUTION_COMPLETED}
    ]
    assert len(execution_events) == 2
    assert execution_events[0].execution_id == report.final_execution_id
    assert execution_events[1].execution_id == report.final_execution_id


def test_acp_failure_reaches_existing_fixloop_infrastructure_settlement(tmp_path):
    runner, runtime, run = _runner(tmp_path, _AcpClient(error=AcpProtocolError("ACP unavailable")))

    with pytest.raises(Exception, match="Worker port failed"):
        runner.run(run.run_id, _worktree(tmp_path), _request())

    types = [event.type for event in runtime.events(run.run_id, limit=100).events]
    assert RunEventType.EXECUTION_FAILED in types
    assert RunEventType.FIX_ATTEMPT_INTERRUPTED in types
    assert RunEventType.FIX_LOOP_FAILED in types


def _git_source(tmp_path):
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


def _real_worker_request():
    return FixWorkerRequest(
        task="mutate the known file in the worker workspace",
        trigger=_trigger(), attempt_index=1, rendered_input="perform the mutation",
    )


def _real_adapter(tmp_path, source, mode):
    runtime, run = _runtime(tmp_path)
    workspace = GitWorktreeWorkspace.create(
        source_root=source, run_id=f"acp-{mode}", base_dir=tmp_path / "workspaces"
    )
    adapter = AcpWorkerAttemptAdapter(
        runtime, run.run_id,
        AcpWorkerLaunchProfile(
            command=sys.executable, args=(_FAKE_AGENT, mode), env={},
        ),
        AcpClientRuntime(),
        limits=AcpClientLimits(prompt_timeout_ms=10_000),
    )
    return adapter, runtime, run, workspace


def test_real_acp_worker_mutates_shadow_worktree_not_source_repo(tmp_path):
    source = _git_source(tmp_path)
    adapter, runtime, run, workspace = _real_adapter(tmp_path, source, "mutate")
    try:
        result = adapter.run(workspace, _real_worker_request(), execution_id="execution-real-1")

        assert result.execution_id == "execution-real-1"
        assert (workspace.root / "acp_worker_mutated.txt").read_text(encoding="utf-8") == "mutated by ACP\n"
        assert not (source / "acp_worker_mutated.txt").exists()
        assert (source / "known.txt").read_text(encoding="utf-8") == "source content\n"
        assert runtime.events(run.run_id, limit=100).events[-1].type == RunEventType.EXECUTION_COMPLETED
    finally:
        workspace.dispose()


def test_real_acp_success_has_execution_completed(tmp_path):
    source = _git_source(tmp_path)
    adapter, runtime, run, workspace = _real_adapter(tmp_path, source, "echo")
    try:
        adapter.run(workspace, _real_worker_request(), execution_id="execution-real-2")
        types = [event.type for event in runtime.events(run.run_id, limit=100).events]
        assert types.count(RunEventType.EXECUTION_STARTED) == 1
        assert types.count(RunEventType.EXECUTION_COMPLETED) == 1
        assert RunEventType.EXECUTION_FAILED not in types
    finally:
        workspace.dispose()


def test_real_acp_failure_has_execution_failed_and_no_completed_event(tmp_path):
    source = _git_source(tmp_path)
    adapter, runtime, run, workspace = _real_adapter(tmp_path, source, "fail")
    try:
        with pytest.raises(Exception):
            adapter.run(workspace, _real_worker_request(), execution_id="execution-real-3")
        types = [event.type for event in runtime.events(run.run_id, limit=100).events]
        assert types.count(RunEventType.EXECUTION_STARTED) == 1
        assert types.count(RunEventType.EXECUTION_FAILED) == 1
        assert RunEventType.EXECUTION_COMPLETED not in types
    finally:
        workspace.dispose()
