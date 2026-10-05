import subprocess
import sys

from agent_runtime.cancellation import CancellationToken
from agent_execution_runtime import AgentExecutionPorts, AgentExecutionRequest, AgentExecutionStatus, execute_task
from change_runtime import GitWorktreeChangeProvider
from executor_runtime import NativeVerificationAttemptAdapter
from fix_runtime.ports import WorkerAttemptResult
from process_runtime.models import ProcessRequest
from run_runtime.events import RunEventType
from run_runtime.service import RunRuntime
from run_runtime.store import RunStore
from verification_runtime.models import VerificationCheck, VerificationPlan
from workspace.worktree import GitWorktreeWorkspace


class WritingWorker:
    def __init__(self, runtime, run_id, *, write=True):
        self.runtime, self.run_id, self.write = runtime, run_id, write

    def run(self, workspace, request, *, execution_id, cancel_token=None):
        if self.write:
            workspace.write_text("answer.txt", "done\n")
        self.runtime.record(run_id=self.run_id, type=RunEventType.EXECUTION_COMPLETED,
                            execution_id=execution_id, payload={"final_text": "Implemented", "model_turns": 1,
                                                                "tool_calls": 1})
        return WorkerAttemptResult(execution_id)


def _repo(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=source, check=True)
    (source / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=source, check=True)
    return source


def _runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="write answer")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def test_changed_task_waits_for_user_with_verification_pass_or_fail(tmp_path):
    source = _repo(tmp_path)
    for expected in ("pass", "fail"):
        runtime, run = _runtime(tmp_path / expected)
        workspace = GitWorktreeWorkspace.create(source_root=source, run_id=f"agent-{expected}",
                                                base_dir=tmp_path / "worktrees")
        plan = VerificationPlan("plan1", (VerificationCheck(
            "check", "check answer", ProcessRequest(argv=(sys.executable, "-c",
                "from pathlib import Path; assert Path('answer.txt').read_text() == 'done\\n'" if expected == "pass"
                else "raise SystemExit(1)"), timeout_ms=5000)),))
        ports = AgentExecutionPorts(
            WritingWorker(runtime, run.run_id), NativeVerificationAttemptAdapter(runtime, run.run_id),
            GitWorktreeChangeProvider(),
        )
        result = execute_task(runtime, run.run_id,
                              AgentExecutionRequest("write answer", "provider", workspace,
                                                    verification_plan=plan), ports=ports)
        assert result.status is AgentExecutionStatus.NEEDS_USER
        assert result.verification_outcome == expected
        assert result.changed_paths == ("answer.txt",)
        assert runtime.get_run(run.run_id).status.value == "waiting_user"
        workspace.dispose()


def test_no_changes_and_no_verification_plan_are_honest(tmp_path):
    source = _repo(tmp_path)
    runtime, run = _runtime(tmp_path)
    workspace = GitWorktreeWorkspace.create(source_root=source, run_id="agent-empty", base_dir=tmp_path / "w")
    ports = AgentExecutionPorts(WritingWorker(runtime, run.run_id, write=False),
                                NativeVerificationAttemptAdapter(runtime, run.run_id), GitWorktreeChangeProvider())
    result = execute_task(runtime, run.run_id, AgentExecutionRequest("nothing", "p", workspace), ports=ports)
    assert result.status is AgentExecutionStatus.NO_CHANGES
    workspace.dispose()


def test_no_plan_is_not_pass_and_cancellation_is_explicit(tmp_path):
    source = _repo(tmp_path)
    runtime, run = _runtime(tmp_path)
    workspace = GitWorktreeWorkspace.create(source_root=source, run_id="agent-no-plan", base_dir=tmp_path / "w")
    ports = AgentExecutionPorts(WritingWorker(runtime, run.run_id),
                                NativeVerificationAttemptAdapter(runtime, run.run_id), GitWorktreeChangeProvider())
    result = execute_task(runtime, run.run_id, AgentExecutionRequest("write", "p", workspace), ports=ports)
    assert result.status is AgentExecutionStatus.NEEDS_USER
    assert result.verification_outcome == "not_run"
    assert result.verification_report is None
    workspace.dispose()

    runtime, run = _runtime(tmp_path / "cancelled")
    token = CancellationToken()
    token.cancel()
    result = execute_task(runtime, run.run_id, AgentExecutionRequest("cancel", "p", workspace),
                          ports=ports, cancel_token=token)
    assert result.status is AgentExecutionStatus.CANCELLED
    assert runtime.get_run(run.run_id).status.value == "cancelled"


def test_worker_failure_is_recorded_as_run_failure(tmp_path):
    class BrokenWorker:
        def run(self, *_args, **_kwargs):
            raise RuntimeError("scripted worker failure")

    source = _repo(tmp_path)
    runtime, run = _runtime(tmp_path)
    workspace = GitWorktreeWorkspace.create(source_root=source, run_id="agent-failure", base_dir=tmp_path / "w")
    ports = AgentExecutionPorts(BrokenWorker(), NativeVerificationAttemptAdapter(runtime, run.run_id),
                                GitWorktreeChangeProvider())
    result = execute_task(runtime, run.run_id, AgentExecutionRequest("fail", "p", workspace), ports=ports)
    assert result.status is AgentExecutionStatus.FAILED
    assert runtime.get_run(run.run_id).status.value == "failed"
    failed = next(e for e in runtime.events(run.run_id).events if e.type == RunEventType.RUN_FAILED)
    assert "scripted worker failure" not in str(failed.payload)
    workspace.dispose()


def test_verification_mutating_unchanged_input_invalidates_pass(tmp_path):
    source = _repo(tmp_path)
    runtime, run = _runtime(tmp_path)
    workspace = GitWorktreeWorkspace.create(source_root=source, run_id="agent-mutating-check", base_dir=tmp_path / "w")
    plan = VerificationPlan("plan1", (VerificationCheck(
        "check", "mutate input", ProcessRequest(argv=(sys.executable, "-c",
            "from pathlib import Path; Path('base.txt').write_text('changed\\n')"), timeout_ms=5000)),))
    ports = AgentExecutionPorts(WritingWorker(runtime, run.run_id),
                                NativeVerificationAttemptAdapter(runtime, run.run_id), GitWorktreeChangeProvider())
    result = execute_task(runtime, run.run_id,
                          AgentExecutionRequest("write answer", "provider", workspace, verification_plan=plan),
                          ports=ports)
    assert result.verification_report.status.value == "pass"
    assert result.verification_outcome == "invalidated"
    proposal = next(e for e in runtime.events(run.run_id).events if e.type == RunEventType.PROPOSAL_READY)
    assert proposal.payload["verification"]["changed_content"] is True
    assert "base.txt" in proposal.payload["changed_paths"]
    workspace.dispose()
