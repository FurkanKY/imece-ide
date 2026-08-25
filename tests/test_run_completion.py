import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentSession, ModelStopReason, ModelTurn, ModelUsage  # noqa: E402
from agent_runtime.errors import AgentBackendError  # noqa: E402
from process_runtime import ProcessRequest, ProcessResult  # noqa: E402
from run_runtime import (  # noqa: E402
    CanonicalAgentEventSink,
    CanonicalReviewEventSink,
    CanonicalVerificationEventSink,
    RunCompletionGate,
    RunEventSpec,
    RunEventType,
    RunPhase,
    RunRuntime,
    RunStatus,
    RunStore,
    build_receipt,
    recover_running_runs,
)
from review_runtime.models import ReviewFinding, ReviewReport, ReviewSeverity, ReviewVerdict  # noqa: E402
from run_runtime.errors import EventSequenceError, RunCompletionError  # noqa: E402
from run_runtime.readmodels import RunReadSnapshot  # noqa: E402
from verification_runtime import (  # noqa: E402
    VerificationCheck,
    VerificationPlan,
    VerificationRunner,
)
from tool_runtime import PolicyEvaluator, ToolExecutionContext, ToolRegistry  # noqa: E402
from workspace.local import LocalWorkspace  # noqa: E402


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
    def __init__(self, exit_code=0):
        self.exit_code = exit_code
        self.calls = []

    def run(self, workspace, request):
        self.calls.append(request)
        return ProcessResult(
            argv=request.argv,
            cwd=request.cwd,
            exit_code=self.exit_code,
            timed_out=False,
            duration_ms=1,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_bytes=0,
            stderr_bytes=0,
        )


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, task, run


def append_execution(runtime, run_id, execution_id, event_type, *, message="worker failure"):
    if event_type == RunEventType.EXECUTION_FAILED:
        payload = {"error_type": "AgentBackendError", "message": message}
    elif event_type == RunEventType.EXECUTION_COMPLETED:
        payload = {"final_text": "done"}
    else:
        payload = {}
    runtime.record(
        run_id=run_id,
        type=event_type,
        payload=payload,
        execution_id=execution_id,
        correlation_id=execution_id,
        source="native_agent",
    )


def append_verification(runtime, run_id, verification_id, status):
    runtime.record_many(run_id=run_id, specs=(
        RunEventSpec(
            type=RunEventType.VERIFICATION_STARTED,
            payload={"verification_id": verification_id, "plan_id": "plan", "check_count": 1},
            correlation_id=verification_id,
            source="verification",
        ),
        RunEventSpec(
            type=RunEventType.VERIFICATION_COMPLETED,
            payload={
                "verification_id": verification_id,
                "plan_id": "plan",
                "status": status,
                "duration_ms": 1,
                "counts": {"pass": status == "pass", "fail": status == "fail", "timeout": status == "timeout", "error": status == "error", "total": 1},
            },
            correlation_id=verification_id,
            source="verification",
        ),
    ))


def receipt(runtime, task, run):
    current = runtime.get_run(run.run_id)
    return build_receipt(RunReadSnapshot(
        task=task,
        run=current,
        events=runtime.events(run.run_id, limit=200).events,
    ))


def test_full_native_execution_verification_and_explicit_completion(tmp_path):
    runtime, task, run = setup_runtime(tmp_path)
    agent = AgentSession(
        backend=ScriptedBackend([ModelTurn("done", (), ModelStopReason.COMPLETED, ModelUsage())]),
        registry=__import__("tool_runtime").ToolRegistry(),
        policy=__import__("tool_runtime").PolicyEvaluator(),
        context=__import__("tool_runtime").ToolExecutionContext(
            LocalWorkspace(tmp_path), run_id=run.run_id, execution_id="exec-1"
        ),
        event_sink=CanonicalAgentEventSink(runtime, run.run_id, execution_id="exec-1"),
        execution_id="exec-1",
    )
    agent.start("task")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    assert RunEventType.RUN_COMPLETED not in [event.type for event in runtime.events(run.run_id, limit=200).events]

    plan = VerificationPlan(("plan"), (
        VerificationCheck("check", "Check", ProcessRequest((sys.executable, "-c", "pass"))),
    ))
    verification_id = "ver-1"
    VerificationRunner(
        FakeProcessRunner(),
        CanonicalVerificationEventSink(runtime, run.run_id, verification_id=verification_id),
    ).run(LocalWorkspace(tmp_path), plan, verification_id=verification_id)
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    assert receipt(runtime, task, run)["status"] == "running"

    RunCompletionGate(runtime).complete_verified(run.run_id, verification_id=verification_id)
    completed = runtime.get_run(run.run_id)
    assert completed.status is RunStatus.SUCCEEDED
    assert completed.phase is RunPhase.DONE
    events = runtime.events(run.run_id, limit=200).events
    assert events[-1].type == RunEventType.RUN_COMPLETED
    assert receipt(runtime, task, run)["status"] == "completed"
    assert receipt(runtime, task, run)["verification"]["status"] == "pass"


def test_execution_failure_is_not_run_failure_until_gate(tmp_path):
    runtime, _, run = setup_runtime(tmp_path)
    append_execution(runtime, run.run_id, "exec-fail", RunEventType.EXECUTION_FAILED, message="transport failed")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    RunCompletionGate(runtime).fail_execution(run.run_id, execution_id="exec-fail")
    failed = runtime.get_run(run.run_id)
    assert failed.status is RunStatus.FAILED
    assert failed.error_code == "execution_failed"
    assert failed.error_message == "transport failed"


def test_agent_execution_failure_requires_explicit_gate_decision(tmp_path):
    runtime, _, run = setup_runtime(tmp_path)
    execution_id = "exec-agent-fail"
    session = AgentSession(
        backend=ScriptedBackend([RuntimeError("provider down")]),
        registry=ToolRegistry(),
        policy=PolicyEvaluator(),
        context=ToolExecutionContext(
            LocalWorkspace(tmp_path), run_id=run.run_id, execution_id=execution_id
        ),
        event_sink=CanonicalAgentEventSink(runtime, run.run_id, execution_id=execution_id),
        execution_id=execution_id,
    )
    with pytest.raises(AgentBackendError):
        session.start("fail")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    RunCompletionGate(runtime).fail_execution(run.run_id, execution_id=execution_id)
    assert runtime.get_run(run.run_id).status is RunStatus.FAILED


def test_verification_failure_is_repairable_and_multiple_executions_remain_running(tmp_path):
    runtime, _, run = setup_runtime(tmp_path)
    first_id = "exec-1"
    first = AgentSession(
        backend=ScriptedBackend([ModelTurn("first", (), ModelStopReason.COMPLETED, ModelUsage())]),
        registry=ToolRegistry(),
        policy=PolicyEvaluator(),
        context=ToolExecutionContext(LocalWorkspace(tmp_path), run_id=run.run_id, execution_id=first_id),
        event_sink=CanonicalAgentEventSink(runtime, run.run_id, execution_id=first_id),
        execution_id=first_id,
    )
    first.start("first")
    append_verification(runtime, run.run_id, "ver-fail", "fail")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    second_id = "exec-2"
    second = AgentSession(
        backend=ScriptedBackend([ModelTurn("second", (), ModelStopReason.COMPLETED, ModelUsage())]),
        registry=ToolRegistry(),
        policy=PolicyEvaluator(),
        context=ToolExecutionContext(LocalWorkspace(tmp_path), run_id=run.run_id, execution_id=second_id),
        event_sink=CanonicalAgentEventSink(runtime, run.run_id, execution_id=second_id),
        execution_id=second_id,
    )
    second.start("second")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    gate = RunCompletionGate(runtime)
    with pytest.raises(RunCompletionError):
        gate.fail_verification(run.run_id, verification_id="ver-fail")
    append_verification(runtime, run.run_id, "ver-repaired", "pass")
    gate.complete_verified(run.run_id, verification_id="ver-repaired")
    assert runtime.get_run(run.run_id).status is RunStatus.SUCCEEDED


@pytest.mark.parametrize("status,code", [("fail", "verification_fail"), ("timeout", "verification_timeout"), ("error", "verification_error")])
def test_explicit_verification_failure_statuses(tmp_path, status, code):
    runtime, _, run = setup_runtime(tmp_path)
    append_execution(runtime, run.run_id, "exec", RunEventType.EXECUTION_COMPLETED)
    append_verification(runtime, run.run_id, "ver", status)
    RunCompletionGate(runtime).fail_verification(run.run_id, verification_id="ver")
    assert runtime.get_run(run.run_id).status is RunStatus.FAILED
    assert runtime.get_run(run.run_id).error_code == code


def test_gate_rejects_stale_verification_execution_and_invalid_states(tmp_path):
    runtime, _, run = setup_runtime(tmp_path)
    append_execution(runtime, run.run_id, "e1", RunEventType.EXECUTION_COMPLETED)
    append_verification(runtime, run.run_id, "v1", "pass")
    append_verification(runtime, run.run_id, "v2", "fail")
    gate = RunCompletionGate(runtime)
    with pytest.raises(RunCompletionError):
        gate.complete_verified(run.run_id, verification_id="v1")
    with pytest.raises(RunCompletionError):
        gate.complete_verified(run.run_id, verification_id="v2")
    with pytest.raises(RunCompletionError):
        gate.fail_verification(run.run_id, verification_id="v1")

    runtime, _, run = setup_runtime(tmp_path / "stale-exec")
    append_execution(runtime, run.run_id, "e1", RunEventType.EXECUTION_FAILED)
    append_execution(runtime, run.run_id, "e2", RunEventType.EXECUTION_COMPLETED)
    with pytest.raises(RunCompletionError):
        RunCompletionGate(runtime).fail_execution(run.run_id, execution_id="e1")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


def test_gate_rejects_verification_evidence_after_newer_execution_activity(tmp_path):
    runtime, _, run = setup_runtime(tmp_path)
    append_execution(runtime, run.run_id, "e1", RunEventType.EXECUTION_COMPLETED)
    append_verification(runtime, run.run_id, "v1", "pass")
    append_execution(runtime, run.run_id, "e2", RunEventType.EXECUTION_COMPLETED)
    with pytest.raises(RunCompletionError):
        RunCompletionGate(runtime).complete_verified(run.run_id, verification_id="v1")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    assert RunEventType.RUN_COMPLETED not in [event.type for event in runtime.events(run.run_id, limit=200).events]

    runtime, _, run = setup_runtime(tmp_path / "active")
    append_execution(runtime, run.run_id, "e1", RunEventType.EXECUTION_COMPLETED)
    append_verification(runtime, run.run_id, "v1", "pass")
    append_execution(runtime, run.run_id, "e2", RunEventType.EXECUTION_STARTED)
    with pytest.raises(RunCompletionError):
        RunCompletionGate(runtime).complete_verified(run.run_id, verification_id="v1")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


def test_gate_requires_execution_to_be_settled_when_verification_starts(tmp_path):
    runtime, _, run = setup_runtime(tmp_path / "pass-active")
    append_execution(runtime, run.run_id, "e1", RunEventType.EXECUTION_COMPLETED)
    append_execution(runtime, run.run_id, "e2", RunEventType.EXECUTION_STARTED)
    append_verification(runtime, run.run_id, "v1", "pass")
    with pytest.raises(RunCompletionError):
        RunCompletionGate(runtime).complete_verified(run.run_id, verification_id="v1")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    assert RunEventType.RUN_COMPLETED not in [
        event.type for event in runtime.events(run.run_id, limit=200).events
    ]

    runtime, _, run = setup_runtime(tmp_path / "fail-active")
    append_execution(runtime, run.run_id, "e1", RunEventType.EXECUTION_COMPLETED)
    append_execution(runtime, run.run_id, "e2", RunEventType.EXECUTION_STARTED)
    append_verification(runtime, run.run_id, "v1", "fail")
    with pytest.raises(RunCompletionError):
        RunCompletionGate(runtime).fail_verification(run.run_id, verification_id="v1")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    assert RunEventType.RUN_FAILED not in [
        event.type for event in runtime.events(run.run_id, limit=200).events
    ]

    runtime, _, run = setup_runtime(tmp_path / "pass-settled")
    append_execution(runtime, run.run_id, "e1", RunEventType.EXECUTION_COMPLETED)
    append_execution(runtime, run.run_id, "e2", RunEventType.EXECUTION_STARTED)
    append_execution(runtime, run.run_id, "e2", RunEventType.EXECUTION_COMPLETED)
    append_verification(runtime, run.run_id, "v1", "pass")
    RunCompletionGate(runtime).complete_verified(run.run_id, verification_id="v1")
    completed = runtime.events(run.run_id, limit=200).events[-1]
    assert completed.type == RunEventType.RUN_COMPLETED
    assert completed.payload["verified_execution_id"] == "e2"


def test_fail_execution_rejects_retry_that_has_only_started(tmp_path):
    runtime, _, run = setup_runtime(tmp_path)
    append_execution(runtime, run.run_id, "e1", RunEventType.EXECUTION_FAILED)
    append_execution(runtime, run.run_id, "e2", RunEventType.EXECUTION_STARTED)
    with pytest.raises(RunCompletionError):
        RunCompletionGate(runtime).fail_execution(run.run_id, execution_id="e1")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


def test_fail_verification_rejects_repair_execution_that_has_only_started(tmp_path):
    runtime, _, run = setup_runtime(tmp_path)
    append_execution(runtime, run.run_id, "e1", RunEventType.EXECUTION_COMPLETED)
    append_verification(runtime, run.run_id, "v1", "fail")
    append_execution(runtime, run.run_id, "e2", RunEventType.EXECUTION_STARTED)
    with pytest.raises(RunCompletionError):
        RunCompletionGate(runtime).fail_verification(run.run_id, verification_id="v1")
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING


def test_gate_uses_optimistic_snapshot_without_retry(tmp_path, monkeypatch):
    runtime, _, run = setup_runtime(tmp_path)
    append_execution(runtime, run.run_id, "exec", RunEventType.EXECUTION_COMPLETED)
    append_verification(runtime, run.run_id, "ver", "pass")
    gate = RunCompletionGate(runtime)
    original = runtime.record
    injected = False

    def racing_record(**kwargs):
        nonlocal injected
        if not injected and kwargs["type"] == RunEventType.RUN_COMPLETED:
            injected = True
            original(run_id=run.run_id, type="future.event", payload={}, source="other")
        return original(**kwargs)

    monkeypatch.setattr(runtime, "record", racing_record)
    with pytest.raises(EventSequenceError):
        gate.complete_verified(run.run_id, verification_id="ver")
    assert RunEventType.RUN_COMPLETED not in [event.type for event in runtime.events(run.run_id, limit=200).events]


def test_recovery_does_not_invent_terminal_result_for_ungated_windows(tmp_path):
    runtime, _, run = setup_runtime(tmp_path)
    append_execution(runtime, run.run_id, "exec", RunEventType.EXECUTION_COMPLETED)
    recover_running_runs(runtime)
    assert runtime.get_run(run.run_id).status is RunStatus.INTERRUPTED
    assert RunEventType.RUN_COMPLETED not in [event.type for event in runtime.events(run.run_id, limit=200).events]

    runtime, _, run = setup_runtime(tmp_path / "verification")
    append_execution(runtime, run.run_id, "exec", RunEventType.EXECUTION_COMPLETED)
    append_verification(runtime, run.run_id, "ver", "pass")
    recover_running_runs(runtime)
    assert runtime.get_run(run.run_id).status is RunStatus.INTERRUPTED


def test_reviewer_canonical_lifecycle_does_not_block_completion_gate(tmp_path):
    """Milestone 3G regression: worker -> verification PASS -> reviewer
    APPROVED lifecycle must still let RunCompletionGate.complete_verified
    succeed, and the Reviewer must not have produced any canonical
    execution.* lifecycle event (see run_runtime/reviewer.py)."""
    runtime, _, run = setup_runtime(tmp_path)
    agent = AgentSession(
        backend=ScriptedBackend([ModelTurn("done", (), ModelStopReason.COMPLETED, ModelUsage())]),
        registry=ToolRegistry(),
        policy=PolicyEvaluator(),
        context=ToolExecutionContext(LocalWorkspace(tmp_path), run_id=run.run_id, execution_id="exec-1"),
        event_sink=CanonicalAgentEventSink(runtime, run.run_id, execution_id="exec-1"),
        execution_id="exec-1",
    )
    agent.start("task")

    verification_id = "ver-1"
    VerificationRunner(
        FakeProcessRunner(),
        CanonicalVerificationEventSink(runtime, run.run_id, verification_id=verification_id),
    ).run(
        LocalWorkspace(tmp_path),
        VerificationPlan(("plan"), (
            VerificationCheck("check", "Check", ProcessRequest((sys.executable, "-c", "pass"))),
        )),
        verification_id=verification_id,
    )

    review_id = "rev-1"
    review_sink = CanonicalReviewEventSink(runtime, run.run_id, review_id=review_id)
    reviewer_agent = AgentSession(
        backend=ScriptedBackend([ModelTurn(
            '{"verdict":"APPROVED","summary":"ok","findings":[]}', (), ModelStopReason.COMPLETED, ModelUsage(),
        )]),
        registry=ToolRegistry(),
        policy=PolicyEvaluator(),
        context=ToolExecutionContext(LocalWorkspace(tmp_path)),
        event_sink=review_sink,
        execution_id=f"review_exec_{review_id}",
    )
    outcome = reviewer_agent.start("review this")
    review_sink.complete(ReviewReport(
        review_id=review_id, verdict=ReviewVerdict.APPROVED, summary=outcome.final_text[:200] or "ok",
        findings=(), repository_fingerprint="a" * 64, diff_sha256="b" * 64,
        verification_id=verification_id, verification_status="pass",
    ))

    events = runtime.events(run.run_id, limit=200).events
    reviewer_events = [event for event in events if event.source == "reviewer"]
    assert reviewer_events, "reviewer must have produced canonical events"
    lifecycle_types = {RunEventType.EXECUTION_STARTED, RunEventType.EXECUTION_COMPLETED, RunEventType.EXECUTION_FAILED}
    assert all(event.type not in lifecycle_types for event in reviewer_events)
    assert all(event.execution_id is None for event in reviewer_events)
    assert reviewer_events[-1].type == RunEventType.REVIEW_COMPLETED

    RunCompletionGate(runtime).complete_verified(run.run_id, verification_id=verification_id)
    completed = runtime.get_run(run.run_id)
    assert completed.status is RunStatus.SUCCEEDED
    assert runtime.events(run.run_id, limit=200).events[-1].type == RunEventType.RUN_COMPLETED
