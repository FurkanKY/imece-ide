"""NativeVerificationAttemptAdapter — binds fix_runtime.ports.VerificationAttemptRunner
to the existing deterministic VerificationRunner."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from executor_runtime.errors import ExecutorAdapterExecutionError, ExecutorAdapterInputError  # noqa: E402
from executor_runtime.native_verification import NativeVerificationAttemptAdapter  # noqa: E402
from process_runtime.models import ProcessRequest, ProcessResult  # noqa: E402
from run_runtime import RunEventType, RunRuntime, RunStore  # noqa: E402
from verification_runtime.models import VerificationCheck, VerificationPlan  # noqa: E402
from workspace.local import LocalWorkspace  # noqa: E402


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _process_result(exit_code=0, timed_out=False):
    return ProcessResult(
        argv=("true",), cwd=".", exit_code=exit_code, timed_out=timed_out, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )


def _plan(plan_id="plan-1", check_id="c1"):
    return VerificationPlan(
        plan_id=plan_id,
        checks=(VerificationCheck(check_id, "Check", ProcessRequest(argv=("true",))),),
    )


class FakeProcessRunner:
    """Records exactly the (workspace, ProcessRequest) pairs it receives."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple[object, ProcessRequest]] = []

    def run(self, workspace, request):
        self.calls.append((workspace, request))
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


# ---------------- constructor / validation ----------------


def test_run_id_property_exposes_constructor_value(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    adapter = NativeVerificationAttemptAdapter(runtime, run.run_id, process_runner=FakeProcessRunner([]))
    assert adapter.run_id == run.run_id


def test_non_verification_plan_rejected_before_process_execution(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    process_runner = FakeProcessRunner([])
    adapter = NativeVerificationAttemptAdapter(runtime, run.run_id, process_runner=process_runner)
    workspace = LocalWorkspace(tmp_path)

    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(workspace, "not a plan", verification_id="ver-1")

    assert process_runner.calls == []


# ---------------- exact id propagation ----------------


def test_exact_verification_id_forwarded_to_sink_and_report(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    process_runner = FakeProcessRunner([_process_result(0)])
    adapter = NativeVerificationAttemptAdapter(runtime, run.run_id, process_runner=process_runner)
    workspace = LocalWorkspace(tmp_path)

    report = adapter.run(workspace, _plan(), verification_id="ver-exact")

    assert report.verification_id == "ver-exact"
    events = runtime.events(run.run_id, limit=200).events
    started = [e for e in events if e.type == RunEventType.VERIFICATION_STARTED]
    assert len(started) == 1
    assert started[0].payload["verification_id"] == "ver-exact"


def test_process_runner_receives_exact_workspace_and_request(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    process_runner = FakeProcessRunner([_process_result(0)])
    adapter = NativeVerificationAttemptAdapter(runtime, run.run_id, process_runner=process_runner)
    workspace = LocalWorkspace(tmp_path)
    plan = _plan()

    adapter.run(workspace, plan, verification_id="ver-1")

    assert len(process_runner.calls) == 1
    called_workspace, called_request = process_runner.calls[0]
    assert called_workspace is workspace
    assert called_request is plan.checks[0].request


# ---------------- normal semantic outcomes (never exceptions) ----------------


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "expected_status"),
    [
        (0, False, "pass"),
        (1, False, "fail"),
        (0, True, "timeout"),
    ],
)
def test_status_outcomes_return_normally(tmp_path, exit_code, timed_out, expected_status):
    runtime, run = setup_runtime(tmp_path)
    process_runner = FakeProcessRunner([_process_result(exit_code, timed_out)])
    adapter = NativeVerificationAttemptAdapter(runtime, run.run_id, process_runner=process_runner)
    workspace = LocalWorkspace(tmp_path)

    report = adapter.run(workspace, _plan(), verification_id="ver-status")

    assert report.status.value == expected_status


def test_process_runtime_error_becomes_verification_error_status_not_exception(tmp_path):
    from process_runtime.errors import ProcessSpawnError

    runtime, run = setup_runtime(tmp_path)
    process_runner = FakeProcessRunner([ProcessSpawnError("no such executable")])
    adapter = NativeVerificationAttemptAdapter(runtime, run.run_id, process_runner=process_runner)
    workspace = LocalWorkspace(tmp_path)

    report = adapter.run(workspace, _plan(), verification_id="ver-error")

    assert report.status.value == "error"


# ---------------- fail-closed contract violations ----------------


def test_report_verification_id_mismatch_fails_closed(tmp_path, monkeypatch):
    import verification_runtime.runner as vr_module

    runtime, run = setup_runtime(tmp_path)
    process_runner = FakeProcessRunner([_process_result(0)])
    adapter = NativeVerificationAttemptAdapter(runtime, run.run_id, process_runner=process_runner)
    workspace = LocalWorkspace(tmp_path)

    real_run = vr_module.VerificationRunner.run

    def _mismatched_run(self, workspace, plan, *, verification_id=None):
        report = real_run(self, workspace, plan, verification_id=verification_id)
        object.__setattr__(report, "verification_id", "totally-different-id")
        return report

    monkeypatch.setattr(vr_module.VerificationRunner, "run", _mismatched_run)

    with pytest.raises(ExecutorAdapterExecutionError):
        adapter.run(workspace, _plan(), verification_id="ver-real")


# ---------------- no orchestration / no duplicate canonical bridge ----------------


def test_no_run_completion_gate_import():
    import executor_runtime.native_verification as module

    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "RunCompletionGate" not in source
    assert "change_runtime" not in source


def test_no_retry_process_runner_called_exactly_once_per_check(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    process_runner = FakeProcessRunner([_process_result(1)])  # FAIL, must not retry
    adapter = NativeVerificationAttemptAdapter(runtime, run.run_id, process_runner=process_runner)
    workspace = LocalWorkspace(tmp_path)

    adapter.run(workspace, _plan(), verification_id="ver-noretry")

    assert len(process_runner.calls) == 1


def test_canonical_verification_started_and_completed_exist_no_execution_events(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    process_runner = FakeProcessRunner([_process_result(0)])
    adapter = NativeVerificationAttemptAdapter(runtime, run.run_id, process_runner=process_runner)
    workspace = LocalWorkspace(tmp_path)

    adapter.run(workspace, _plan(), verification_id="ver-lifecycle")

    events = runtime.events(run.run_id, limit=200).events
    types = [e.type for e in events]
    assert RunEventType.VERIFICATION_STARTED in types
    assert RunEventType.VERIFICATION_COMPLETED in types
    assert RunEventType.EXECUTION_STARTED not in types
    assert RunEventType.EXECUTION_COMPLETED not in types


def test_default_process_runner_is_a_real_process_runner(tmp_path):
    from process_runtime import ProcessRunner

    runtime, run = setup_runtime(tmp_path)
    adapter = NativeVerificationAttemptAdapter(runtime, run.run_id)
    assert isinstance(adapter._process_runner, ProcessRunner)
