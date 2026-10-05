import errno
import os
import sys
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
import agent_execution_runtime.execution as agent_execution

from agent_execution_runtime import AgentExecutionPorts, AgentExecutionRequest, execute_task
from agent_execution_runtime.execution import _workspace_fingerprint, _workspace_inventory
from collab_runtime.candidates import _fingerprint_records
from change_runtime import GitWorktreeChangeProvider
from executor_runtime import NativeVerificationAttemptAdapter
from fix_runtime.ports import WorkerAttemptResult
from process_runtime import ProcessRunner
from process_runtime.models import ProcessRequest
from run_runtime.events import RunEventType
from run_runtime.service import RunRuntime
from run_runtime.store import RunStore
from verification_runtime.models import VerificationCheck, VerificationPlan, new_verification_id
from verification_runtime.runner import VerificationRunner
from workspace.worktree import GitWorktreeWorkspace
from agent_execution_runtime import AgentExecutionResult, AgentExecutionStatus
import webhost.api.run as run_api


class WritingWorker:
    def __init__(self, runtime, run_id, action):
        self.runtime, self.run_id, self.action = runtime, run_id, action

    def run(self, workspace, request, *, execution_id, cancel_token=None):
        self.action(workspace)
        self.runtime.record(run_id=self.run_id, type=RunEventType.EXECUTION_COMPLETED,
                            execution_id=execution_id, payload={"final_text": "done"})
        return WorkerAttemptResult(execution_id)


def _runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="verify evidence")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _run_execution(tmp_path, worker_action, verifier, check_argv):
    source = _repo(tmp_path / "repo")
    (source / "base.txt").write_text("baseline")
    subprocess.run(["git", "add", "base.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=source, check=True)
    runtime, run = _runtime(tmp_path / "run")
    workspace = GitWorktreeWorkspace.create(source_root=source, run_id="agent", base_dir=tmp_path / "worktrees")
    plan = VerificationPlan("plan", (VerificationCheck(
        "check", "run check", ProcessRequest(argv=tuple(check_argv), timeout_ms=30_000)),))
    result = execute_task(runtime, run.run_id, AgentExecutionRequest("task", "provider", workspace,
                                                                      verification_plan=plan),
                          ports=AgentExecutionPorts(WritingWorker(runtime, run.run_id, worker_action),
                                                    verifier(runtime, run.run_id), GitWorktreeChangeProvider()))
    workspace.dispose()
    return result


class RootWorkspace:
    def __init__(self, root):
        self.root = root


def _repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Evidence Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "evidence@example.test"], cwd=path, check=True)
    return path


def _commit(path):
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=path, check=True)


def test_workspace_fingerprint_detects_mode_and_original_cache_input(tmp_path):
    root = _repo(tmp_path / "repo")
    cache_input = root / ".pytest_cache" / "input.txt"
    cache_input.parent.mkdir()
    cache_input.write_text("original")
    executable = root / "run.sh"
    executable.write_text("#!/bin/sh\ntrue\n")
    executable.chmod(0o755)
    subprocess.run(["git", "add", "-f", ".pytest_cache/input.txt"], cwd=root, check=True)
    _commit(root)
    workspace = RootWorkspace(root)

    originals, complete = _workspace_inventory(workspace)
    before, fingerprint_complete = _workspace_fingerprint(workspace, originals)
    supports_safe_inventory = (
        os.scandir in os.supports_fd
        and os.open in os.supports_dir_fd
        and hasattr(os, "O_NOFOLLOW")
    )
    assert complete is supports_safe_inventory
    assert fingerprint_complete is supports_safe_inventory
    if not supports_safe_inventory:
        # The supported security boundary reports incomplete evidence rather
        # than pretending it inspected files it cannot open nofollow.
        return
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "generated.pyc").write_bytes(b"generated cache")
    assert _workspace_fingerprint(workspace, originals) == (before, True)

    cache_input.write_text("modified")
    assert _workspace_fingerprint(workspace, originals)[0] != before
    cache_input.write_text("original")
    originals, _ = _workspace_inventory(workspace)
    before, _ = _workspace_fingerprint(workspace, originals)
    executable.chmod(0o644)
    assert _workspace_fingerprint(workspace, originals)[0] != before


def test_workspace_fingerprint_nonregular_links_and_oversize_are_incomplete(tmp_path):
    root = _repo(tmp_path / "repo")
    tracked = root / "input.txt"
    tracked.write_text("input")
    _commit(root)

    external = tmp_path / "outside.txt"
    external.write_text("secret external data")
    try:
        (root / "linked-directory").symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM):
            raise
        pytest.skip("creating symlinks requires platform permission")
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "input.fifo")
    _, complete = _fingerprint_records(root, ("input.txt",))
    assert complete is False

    (root / "linked-directory").unlink()
    if hasattr(os, "mkfifo"):
        os.unlink(root / "input.fifo")
    (root / "oversize.bin").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    _, complete = _fingerprint_records(root, ("input.txt",))
    assert complete is False


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
def test_fifo_fingerprint_does_not_block(tmp_path):
    root = _repo(tmp_path / "repo")
    os.mkfifo(root / "blocked.fifo")
    _, complete = _fingerprint_records(root)
    assert complete is False


def test_inventory_budget_overflow_is_incomplete(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo")
    (root / "one.txt").write_text("one")
    (root / "two.txt").write_text("two")
    monkeypatch.setattr(agent_execution, "_FP_ENTRY_BUDGET", 1)
    _paths, complete = agent_execution._workspace_inventory(RootWorkspace(root))
    assert complete is False


def test_inventory_scandir_error_is_incomplete(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo")
    (root / "input.txt").write_text("input")
    def fail_scandir(_fd):
        raise PermissionError("injected inventory failure")
    monkeypatch.setattr(agent_execution.os, "scandir", fail_scandir)
    _paths, complete = agent_execution._workspace_inventory(RootWorkspace(root))
    assert complete is False


def test_agent_pins_worker_created_cache_named_input_before_verification(tmp_path):
    def create_input(workspace):
        workspace.write_text("answer.txt", "done")
        workspace.write_text(".pytest_cache/custom_input.txt", "before")

    result = _run_execution(
        tmp_path, create_input,
        lambda runtime, run_id: NativeVerificationAttemptAdapter(runtime, run_id),
        (sys.executable, "-c", "import subprocess; from pathlib import Path; p='.pytest_cache/custom_input.txt'; "
         "Path(p).write_text('after'); subprocess.run(['git','add',p],check=True); "
         "subprocess.run(['git','rm','--cached','-q','-f','--',p],check=True)"),
    )
    assert result.verification_outcome == "invalidated"


def test_real_pytest_generated_cache_does_not_invalidate_pass(tmp_path):
    def create_test(workspace):
        workspace.write_text("answer.txt", "done")
        workspace.write_text("test_smoke.py", "def test_smoke():\n    assert True\n")

    result = _run_execution(
        tmp_path, create_test,
        lambda runtime, run_id: NativeVerificationAttemptAdapter(runtime, run_id),
        (sys.executable, "-m", "pytest", "-q", "test_smoke.py"),
    )
    assert result.verification_report.status.value == "pass"
    supports_safe_inventory = (
        os.scandir in os.supports_fd
        and os.open in os.supports_dir_fd
        and hasattr(os, "O_NOFOLLOW")
    )
    assert result.verification_outcome == ("pass" if supports_safe_inventory else "invalidated")


@pytest.mark.parametrize("mutation", ("verification_id", "plan_id", "check_id", "unrecorded"))
def test_mismatched_or_unrecorded_report_never_passes(tmp_path, mutation):
    def write_answer(workspace):
        workspace.write_text("answer.txt", "done")

    class ForgingVerifier:
        def __init__(self, runtime, run_id):
            self.native = NativeVerificationAttemptAdapter(runtime, run_id)

        def run(self, workspace, plan, *, verification_id, cancel_token=None):
            if mutation == "unrecorded":
                return VerificationRunner(ProcessRunner()).run(
                    workspace, plan, verification_id=verification_id, cancel_token=cancel_token
                )
            report = self.native.run(workspace, plan, verification_id=verification_id,
                                     cancel_token=cancel_token)
            if mutation == "verification_id":
                return replace(report, verification_id=new_verification_id())
            if mutation == "plan_id":
                return replace(report, plan_id="wrong-plan")
            return replace(report, results=(replace(report.results[0], check_id="wrong-check"),))

    result = _run_execution(
        tmp_path, write_answer, ForgingVerifier,
        (sys.executable, "-c", "pass"),
    )
    assert result.verification_outcome == "error"


def test_bridge_final_capture_hash_mismatch_downgrades_displayed_pass(monkeypatch):
    class Signal:
        def __init__(self):
            self.callback = None
        def connect(self, callback):
            self.callback = callback
        def emit(self, *args):
            self.callback(*args)

    class Worker:
        def __init__(self):
            self.stage, self.failed, self.finished_ok = Signal(), Signal(), Signal()
            self.ports = SimpleNamespace(change_provider=SimpleNamespace(
                capture=lambda _workspace: SimpleNamespace(diff_sha256="final-hash", changed_paths=("x",))))
        def start(self):
            self.finished_ok.emit(AgentExecutionResult(
                "run", AgentExecutionStatus.NEEDS_USER, "proposal_pending", ("x",), "pass", "exec", None))

    class Bridge:
        def __init__(self):
            self.events = []
        def emit_event(self, _name, payload):
            self.events.append(payload)

    monkeypatch.setattr(run_api, "_agent_event_for_execution", lambda *_: SimpleNamespace(payload={
        "execution_id": "exec", "diff_sha256": "captured-hash", "agent_message": "",
        "verification": {"outcome": "pass"},
    }))
    monkeypatch.setattr(run_api, "_build_pipeline_proposals", lambda *_: ([{"path": "x"}], []))
    monkeypatch.setattr(run_api, "_stop_activity_streamer", lambda: None)
    monkeypatch.setattr(run_api, "ActivityStreamer", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setitem(run_api._active, "proposals", [])
    emitted, bridge = [], Bridge()
    run_api._wire_agent_worker(
        Worker(), runtime=object(), run_id="run", coordinator=SimpleNamespace(), workspace=object(),
        proj=object(), emit_ui=emitted.append, bridge=bridge, ended={"flag": False},
    )
    evidence = next(event for event in emitted if event["type"] == "evidence")
    assert evidence["verification"]["outcome"] == "invalidated"
