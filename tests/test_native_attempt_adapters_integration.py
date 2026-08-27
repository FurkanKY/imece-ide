"""Integration proof: the real 3J1 adapters plug into the real, unmodified
FixLoopRunner (fix_runtime.runner) without changing FixLoopRunner or
fix_runtime/ports.py.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import ModelStopReason, ModelToolCall, ModelTurn, ModelUsage  # noqa: E402
from change_runtime import GitWorktreeChangeProvider  # noqa: E402
from executor_runtime.native_reviewer import NativeReviewAttemptAdapter  # noqa: E402
from executor_runtime.native_verification import NativeVerificationAttemptAdapter  # noqa: E402
from executor_runtime.native_worker import NativeWorkerAttemptAdapter  # noqa: E402
from fix_runtime.models import FixLoopRequest, FixLoopStatus, FixTrigger, FixTriggerKind  # noqa: E402
from fix_runtime.runner import FixLoopRunner  # noqa: E402
from process_runtime.models import ProcessRequest, ProcessResult  # noqa: E402
from review_runtime.runner import ReviewerRunner  # noqa: E402
from run_runtime import RunEventType, RunRuntime, RunStore  # noqa: E402
from verification_runtime.models import VerificationCheck, VerificationCheckResult, VerificationPlan, VerificationReport, VerificationStatus  # noqa: E402
from workspace.worktree import GitWorktreeWorkspace  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")


# ---------------- shared scripted-model helpers ----------------


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
        self.calls = 0

    def run(self, workspace, request):
        self.calls += 1
        return self._results.pop(0)


def _completed_turn(text):
    return ModelTurn(text, (), ModelStopReason.COMPLETED, ModelUsage())


def _process_result(exit_code=0):
    return ProcessResult(
        argv=("true",), cwd=".", exit_code=exit_code, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
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
    _git(["add", "-A"])
    _git(["commit", "-q", "-m", "init"])

    ws = GitWorktreeWorkspace.create(source_root=source, run_id="fixloop-integration", base_dir=tmp_path / "workspaces")
    yield ws
    ws.dispose()


def _initial_fail_trigger():
    result = VerificationCheckResult("c1", "Check", VerificationStatus.FAIL, _process_result(1))
    report = VerificationReport(verification_id="ver-initial", plan_id="plan-1", results=(result,), duration_ms=1)
    return FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=report)


def _verification_plan():
    return VerificationPlan(
        plan_id="plan-1", checks=(VerificationCheck("c1", "Check", ProcessRequest(argv=("true",))),),
    )


def test_real_adapters_plug_into_real_fix_loop_runner_end_to_end(tmp_path, repo_workspace):
    runtime, run = setup_runtime(tmp_path)

    # Worker: one TOOL_USE turn writing the fix, then COMPLETED.
    worker_backend = ScriptedBackend([
        ModelTurn(
            "", (ModelToolCall("c1", "write_file", {"path": "a.txt", "content": "fixed\n"}),),
            ModelStopReason.TOOL_USE, ModelUsage(),
        ),
        _completed_turn("Fixed the bug."),
    ])
    worker = NativeWorkerAttemptAdapter(runtime, run.run_id, worker_backend)

    # Verification: scripted PASS via a fake ProcessRunner.
    verification = NativeVerificationAttemptAdapter(
        runtime, run.run_id, process_runner=FakeProcessRunner([_process_result(0)]),
    )

    # Reviewer: scripted APPROVED via a real ReviewerRunner.
    review_backend = ScriptedBackend([_completed_turn('{"verdict":"APPROVED","summary":"Good fix.","findings":[]}')])
    reviewer = NativeReviewAttemptAdapter(runtime, run.run_id, ReviewerRunner(review_backend))

    change_provider = GitWorktreeChangeProvider()

    fix_loop = FixLoopRunner(
        runtime, worker=worker, verification=verification, reviewer=reviewer, change_provider=change_provider,
    )

    request = FixLoopRequest(
        task="Fix the bug in a.txt", trigger=_initial_fail_trigger(),
        verification_plan=_verification_plan(), max_fix_attempts=1,
    )

    report = fix_loop.run(run.run_id, repo_workspace, request)

    assert report.status is FixLoopStatus.COMPLETED
    assert (repo_workspace.root / "a.txt").read_text(encoding="utf-8") == "fixed\n"

    events = runtime.events(run.run_id, limit=500).events

    worker_execution_id = report.final_execution_id
    exec_started = [e for e in events if e.type == RunEventType.EXECUTION_STARTED and e.execution_id == worker_execution_id]
    exec_completed = [e for e in events if e.type == RunEventType.EXECUTION_COMPLETED and e.execution_id == worker_execution_id]
    assert len(exec_started) == 1
    assert len(exec_completed) == 1

    verification_events = [e for e in events if e.type in (RunEventType.VERIFICATION_STARTED, RunEventType.VERIFICATION_COMPLETED)]
    assert verification_events
    assert all(e.correlation_id == verification_events[0].correlation_id for e in verification_events)

    review_events = [e for e in events if e.type in (RunEventType.REVIEW_STARTED, RunEventType.REVIEW_COMPLETED)]
    assert review_events
    assert all(e.execution_id is None for e in review_events)

    assert not any(e.type == RunEventType.RUN_WAITING_USER for e in events)

    terminal_types = [e.type for e in events if e.type in (RunEventType.FIX_LOOP_COMPLETED, RunEventType.RUN_COMPLETED)]
    assert RunEventType.FIX_LOOP_COMPLETED in terminal_types
