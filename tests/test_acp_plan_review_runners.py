"""Unit tests for T1.3: AcpPlanAttemptRunner / AcpReviewAttemptRunner, driven
by a structural fake ACP client (mirrors tests/test_acp_worker.py's style).

Covers: happy path (planner + reviewer), protocol error -> typed failure +
canonical review/plan.failed, workspace mutated -> failure, permission
request auto-denial does not derail a successful attempt, and that no
execution.* canonical events are ever recorded for either role.
"""

from __future__ import annotations

from pathlib import Path

import acp
import pytest

from acp_runtime.errors import AcpProtocolError
from acp_runtime.events import AcpPermissionRequested, AcpPermissionResolved, AcpSessionUpdateObserved
from acp_runtime.models import AcpClientLimits, AcpRunResult
from executor_runtime.acp_reviewer import AcpReviewAttemptRunner
from executor_runtime.acp_semantic import AcpSemanticWorkspaceMutatedError
from executor_runtime.acp_worker import AcpWorkerLaunchProfile
from executor_runtime.errors import ExecutorAdapterExecutionError, ExecutorAdapterInputError
from pipeline_runtime.acp_planner import AcpPlanAttemptRunner
from pipeline_runtime.errors import PipelineExecutionError
from planner_runtime.models import PlanReport
from review_runtime.models import ReviewReport, ReviewRequest, ReviewVerdict
from run_runtime import RunEventType, RunRuntime, RunStore
from workspace.worktree import GitWorktreeWorkspace


def _executable(path: Path) -> str:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return str(path)


def _running_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _fake_worktree(root: Path) -> GitWorktreeWorkspace:
    """A minimal structural GitWorktreeWorkspace stand-in, usable only for
    tests that never reach change_runtime.GitWorktreeChangeProvider.capture()
    (it has no `snapshot`). Tests that exercise the read-only workspace-
    mutation-detection path need a REAL workspace instead -- see
    _real_workspace below."""
    workspace = object.__new__(GitWorktreeWorkspace)
    workspace._root = root
    return workspace


def _git_source(tmp_path):
    import subprocess

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


def _real_workspace(tmp_path, run_id: str) -> GitWorktreeWorkspace:
    """A real, valid GitWorktreeWorkspace (real snapshot_commit, real Git
    worktree) -- required by every test that runs the ACP session all the
    way through run_acp_semantic_prompt, since that always captures a real
    change_runtime.GitWorktreeChangeProvider fingerprint before/after."""
    source = _git_source(tmp_path)
    return GitWorktreeWorkspace.create(source_root=source, run_id=run_id, base_dir=tmp_path / "workspaces")


_VALID_PLAN_JSON = (
    '{"summary": "Do the thing", '
    '"steps": [{"title": "Step one", "objective": "Achieve it"}], '
    '"acceptance_criteria": ["Existing tests pass"], "risks": [], '
    '"task_profile": {"complexity": "LOW", "scope": "LOCAL"}}'
)

_VALID_APPROVED_REVIEW_JSON = '{"verdict": "APPROVED", "summary": "Looks correct", "findings": []}'
_VALID_NEEDS_FIX_REVIEW_JSON = (
    '{"verdict": "NEEDS_FIX", "summary": "One issue", "findings": '
    '[{"severity": "major", "message": "Off by one"}]}'
)


class _FakeAcpClient:
    """Structural fake matching acp_runtime.client.AcpClientRuntime's async
    run() surface, without spawning a real subprocess."""

    def __init__(
        self,
        *,
        final_text: str | None = None,
        mutate_relative_path: str | None = None,
        error: Exception | None = None,
        emit_permission: bool = False,
    ) -> None:
        self.calls: list[dict] = []
        self.final_text = final_text
        self.mutate_relative_path = mutate_relative_path
        self.error = error
        self.emit_permission = emit_permission

    async def run(self, launch, request, *, limits=None, event_sink=None):
        self.calls.append({"launch": launch, "request": request, "limits": limits, "event_sink": event_sink})
        if self.mutate_relative_path is not None:
            Path(request.cwd, self.mutate_relative_path).write_text("mutated\n", encoding="utf-8")
        if self.emit_permission:
            event_sink.emit(AcpPermissionRequested("session-1", "tool-1", "Write a file", ["allow-once"]))
            event_sink.emit(AcpPermissionResolved("session-1", "tool-1", "cancelled"))
        if self.error is not None:
            raise self.error
        if self.final_text is not None:
            event_sink.emit(
                AcpSessionUpdateObserved("session-1", acp.update_agent_message_text(self.final_text), len(self.final_text))
            )
        return AcpRunResult(
            session_id="session-1",
            stop_reason="end_turn",
            update_count=1,
            update_chars=len(self.final_text or ""),
            permission_request_count=1 if self.emit_permission else 0,
            session_close_supported=True,
            session_close_succeeded=True,
        )


def _plan_runner(tmp_path, client, *, limits=None):
    runtime, run = _running_runtime(tmp_path)
    profile = AcpWorkerLaunchProfile(command=_executable(tmp_path / "agent"))
    return AcpPlanAttemptRunner(runtime, run.run_id, profile, client, limits=limits), runtime, run


def _review_runner(tmp_path, client, *, limits=None):
    runtime, run = _running_runtime(tmp_path)
    profile = AcpWorkerLaunchProfile(command=_executable(tmp_path / "agent"))
    return AcpReviewAttemptRunner(runtime, run.run_id, profile, client, limits=limits), runtime, run


def _review_request():
    return ReviewRequest(task="review the change", diff="diff --git a/x b/x\n")


# ---------------- Planner: happy path ----------------


def test_planner_happy_path_returns_plan_report_and_records_plan_completed(tmp_path):
    client = _FakeAcpClient(final_text=_VALID_PLAN_JSON)
    runner, runtime, run = _plan_runner(tmp_path, client)
    workspace = _real_workspace(tmp_path, "acp-plan-happy")

    report = runner.run(workspace, "implement the feature", plan_id="plan-1")

    assert isinstance(report, PlanReport)
    assert report.plan_id == "plan-1"
    assert report.summary == "Do the thing"
    types = [event.type for event in runtime.events(run.run_id, limit=50).events]
    assert types == [RunEventType.RUN_STARTED, RunEventType.PLAN_STARTED, RunEventType.PLAN_COMPLETED]
    assert len(client.calls) == 1


def test_planner_run_id_property(tmp_path):
    runner, _, run = _plan_runner(tmp_path, _FakeAcpClient(final_text=_VALID_PLAN_JSON))
    assert runner.run_id == run.run_id


# ---------------- Reviewer: happy path ----------------


def test_reviewer_happy_path_approved_records_review_completed(tmp_path):
    client = _FakeAcpClient(final_text=_VALID_APPROVED_REVIEW_JSON)
    runner, runtime, run = _review_runner(tmp_path, client)
    workspace = _real_workspace(tmp_path, "acp-review-happy")

    report = runner.run(workspace, _review_request(), review_id="rev-1")

    assert isinstance(report, ReviewReport)
    assert report.review_id == "rev-1"
    assert report.verdict is ReviewVerdict.APPROVED
    types = [event.type for event in runtime.events(run.run_id, limit=50).events]
    assert types == [RunEventType.RUN_STARTED, RunEventType.REVIEW_STARTED, RunEventType.REVIEW_COMPLETED]


def test_reviewer_needs_fix_is_a_normal_completion_not_a_failure(tmp_path):
    client = _FakeAcpClient(final_text=_VALID_NEEDS_FIX_REVIEW_JSON)
    runner, runtime, run = _review_runner(tmp_path, client)
    workspace = _real_workspace(tmp_path, "acp-review-needsfix")

    report = runner.run(workspace, _review_request(), review_id="rev-2")

    assert report.verdict is ReviewVerdict.NEEDS_FIX
    assert len(report.findings) == 1
    types = [event.type for event in runtime.events(run.run_id, limit=50).events]
    assert RunEventType.REVIEW_COMPLETED in types
    assert RunEventType.REVIEW_FAILED not in types


# ---------------- Protocol error ----------------


def test_planner_protocol_error_records_plan_failed_and_raises(tmp_path):
    client = _FakeAcpClient(final_text="not json at all")
    runner, runtime, run = _plan_runner(tmp_path, client)
    workspace = _real_workspace(tmp_path, "acp-plan-protoerr")

    with pytest.raises(PipelineExecutionError):
        runner.run(workspace, "implement the feature", plan_id="plan-err")

    types = [event.type for event in runtime.events(run.run_id, limit=50).events]
    assert types == [RunEventType.RUN_STARTED, RunEventType.PLAN_STARTED, RunEventType.PLAN_FAILED]
    failed_event = runtime.events(run.run_id, limit=50).events[-1]
    assert failed_event.payload["error_type"] == "PlannerProtocolError"


def test_reviewer_protocol_error_records_review_failed_and_raises(tmp_path):
    client = _FakeAcpClient(final_text="```markdown fenced```")
    runner, runtime, run = _review_runner(tmp_path, client)
    workspace = _real_workspace(tmp_path, "acp-review-protoerr")

    with pytest.raises(ExecutorAdapterExecutionError):
        runner.run(workspace, _review_request(), review_id="rev-err")

    types = [event.type for event in runtime.events(run.run_id, limit=50).events]
    assert types == [RunEventType.RUN_STARTED, RunEventType.REVIEW_STARTED, RunEventType.REVIEW_FAILED]
    failed_event = runtime.events(run.run_id, limit=50).events[-1]
    assert failed_event.payload["error_type"] == "ReviewProtocolError"


# ---------------- Workspace mutated ----------------


def test_planner_workspace_mutation_fails_the_attempt(tmp_path):
    workspace = _real_workspace(tmp_path, "acp-plan-mut")
    source = workspace.snapshot.source_root
    try:
        client = _FakeAcpClient(final_text=_VALID_PLAN_JSON, mutate_relative_path="mutated.txt")
        runner, runtime, run = _plan_runner(tmp_path, client)

        with pytest.raises(PipelineExecutionError) as raised:
            runner.run(workspace, "implement the feature", plan_id="plan-mut")

        assert isinstance(raised.value.__cause__, AcpSemanticWorkspaceMutatedError)
        types = [event.type for event in runtime.events(run.run_id, limit=50).events]
        assert types == [RunEventType.RUN_STARTED, RunEventType.PLAN_STARTED, RunEventType.PLAN_FAILED]
        assert not (source / "mutated.txt").exists()
    finally:
        workspace.dispose()


def test_reviewer_workspace_mutation_fails_the_attempt(tmp_path):
    workspace = _real_workspace(tmp_path, "acp-review-mut")
    source = workspace.snapshot.source_root
    try:
        client = _FakeAcpClient(final_text=_VALID_APPROVED_REVIEW_JSON, mutate_relative_path="mutated.txt")
        runner, runtime, run = _review_runner(tmp_path, client)

        with pytest.raises(ExecutorAdapterExecutionError) as raised:
            runner.run(workspace, _review_request(), review_id="rev-mut")

        assert isinstance(raised.value.__cause__, AcpSemanticWorkspaceMutatedError)
        types = [event.type for event in runtime.events(run.run_id, limit=50).events]
        assert types == [RunEventType.RUN_STARTED, RunEventType.REVIEW_STARTED, RunEventType.REVIEW_FAILED]
        assert not (source / "mutated.txt").exists()
    finally:
        workspace.dispose()


# ---------------- Permission auto-denial does not derail success ----------------


def test_planner_permission_request_is_auto_denied_and_attempt_still_succeeds(tmp_path):
    client = _FakeAcpClient(final_text=_VALID_PLAN_JSON, emit_permission=True)
    runner, runtime, run = _plan_runner(tmp_path, client)
    workspace = _real_workspace(tmp_path, "acp-plan-perm")

    report = runner.run(workspace, "implement the feature", plan_id="plan-perm")

    assert report.plan_id == "plan-perm"
    types = [event.type for event in runtime.events(run.run_id, limit=50).events]
    assert types == [RunEventType.RUN_STARTED, RunEventType.PLAN_STARTED, RunEventType.PLAN_COMPLETED]
    assert RunEventType.PERMISSION_REQUESTED not in types


# ---------------- No execution.* events ever ----------------


def test_planner_never_records_execution_events(tmp_path):
    client = _FakeAcpClient(final_text=_VALID_PLAN_JSON, emit_permission=True)
    runner, runtime, run = _plan_runner(tmp_path, client)
    workspace = _real_workspace(tmp_path, "acp-plan-execcheck")

    runner.run(workspace, "implement the feature", plan_id="plan-exec-check")

    types = {event.type for event in runtime.events(run.run_id, limit=50).events}
    assert not any(str(t).startswith("execution.") for t in types)


def test_reviewer_never_records_execution_events(tmp_path):
    client = _FakeAcpClient(final_text=_VALID_APPROVED_REVIEW_JSON, emit_permission=True)
    runner, runtime, run = _review_runner(tmp_path, client)
    workspace = _real_workspace(tmp_path, "acp-review-execcheck")

    runner.run(workspace, _review_request(), review_id="rev-exec-check")

    types = {event.type for event in runtime.events(run.run_id, limit=50).events}
    assert not any(str(t).startswith("execution.") for t in types)


# ---------------- ACP transport/infrastructure failure ----------------


def test_planner_acp_transport_failure_records_plan_failed(tmp_path):
    client = _FakeAcpClient(error=AcpProtocolError("ACP unavailable"))
    runner, runtime, run = _plan_runner(tmp_path, client)
    workspace = _real_workspace(tmp_path, "acp-plan-infra")

    with pytest.raises(PipelineExecutionError):
        runner.run(workspace, "implement the feature", plan_id="plan-infra")

    types = [event.type for event in runtime.events(run.run_id, limit=50).events]
    assert types == [RunEventType.RUN_STARTED, RunEventType.PLAN_STARTED, RunEventType.PLAN_FAILED]


def test_reviewer_acp_transport_failure_records_review_failed(tmp_path):
    client = _FakeAcpClient(error=AcpProtocolError("ACP unavailable"))
    runner, runtime, run = _review_runner(tmp_path, client)
    workspace = _real_workspace(tmp_path, "acp-review-infra")

    with pytest.raises(ExecutorAdapterExecutionError):
        runner.run(workspace, _review_request(), review_id="rev-infra")

    types = [event.type for event in runtime.events(run.run_id, limit=50).events]
    assert types == [RunEventType.RUN_STARTED, RunEventType.REVIEW_STARTED, RunEventType.REVIEW_FAILED]


# ---------------- Input validation ----------------


def test_review_runner_rejects_non_review_request(tmp_path):
    runner, _, _ = _review_runner(tmp_path, _FakeAcpClient(final_text=_VALID_APPROVED_REVIEW_JSON))
    with pytest.raises(ExecutorAdapterInputError):
        runner.run(_fake_worktree(tmp_path), object(), review_id="bad")


def test_plan_and_review_runners_reject_bad_launch_profile_type(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    with pytest.raises(Exception):
        AcpPlanAttemptRunner(runtime, run.run_id, object(), _FakeAcpClient())
    with pytest.raises(Exception):
        AcpReviewAttemptRunner(runtime, run.run_id, object(), _FakeAcpClient())
