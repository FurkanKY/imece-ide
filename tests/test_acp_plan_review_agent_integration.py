"""Integration coverage for T1.3: AcpPlanAttemptRunner/AcpReviewAttemptRunner
driven through a REAL local ACP agent subprocess (the official
agent-client-protocol SDK's stdio transport, via acp_runtime.client.
AcpClientRuntime), using the new tests/fixtures/acp_plan_review_fake_agent.py
fixture -- never the ACP Worker's existing fixture (see T1.3 scope: only new
fixture files, not edits to the existing one).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from acp_runtime.client import AcpClientRuntime
from acp_runtime.models import AcpClientLimits
from executor_runtime.acp_reviewer import AcpReviewAttemptRunner
from executor_runtime.acp_semantic import AcpSemanticWorkspaceMutatedError
from executor_runtime.acp_worker import AcpWorkerLaunchProfile
from executor_runtime.errors import ExecutorAdapterExecutionError
from pipeline_runtime.acp_planner import AcpPlanAttemptRunner
from pipeline_runtime.errors import PipelineExecutionError
from planner_runtime.models import PlanReport
from review_runtime.models import ReviewReport, ReviewRequest, ReviewVerdict
from run_runtime import RunEventType, RunRuntime, RunStore
from workspace.worktree import GitWorktreeWorkspace

_FAKE_AGENT = str(Path(__file__).resolve().parent / "fixtures" / "acp_plan_review_fake_agent.py")

_VALID_PLAN_JSON = (
    '{"summary": "Do the thing", '
    '"steps": [{"title": "Step one", "objective": "Achieve it"}], '
    '"acceptance_criteria": ["Existing tests pass"], "risks": [], '
    '"task_profile": {"complexity": "LOW", "scope": "LOCAL"}}'
)
_VALID_APPROVED_REVIEW_JSON = '{"verdict": "APPROVED", "summary": "Looks correct", "findings": []}'


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


def _running_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _profile(mode: str, *, text: str | None = None) -> AcpWorkerLaunchProfile:
    """AcpLaunchSpec.env is the EXACT child environment (never merged with
    the test process's own os.environ -- see acp_runtime.models), so the
    fixture's ACP_FAKE_TEXT must be passed here, not via monkeypatch.setenv
    on the pytest process."""
    env = {"ACP_FAKE_TEXT": text} if text is not None else {}
    return AcpWorkerLaunchProfile(command=sys.executable, args=(_FAKE_AGENT, mode), env=env)


def _workspace(tmp_path, run_id):
    source = _git_source(tmp_path)
    return GitWorktreeWorkspace.create(source_root=source, run_id=run_id, base_dir=tmp_path / "workspaces")


def test_real_acp_planner_happy_path(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    runner = AcpPlanAttemptRunner(
        runtime, run.run_id, _profile("text", text=_VALID_PLAN_JSON), AcpClientRuntime(),
        limits=AcpClientLimits(prompt_timeout_ms=10_000),
    )
    workspace = _workspace(tmp_path, "acp-real-plan")
    try:
        report = runner.run(workspace, "implement the feature", plan_id="plan-real-1")
        assert isinstance(report, PlanReport)
        assert report.summary == "Do the thing"
        types = [event.type for event in runtime.events(run.run_id, limit=50).events]
        assert types == [RunEventType.RUN_STARTED, RunEventType.PLAN_STARTED, RunEventType.PLAN_COMPLETED]
    finally:
        workspace.dispose()


def test_real_acp_reviewer_happy_path(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    runner = AcpReviewAttemptRunner(
        runtime, run.run_id, _profile("text", text=_VALID_APPROVED_REVIEW_JSON), AcpClientRuntime(),
        limits=AcpClientLimits(prompt_timeout_ms=10_000),
    )
    workspace = _workspace(tmp_path, "acp-real-review")
    try:
        report = runner.run(
            workspace, ReviewRequest(task="review the change", diff="diff --git a/x b/x\n"), review_id="rev-real-1"
        )
        assert isinstance(report, ReviewReport)
        assert report.verdict is ReviewVerdict.APPROVED
        types = [event.type for event in runtime.events(run.run_id, limit=50).events]
        assert types == [RunEventType.RUN_STARTED, RunEventType.REVIEW_STARTED, RunEventType.REVIEW_COMPLETED]
    finally:
        workspace.dispose()


def test_real_acp_thought_chunks_are_dropped_from_final_text(tmp_path):
    """agent_thought_chunk content must never leak into the parsed decision:
    if it did, this test's thought text (the plain-text sentinel below) would
    break parsing even though the real agent_message_chunk carries valid
    JSON, since both reuse the same $ACP_FAKE_TEXT value in 'thought' mode."""
    runtime, run = _running_runtime(tmp_path)
    runner = AcpPlanAttemptRunner(
        runtime, run.run_id, _profile("thought", text=_VALID_PLAN_JSON), AcpClientRuntime(),
        limits=AcpClientLimits(prompt_timeout_ms=10_000),
    )
    workspace = _workspace(tmp_path, "acp-real-thought")
    try:
        report = runner.run(workspace, "implement the feature", plan_id="plan-real-thought")
        assert report.summary == "Do the thing"
    finally:
        workspace.dispose()


def test_real_acp_planner_protocol_error(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    runner = AcpPlanAttemptRunner(
        runtime, run.run_id, _profile("text", text="this is not json"), AcpClientRuntime(),
        limits=AcpClientLimits(prompt_timeout_ms=10_000),
    )
    workspace = _workspace(tmp_path, "acp-real-plan-protoerr")
    try:
        with pytest.raises(PipelineExecutionError):
            runner.run(workspace, "implement the feature", plan_id="plan-real-err")
        types = [event.type for event in runtime.events(run.run_id, limit=50).events]
        assert types == [RunEventType.RUN_STARTED, RunEventType.PLAN_STARTED, RunEventType.PLAN_FAILED]
    finally:
        workspace.dispose()


def test_real_acp_reviewer_workspace_mutation_is_caught(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    runner = AcpReviewAttemptRunner(
        runtime, run.run_id, _profile("mutate", text=_VALID_APPROVED_REVIEW_JSON), AcpClientRuntime(),
        limits=AcpClientLimits(prompt_timeout_ms=10_000),
    )
    workspace = _workspace(tmp_path, "acp-real-review-mutate")
    try:
        with pytest.raises(ExecutorAdapterExecutionError) as raised:
            runner.run(
                workspace, ReviewRequest(task="review the change", diff="diff --git a/x b/x\n"), review_id="rev-real-mut"
            )
        assert isinstance(raised.value.__cause__, AcpSemanticWorkspaceMutatedError)
        types = [event.type for event in runtime.events(run.run_id, limit=50).events]
        assert types == [RunEventType.RUN_STARTED, RunEventType.REVIEW_STARTED, RunEventType.REVIEW_FAILED]
    finally:
        workspace.dispose()


def test_real_acp_planner_permission_request_auto_denied_still_succeeds(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    runner = AcpPlanAttemptRunner(
        runtime, run.run_id, _profile("permission", text=_VALID_PLAN_JSON), AcpClientRuntime(),
        limits=AcpClientLimits(prompt_timeout_ms=10_000),
    )
    workspace = _workspace(tmp_path, "acp-real-plan-perm")
    try:
        report = runner.run(workspace, "implement the feature", plan_id="plan-real-perm")
        assert report.plan_id == "plan-real-perm"
        types = [event.type for event in runtime.events(run.run_id, limit=50).events]
        assert types == [RunEventType.RUN_STARTED, RunEventType.PLAN_STARTED, RunEventType.PLAN_COMPLETED]
    finally:
        workspace.dispose()


def test_real_acp_planner_failure_records_plan_failed(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    runner = AcpPlanAttemptRunner(
        runtime, run.run_id, _profile("fail"), AcpClientRuntime(),
        limits=AcpClientLimits(prompt_timeout_ms=10_000),
    )
    workspace = _workspace(tmp_path, "acp-real-plan-fail")
    try:
        with pytest.raises(PipelineExecutionError):
            runner.run(workspace, "implement the feature", plan_id="plan-real-fail")
        types = [event.type for event in runtime.events(run.run_id, limit=50).events]
        assert types == [RunEventType.RUN_STARTED, RunEventType.PLAN_STARTED, RunEventType.PLAN_FAILED]
    finally:
        workspace.dispose()
