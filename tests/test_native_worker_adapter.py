"""NativeWorkerAttemptAdapter — binds fix_runtime.ports.WorkerAttemptRunner
to the real native AgentSession harness."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import ModelStopReason, ModelToolCall, ModelTurn, ModelUsage  # noqa: E402
from agent_runtime.errors import AgentBackendError  # noqa: E402
from executor_runtime.errors import ExecutorAdapterExecutionError, ExecutorAdapterInputError  # noqa: E402
from executor_runtime.native_worker import NativeWorkerAttemptAdapter  # noqa: E402
from fix_runtime.models import FixTrigger, FixTriggerKind, FixWorkerRequest  # noqa: E402
from fix_runtime.prompt import render_fix_worker_input  # noqa: E402
from process_runtime.models import ProcessResult  # noqa: E402
from run_runtime import RunEventType, RunRuntime, RunStore  # noqa: E402
from verification_runtime.models import VerificationCheckResult, VerificationReport, VerificationStatus  # noqa: E402
from workspace.local import LocalWorkspace  # noqa: E402


# ---------------- shared fakes/helpers ----------------


class ScriptedSession:
    def __init__(self, turns):
        self.turns = list(turns)
        self.inputs = []

    def respond(self, input_items):
        self.inputs.append(input_items)
        value = self.turns.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ScriptedBackend:
    def __init__(self, turns):
        self.session = ScriptedSession(turns)
        self.opened_with = None
        self.open_session_calls: list[dict] = []

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        self.opened_with = {
            "instructions": instructions,
            "tools": tools,
            "allow_parallel_tool_calls": allow_parallel_tool_calls,
        }
        self.open_session_calls.append(self.opened_with)
        return self.session


class NeverOpenedBackend:
    """Fails the test if open_session is ever called (pre-model-open validation proof)."""

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        raise AssertionError("backend.open_session must not be called for a rejected request")


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _verification_fail_report(verification_id="ver-1"):
    result = VerificationCheckResult(
        "c1", "Check", VerificationStatus.FAIL,
        ProcessResult(
            argv=("true",), cwd=".", exit_code=1, timed_out=False, duration_ms=1,
            stdout="", stderr="", stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
        ),
    )
    return VerificationReport(verification_id=verification_id, plan_id="plan-1", results=(result,), duration_ms=1)


def _valid_worker_request(attempt_index=1, max_fix_attempts=2):
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification_fail_report())
    rendered = render_fix_worker_input(
        task="fix the bug", plan=None, trigger=trigger, attempt_index=attempt_index, max_fix_attempts=max_fix_attempts,
    )
    return FixWorkerRequest(
        task="fix the bug", trigger=trigger, attempt_index=attempt_index, rendered_input=rendered,
    )


def _completed_turn(text="Made the change."):
    return ModelTurn(text, (), ModelStopReason.COMPLETED, ModelUsage())


# ---------------- Task 1: constructor + pre-side-effect validation ----------------


def test_run_id_property_exposes_constructor_value(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([_completed_turn()])
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend)
    assert adapter.run_id == run.run_id


def test_non_fix_worker_request_rejected_before_model_open(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, NeverOpenedBackend())
    workspace = LocalWorkspace(tmp_path)
    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(workspace, "not a request", execution_id="exec_fix_1")


def test_invalid_execution_id_rejected_before_model_open(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, NeverOpenedBackend())
    workspace = LocalWorkspace(tmp_path)
    request = _valid_worker_request()
    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(workspace, request, execution_id="not an id with spaces")


def test_empty_execution_id_rejected_before_model_open(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, NeverOpenedBackend())
    workspace = LocalWorkspace(tmp_path)
    request = _valid_worker_request()
    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(workspace, request, execution_id="")


def test_local_workspace_rejected_before_model_open_and_no_canonical_events(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, NeverOpenedBackend())
    workspace = LocalWorkspace(tmp_path)
    request = _valid_worker_request()

    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(workspace, request, execution_id="exec_fix_1")

    # No workspace mutation and no canonical execution.* lifecycle recorded.
    events = runtime.events(run.run_id, limit=200).events
    assert not any(event.type == RunEventType.EXECUTION_STARTED for event in events)


def test_git_worktree_workspace_accepted(tmp_path, git_worktree_workspace):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([_completed_turn()])
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend)
    request = _valid_worker_request()

    result = adapter.run(git_worktree_workspace, request, execution_id="exec_fix_1")

    assert result.execution_id == "exec_fix_1"


# ---------------- Task 2: registry/policy + exact rendered_input ----------------


_EXPECTED_TOOL_NAMES = {
    "read_file", "list_files", "search_text", "write_file", "delete_path", "repo_map", "search_code",
}


def test_backend_sees_exactly_the_seven_expected_tool_definitions(tmp_path, git_worktree_workspace):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([_completed_turn()])
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend)
    request = _valid_worker_request()

    adapter.run(git_worktree_workspace, request, execution_id="exec_fix_1")

    names = {tool.name for tool in backend.opened_with["tools"]}
    assert names == _EXPECTED_TOOL_NAMES


def test_run_process_is_never_registered(tmp_path, git_worktree_workspace):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([_completed_turn()])
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend)
    request = _valid_worker_request()

    adapter.run(git_worktree_workspace, request, execution_id="exec_fix_1")

    names = {tool.name for tool in backend.opened_with["tools"]}
    assert "run_process" not in names
    assert not any("shell" in name or "process" in name for name in names)


def test_worker_policy_allows_read_list_search_edit_delete_without_ask():
    from tool_runtime.models import PermissionEffect, PermissionRequest

    from executor_runtime.native_worker import _worker_policy

    policy = _worker_policy()
    for action in ("read", "list", "search", "edit", "delete"):
        evaluation = policy.evaluate_one(PermissionRequest(action, "some/path"))
        assert evaluation.effect is PermissionEffect.ALLOW

    unknown = policy.evaluate_one(PermissionRequest("run_process", "some/cmd"))
    assert unknown.effect is PermissionEffect.DENY
    assert policy.default_effect is PermissionEffect.DENY


def test_exact_rendered_input_reaches_the_first_user_input_verbatim(tmp_path, git_worktree_workspace):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([_completed_turn()])
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend)
    request = _valid_worker_request()

    adapter.run(git_worktree_workspace, request, execution_id="exec_fix_1")

    first_inputs = backend.session.inputs[0]
    assert len(first_inputs) == 1
    assert first_inputs[0].text == request.rendered_input


def test_system_instructions_are_separate_from_rendered_input(tmp_path, git_worktree_workspace):
    from executor_runtime.native_worker import NATIVE_FIX_WORKER_SYSTEM_INSTRUCTIONS

    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([_completed_turn()])
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend)
    request = _valid_worker_request()

    adapter.run(git_worktree_workspace, request, execution_id="exec_fix_1")

    assert backend.opened_with["instructions"] == NATIVE_FIX_WORKER_SYSTEM_INSTRUCTIONS
    first_inputs = backend.session.inputs[0]
    assert NATIVE_FIX_WORKER_SYSTEM_INSTRUCTIONS not in first_inputs[0].text
    assert first_inputs[0].text == request.rendered_input


def test_supplied_execution_id_used_exactly_and_returned(tmp_path, git_worktree_workspace):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([_completed_turn()])
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend)
    request = _valid_worker_request()

    result = adapter.run(git_worktree_workspace, request, execution_id="exec_fix_specific")

    assert result.execution_id == "exec_fix_specific"


# ---------------- Task 3: canonical lifecycle + failure semantics ----------------


def test_canonical_execution_started_and_completed_exist_for_exact_id_no_duplicates(tmp_path, git_worktree_workspace):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([_completed_turn()])
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend)
    request = _valid_worker_request()

    adapter.run(git_worktree_workspace, request, execution_id="exec_fix_dup")

    events = runtime.events(run.run_id, limit=200).events
    started = [e for e in events if e.type == RunEventType.EXECUTION_STARTED and e.execution_id == "exec_fix_dup"]
    completed = [e for e in events if e.type == RunEventType.EXECUTION_COMPLETED and e.execution_id == "exec_fix_dup"]
    assert len(started) == 1
    assert len(completed) == 1


def test_fresh_agent_session_opens_a_new_model_session_per_call(tmp_path, git_worktree_workspace):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([_completed_turn(), _completed_turn()])
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend)

    adapter.run(git_worktree_workspace, _valid_worker_request(), execution_id="exec_fix_a")
    adapter.run(git_worktree_workspace, _valid_worker_request(), execution_id="exec_fix_b")

    assert len(backend.open_session_calls) == 2


def test_agent_runtime_error_wrapped_with_cause_and_no_result_returned(tmp_path, git_worktree_workspace):
    from agent_runtime.errors import AgentBackendError

    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([RuntimeError("provider unavailable")])
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend)
    request = _valid_worker_request()

    with pytest.raises(ExecutorAdapterExecutionError) as excinfo:
        adapter.run(git_worktree_workspace, request, execution_id="exec_fix_fail")

    assert isinstance(excinfo.value.__cause__, AgentBackendError)


def test_approval_pause_raises_and_is_not_resumed_no_events_recorded(tmp_path, git_worktree_workspace, monkeypatch):
    from agent_runtime.models import ApprovalPause

    class _ApprovalPauseSession:
        def __init__(self, **kwargs):
            pass

        def start(self, task):
            return ApprovalPause("call1", "some_tool", "fingerprint1", (), "session1")

    monkeypatch.setattr("executor_runtime.native_worker.AgentSession", _ApprovalPauseSession)

    runtime, run = setup_runtime(tmp_path)
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, ScriptedBackend([]))
    request = _valid_worker_request()
    before = runtime.events(run.run_id, limit=200).events

    with pytest.raises(ExecutorAdapterExecutionError):
        adapter.run(git_worktree_workspace, request, execution_id="exec_fix_pause")

    after = runtime.events(run.run_id, limit=200).events
    assert after == before
    assert not any(e.type == RunEventType.RUN_WAITING_USER for e in after)


def test_worker_never_touches_process_runner(tmp_path, git_worktree_workspace):
    """Structural proof: executor_runtime.native_worker never imports process_runtime."""
    import executor_runtime.native_worker as module

    assert "process_runtime" not in module.__dict__
    with open(module.__file__, encoding="utf-8") as handle:
        assert "process_runtime" not in handle.read()


def test_real_git_worktree_mutation_stays_isolated_from_source_repo(tmp_path):
    """The Worker's ALLOW mutation policy must be constrained by the actual
    isolated workspace: writing a file must only ever touch the shadow
    worktree, never the user's real source repository."""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not found")

    from agent_runtime.models import ModelToolCall
    from workspace.worktree import GitWorktreeWorkspace

    source = tmp_path / "repo"
    source.mkdir()

    def _git(args):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)

    _git(["init", "-q"])
    _git(["config", "user.name", "T"])
    _git(["config", "user.email", "t@example.com"])
    (source / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"])
    _git(["commit", "-q", "-m", "init"])

    ws = GitWorktreeWorkspace.create(source_root=source, run_id="mutation-test", base_dir=tmp_path / "workspaces")
    try:
        tool_call_turn = ModelTurn(
            "", (ModelToolCall("c1", "write_file", {"path": "a.txt", "content": "changed\n"}),),
            ModelStopReason.TOOL_USE, ModelUsage(),
        )
        final_turn = _completed_turn("Updated a.txt.")
        backend = ScriptedBackend([tool_call_turn, final_turn])

        runtime, run = setup_runtime(tmp_path)
        adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend)
        request = _valid_worker_request()

        result = adapter.run(ws, request, execution_id="exec_fix_mutate")

        assert result.execution_id == "exec_fix_mutate"
        assert (source / "a.txt").read_text(encoding="utf-8") == "hello\n"
        assert (ws.root / "a.txt").read_text(encoding="utf-8") == "changed\n"

        events = runtime.events(run.run_id, limit=200).events
        completed = [e for e in events if e.type == RunEventType.EXECUTION_COMPLETED and e.execution_id == "exec_fix_mutate"]
        assert len(completed) == 1
        assert not any(e.type == RunEventType.RUN_WAITING_USER for e in events)
    finally:
        ws.dispose()


# ---------------- shared real-git fixture (used from Task 1 onward) ----------------


@pytest.fixture
def git_worktree_workspace(tmp_path):
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not found")

    from workspace.worktree import GitWorktreeWorkspace

    source = tmp_path / "repo"
    source.mkdir()

    def _git(args):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)

    _git(["init", "-q"])
    _git(["config", "user.name", "T"])
    _git(["config", "user.email", "t@example.com"])
    (source / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"])
    _git(["commit", "-q", "-m", "init"])

    ws = GitWorktreeWorkspace.create(source_root=source, run_id="worker-adapter-test", base_dir=tmp_path / "workspaces")
    yield ws
    ws.dispose()
