"""Integration proof for T1.3: PipelineRunner composes AcpPlanAttemptRunner
and AcpReviewAttemptRunner (both driven through a REAL local ACP agent
subprocess, via tests/fixtures/acp_plan_review_fake_agent.py) together with
the existing native Worker (ScriptedBackend) and Verification adapters, on a
real GitWorktreeWorkspace -- mirroring tests/test_pipeline_integration.py's
pattern, with the Planner/Reviewer legs swapped for their ACP counterparts.

NOTE (per task instructions): PipelineRunner is being modified concurrently
by another task. This test was written last and adapted to what is on disk;
if PipelineRunner's shape changes again after this file is written, that is
for the planner to reconcile, not this task to chase.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from acp_runtime.client import AcpClientRuntime
from acp_runtime.models import AcpClientLimits
from agent_runtime import ModelStopReason, ModelToolCall, ModelTurn, ModelUsage
from change_runtime import GitWorktreeChangeProvider
from executor_runtime.acp_reviewer import AcpReviewAttemptRunner
from executor_runtime.acp_worker import AcpWorkerLaunchProfile
from executor_runtime.native_verification import NativeVerificationAttemptAdapter
from executor_runtime.native_worker import NativeWorkerAttemptAdapter
from pipeline_runtime.acp_planner import AcpPlanAttemptRunner
from pipeline_runtime.models import PipelineStatus
from pipeline_runtime.runner import PipelineRunner
from process_runtime.models import ProcessResult
from run_runtime import RunEventType, RunRuntime, RunStore
from workspace.worktree import GitWorktreeWorkspace
from acp_test_support import fixture_child_env

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")

_FAKE_AGENT = str(Path(__file__).resolve().parent / "fixtures" / "acp_plan_review_fake_agent.py")

_PLAN_JSON = (
    '{"summary":"Fix the bug in a.txt.","steps":[{"title":"Step 1","objective":"Fix it."}],'
    '"acceptance_criteria":["a.txt says fixed"],"risks":[],'
    '"task_profile":{"complexity":"LOW","scope":"LOCAL"}}'
)
_APPROVED_REVIEW_JSON = '{"verdict":"APPROVED","summary":"Good fix.","findings":[]}'


class ScriptedSession:
    def __init__(self, turns):
        self.turns = list(turns)

    def respond(self, input_items):
        value = self.turns.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ScriptedBackend:
    def __init__(self, turns):
        self.session = ScriptedSession(turns)

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        return self.session


class FakeProcessRunner:
    def __init__(self, results):
        self._results = list(results)

    def run(self, workspace, request, *, cancel_token=None):
        return self._results.pop(0)


def _completed_turn(text):
    return ModelTurn(text, (), ModelStopReason.COMPLETED, ModelUsage())


def _process_result(exit_code=0):
    return ProcessResult(
        argv=("true",), cwd=".", exit_code=exit_code, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )


def _acp_profile(*, text: str) -> AcpWorkerLaunchProfile:
    # AcpLaunchSpec.env is the EXACT child environment; Windows needs its
    # minimal OS variables present or the Python child never starts.
    return AcpWorkerLaunchProfile(
        command=sys.executable, args=(_FAKE_AGENT, "text"),
        env=fixture_child_env({"ACP_FAKE_TEXT": text}),
    )


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


@pytest.fixture
def repo_workspace(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()

    def _git(args):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)

    _git(["init", "-q"])
    _git(["config", "user.name", "T"])
    _git(["config", "user.email", "t@example.com"])
    (source / "a.txt").write_text("buggy\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_a.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    _git(["add", "-A"])
    _git(["commit", "-q", "-m", "init"])

    ws = GitWorktreeWorkspace.create(source_root=source, run_id="pipeline-acp-integration", base_dir=tmp_path / "workspaces")
    yield ws
    ws.dispose()


def test_pipeline_with_acp_planner_and_acp_reviewer_approved_first_try(tmp_path, repo_workspace):
    runtime, run = setup_runtime(tmp_path)

    planner = AcpPlanAttemptRunner(
        runtime, run.run_id, _acp_profile(text=_PLAN_JSON), AcpClientRuntime(),
        limits=AcpClientLimits(prompt_timeout_ms=10_000),
    )

    worker_backend = ScriptedBackend([
        ModelTurn(
            "", (ModelToolCall("c1", "write_file", {"path": "a.txt", "content": "fixed\n"}),),
            ModelStopReason.TOOL_USE, ModelUsage(),
        ),
        _completed_turn("Fixed the bug."),
    ])
    worker = NativeWorkerAttemptAdapter(runtime, run.run_id, worker_backend)

    verification = NativeVerificationAttemptAdapter(
        runtime, run.run_id, process_runner=FakeProcessRunner([_process_result(0)]),
    )

    reviewer = AcpReviewAttemptRunner(
        runtime, run.run_id, _acp_profile(text=_APPROVED_REVIEW_JSON), AcpClientRuntime(),
        limits=AcpClientLimits(prompt_timeout_ms=10_000),
    )

    change_provider = GitWorktreeChangeProvider()

    pipeline = PipelineRunner(
        runtime, planner=planner, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=change_provider,
    )

    stages = []
    report = pipeline.run(run.run_id, repo_workspace, "Fix the bug in a.txt", on_stage=lambda s, i: stages.append(s))

    assert report.status is PipelineStatus.NEEDS_USER
    assert report.reason == "reviewed"
    assert (repo_workspace.root / "a.txt").read_text(encoding="utf-8") == "fixed\n"
    assert stages == ["planning", "working", "verifying", "reviewing", "done"]

    events = runtime.events(run.run_id, limit=500).events
    types = [e.type for e in events]

    assert RunEventType.PLAN_STARTED in types
    assert RunEventType.PLAN_COMPLETED in types
    assert RunEventType.EXECUTION_STARTED in types
    assert RunEventType.EXECUTION_COMPLETED in types
    assert RunEventType.VERIFICATION_STARTED in types
    assert RunEventType.VERIFICATION_COMPLETED in types
    assert RunEventType.REVIEW_STARTED in types
    assert RunEventType.REVIEW_COMPLETED in types
    assert RunEventType.PROPOSAL_READY in types
    assert RunEventType.RUN_WAITING_USER in types
    assert RunEventType.RUN_COMPLETED not in types
    assert runtime.get_run(run.run_id).status.value == "waiting_user"

    # Planner/Reviewer activity (ACP-driven here) must never be mistaken for
    # Worker execution activity: exactly one execution.started/completed
    # pair (the Worker's), and every planner/reviewer-sourced event carries
    # execution_id=None.
    assert types.count(RunEventType.EXECUTION_STARTED) == 1
    assert types.count(RunEventType.EXECUTION_COMPLETED) == 1
    planner_events = [e for e in events if e.source == "planner"]
    reviewer_events = [e for e in events if e.source == "reviewer"]
    assert planner_events and reviewer_events
    assert all(e.execution_id is None for e in planner_events)
    assert all(e.execution_id is None for e in reviewer_events)
