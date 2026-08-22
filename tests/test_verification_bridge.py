import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process_runtime import ProcessRequest, ProcessResult  # noqa: E402
from run_runtime import (  # noqa: E402
    CanonicalVerificationEventSink,
    RunEventType,
    RunEventSpec,
    RunRuntime,
    RunStatus,
    RunStore,
    build_receipt,
)
from run_runtime.readmodels import RunReadSnapshot  # noqa: E402
from run_runtime.recovery import recover_running_runs  # noqa: E402
from verification_runtime import (  # noqa: E402
    VerificationCheck,
    VerificationCheckCompleted,
    VerificationCheckFailed,
    VerificationCheckResult,
    VerificationPlan,
    VerificationRunner,
    VerificationStarted,
    VerificationStatus,
)
from workspace.local import LocalWorkspace  # noqa: E402


def result(exit_code=0):
    return ProcessResult(
        argv=(sys.executable,), cwd=".", exit_code=exit_code, timed_out=False,
        duration_ms=2, stdout="ok", stderr="", stdout_truncated=False,
        stderr_truncated=False, stdout_bytes=2, stderr_bytes=0,
    )


class FakeRunner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def run(self, workspace, request):
        self.calls.append(request)
        return self.outcomes.pop(0)


def active_run(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="verify")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, task, run


def plan(env=None):
    return VerificationPlan("plan-1", (
        VerificationCheck(
            "check-a", "Check A",
            ProcessRequest((sys.executable, "-c", "pass"), env=env or {"IMECE_TEST_SECRET": "do-not-persist-value"}),
        ),
        VerificationCheck("check-b", "Check B", ProcessRequest((sys.executable, "-c", "pass"))),
    ))


def test_canonical_bridge_success_preserves_audit_fields_and_run_stays_running(tmp_path):
    runtime, task, run = active_run(tmp_path)
    verification_id = "ver_bridge"
    sink = CanonicalVerificationEventSink(runtime, run.run_id, verification_id=verification_id)
    report = VerificationRunner(FakeRunner([result(), result()]), sink).run(
        LocalWorkspace(tmp_path), plan(), verification_id=verification_id
    )
    assert report.status is VerificationStatus.PASS
    assert runtime.get_run(run.run_id).status is RunStatus.RUNNING
    events = runtime.events(run.run_id, limit=200).events
    verification_events = [event for event in events if event.source == "verification"]
    assert [event.type for event in verification_events] == [
        RunEventType.VERIFICATION_STARTED,
        RunEventType.VERIFICATION_CHECK_STARTED,
        RunEventType.VERIFICATION_CHECK_COMPLETED,
        RunEventType.VERIFICATION_CHECK_STARTED,
        RunEventType.VERIFICATION_CHECK_COMPLETED,
        RunEventType.VERIFICATION_COMPLETED,
    ]
    assert all(event.correlation_id == verification_id for event in verification_events)
    check_items = [event.item_id for event in verification_events if event.type == RunEventType.VERIFICATION_CHECK_STARTED]
    assert len(set(check_items)) == 2
    started = verification_events[1]
    assert started.payload["argv"] == [sys.executable, "-c", "pass"]
    assert started.payload["timeout_ms"] == 120000
    assert "do-not-persist-value" not in str(started.payload)
    assert started.payload["env_override_keys"] == ["IMECE_TEST_SECRET"]
    completed = verification_events[2]
    assert completed.payload["status"] == "pass"
    assert completed.payload["execution_isolation"] == "host"
    assert verification_events[-1].payload["counts"] == {
        "pass": 2, "fail": 0, "timeout": 0, "error": 0, "total": 2,
    }


def test_canonical_process_error_is_check_error_and_later_check_runs(tmp_path):
    runtime, _, run = active_run(tmp_path)
    verification_id = "ver_error"
    sink = CanonicalVerificationEventSink(runtime, run.run_id, verification_id=verification_id)
    from process_runtime.errors import ProcessSpawnError  # noqa: E402

    class ErrorRunner(FakeRunner):
        def run(self, workspace, request):
            self.calls.append(request)
            if len(self.calls) == 1:
                raise ProcessSpawnError("missing executable")
            return result()

    report = VerificationRunner(ErrorRunner([]), sink).run(
        LocalWorkspace(tmp_path), plan(), verification_id=verification_id
    )
    assert report.status is VerificationStatus.ERROR
    assert len(report.results) == 2
    events = [event for event in runtime.events(run.run_id, limit=200).events if event.source == "verification"]
    assert [event.type for event in events] == [
        RunEventType.VERIFICATION_STARTED,
        RunEventType.VERIFICATION_CHECK_STARTED,
        RunEventType.VERIFICATION_CHECK_FAILED,
        RunEventType.VERIFICATION_CHECK_STARTED,
        RunEventType.VERIFICATION_CHECK_COMPLETED,
        RunEventType.VERIFICATION_COMPLETED,
    ]
    assert events[2].payload["status"] == "error"
    assert events[2].item_id == events[1].item_id


def test_canonical_sink_rejects_inconsistent_check_results_before_persistence(tmp_path):
    runtime, _, run = active_run(tmp_path)
    sink = CanonicalVerificationEventSink(runtime, run.run_id, verification_id="ver_coherence")
    first = VerificationCheck("first", "First", ProcessRequest((sys.executable, "-c", "pass")))
    second = VerificationCheck("second", "Second", ProcessRequest((sys.executable, "-c", "pass")))
    completed = VerificationCheckCompleted(
        "ver_coherence", second, VerificationCheckResult("first", "First", VerificationStatus.PASS, result())
    )
    with pytest.raises(ValueError):
        sink.emit(completed)
    failed = VerificationCheckFailed(
        "ver_coherence", first,
        VerificationCheckResult("first", "First", VerificationStatus.ERROR, result()),
    )
    with pytest.raises(ValueError):
        sink.emit(failed)
    assert len(runtime.events(run.run_id, limit=200).events) == 1


def test_canonical_sink_rejects_verification_id_reuse(tmp_path):
    runtime, _, run = active_run(tmp_path)
    first = CanonicalVerificationEventSink(runtime, run.run_id, verification_id="ver_reused")
    event = VerificationStarted("ver_reused", "plan-1", 1)
    first.emit(event)
    before = runtime.get_run(run.run_id).last_event_seq
    second = CanonicalVerificationEventSink(runtime, run.run_id, verification_id="ver_reused")
    with pytest.raises(ValueError):
        second.emit(event)
    assert runtime.get_run(run.run_id).last_event_seq == before


def test_recovery_interrupts_unfinished_verification_and_tool_atomically(tmp_path):
    runtime, _, run = active_run(tmp_path)
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(
            type=RunEventType.VERIFICATION_STARTED,
            payload={"verification_id": "ver_recover", "plan_id": "p", "check_count": 1},
            correlation_id="ver_recover", source="verification",
        ),
        RunEventSpec(
            type=RunEventType.VERIFICATION_CHECK_STARTED,
            payload={"verification_id": "ver_recover", "check_id": "a", "name": "A"},
            item_id="verification:ver_recover:a", correlation_id="ver_recover", source="verification",
        ),
        RunEventSpec(
            type=RunEventType.TOOL_STARTED,
            payload={"call_id": "tool", "tool_name": "x"}, item_id="tool-item", source="native_agent",
        ),
    ))
    before = runtime.get_run(run.run_id).last_event_seq
    report = recover_running_runs(runtime)
    assert report.interrupted_run_ids == (run.run_id,)
    suffix = runtime.events(run.run_id, after_seq=before, limit=200).events
    assert [event.type for event in suffix] == [
        RunEventType.TOOL_INTERRUPTED,
        RunEventType.VERIFICATION_CHECK_INTERRUPTED,
        RunEventType.VERIFICATION_INTERRUPTED,
        RunEventType.RUN_INTERRUPTED,
    ]
    assert suffix[1].payload["outcome_unknown"] is True
    assert runtime.get_run(run.run_id).status is RunStatus.INTERRUPTED
    assert recover_running_runs(runtime).interrupted_run_ids == ()


def test_receipt_uses_latest_verification_attempt_by_sequence(tmp_path):
    runtime, task, run = active_run(tmp_path)
    def spec(event_type, payload):
        return RunEventSpec(type=event_type, payload=payload, source="verification")

    runtime.record_many(run_id=run.run_id, specs=(
        spec(RunEventType.VERIFICATION_STARTED, {"verification_id": "v1", "plan_id": "p", "check_count": 1}),
        spec(RunEventType.VERIFICATION_COMPLETED, {"verification_id": "v1", "plan_id": "p", "status": "fail", "duration_ms": 1, "counts": {"pass": 0, "fail": 1, "timeout": 0, "error": 0, "total": 1}}),
        spec(RunEventType.VERIFICATION_STARTED, {"verification_id": "v2", "plan_id": "p", "check_count": 2}),
        spec(RunEventType.VERIFICATION_COMPLETED, {"verification_id": "v2", "plan_id": "p", "status": "pass", "duration_ms": 1, "counts": {"pass": 2, "fail": 0, "timeout": 0, "error": 0, "total": 2}}),
    ))
    snapshot = RunReadSnapshot(
        task=task, run=runtime.get_run(run.run_id),
        events=runtime.events(run.run_id, limit=200).events,
    )
    receipt = build_receipt(snapshot)
    assert receipt["verification"] == {"status": "pass", "detail": "2/2 checks passed."}


def test_receipt_reused_id_is_sequence_scoped_and_invalid_status_is_error(tmp_path):
    runtime, task, run = active_run(tmp_path)
    def spec(event_type, payload):
        return RunEventSpec(type=event_type, payload=payload, source="verification")

    runtime.record_many(run_id=run.run_id, specs=(
        spec(RunEventType.VERIFICATION_STARTED, {"verification_id": "same", "plan_id": "p", "check_count": 1}),
        spec(RunEventType.VERIFICATION_COMPLETED, {"verification_id": "same", "plan_id": "p", "status": "fail", "duration_ms": 1, "counts": {"pass": 0, "fail": 1, "timeout": 0, "error": 0, "total": 1}}),
        spec(RunEventType.VERIFICATION_STARTED, {"verification_id": "same", "plan_id": "p", "check_count": 1}),
    ))
    snapshot = RunReadSnapshot(task=task, run=runtime.get_run(run.run_id), events=runtime.events(run.run_id, limit=200).events)
    assert build_receipt(snapshot)["verification"] == {"status": "running", "detail": "Verification is running."}
    runtime.record(
        run_id=run.run_id,
        type=RunEventType.VERIFICATION_COMPLETED,
        payload={"verification_id": "same", "plan_id": "p", "status": "not-valid", "duration_ms": 1, "counts": {}},
        source="verification",
    )
    snapshot = RunReadSnapshot(task=task, run=runtime.get_run(run.run_id), events=runtime.events(run.run_id, limit=200).events)
    assert build_receipt(snapshot)["verification"] == {
        "status": "error", "detail": "Verification completed with an invalid status."
    }


def test_receipt_completed_statuses_have_deterministic_details(tmp_path):
    runtime, task, run = active_run(tmp_path)
    cases = [
        ("pass", {"pass": 2, "fail": 0, "timeout": 0, "error": 0, "total": 2}, "2/2 checks passed."),
        ("fail", {"pass": 0, "fail": 1, "timeout": 0, "error": 0, "total": 1}, "1 failed."),
        ("timeout", {"pass": 0, "fail": 0, "timeout": 1, "error": 0, "total": 1}, "1 timed out."),
        ("error", {"pass": 0, "fail": 0, "timeout": 0, "error": 1, "total": 1}, "1 errors."),
    ]
    for index, (status, counts, detail) in enumerate(cases):
        verification_id = f"status-{index}"
        runtime.record_many(run_id=run.run_id, specs=(
            RunEventSpec(
                type=RunEventType.VERIFICATION_STARTED,
                payload={"verification_id": verification_id, "plan_id": "p", "check_count": 1},
                source="verification",
            ),
            RunEventSpec(
                type=RunEventType.VERIFICATION_COMPLETED,
                payload={
                    "verification_id": verification_id,
                    "plan_id": "p",
                    "status": status,
                    "duration_ms": 1,
                    "counts": counts,
                },
                source="verification",
            ),
        ))
        snapshot = RunReadSnapshot(
            task=task,
            run=runtime.get_run(run.run_id),
            events=runtime.events(run.run_id, limit=200).events,
        )
        assert build_receipt(snapshot)["verification"] == {"status": status, "detail": detail}


def test_recovery_scopes_reused_verification_id_by_sequence(tmp_path):
    runtime, _, run = active_run(tmp_path)
    verification_id = "malformed-reuse"
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(
            type=RunEventType.VERIFICATION_STARTED,
            payload={"verification_id": verification_id, "plan_id": "p", "check_count": 1},
            correlation_id=verification_id, source="verification",
        ),
        RunEventSpec(
            type=RunEventType.VERIFICATION_COMPLETED,
            payload={"verification_id": verification_id, "plan_id": "p", "status": "fail", "counts": {}},
            correlation_id=verification_id, source="verification",
        ),
        RunEventSpec(
            type=RunEventType.VERIFICATION_STARTED,
            payload={"verification_id": verification_id, "plan_id": "p", "check_count": 1},
            correlation_id=verification_id, source="verification",
        ),
        RunEventSpec(
            type=RunEventType.VERIFICATION_CHECK_STARTED,
            payload={"verification_id": verification_id, "check_id": "a", "name": "A"},
            item_id="verification:malformed-reuse:a",
            correlation_id=verification_id, source="verification",
        ),
    ))
    before = runtime.get_run(run.run_id).last_event_seq
    recover_running_runs(runtime)
    suffix = runtime.events(run.run_id, after_seq=before, limit=200).events
    assert [event.type for event in suffix] == [
        RunEventType.VERIFICATION_CHECK_INTERRUPTED,
        RunEventType.VERIFICATION_INTERRUPTED,
        RunEventType.RUN_INTERRUPTED,
    ]


def test_receipt_derives_not_run_running_and_interrupted_states(tmp_path):
    runtime, task, run = active_run(tmp_path)

    def receipt():
        current = runtime.get_run(run.run_id)
        return build_receipt(RunReadSnapshot(
            task=task,
            run=current,
            events=runtime.events(run.run_id, limit=200).events,
        ))["verification"]

    assert receipt() == {
        "status": "not_run",
        "detail": "Bu koşuda doğrulama komutu çalıştırılmadı.",
    }
    runtime.record(
        run_id=run.run_id,
        type=RunEventType.VERIFICATION_STARTED,
        payload={"verification_id": "v-running", "plan_id": "p", "check_count": 1},
        source="verification",
    )
    assert receipt() == {"status": "running", "detail": "Verification is running."}
    runtime.record(
        run_id=run.run_id,
        type=RunEventType.VERIFICATION_INTERRUPTED,
        payload={"verification_id": "v-running", "reason": "process_restart"},
        source="recovery",
    )
    assert receipt() == {"status": "interrupted", "detail": "Verification was interrupted."}
