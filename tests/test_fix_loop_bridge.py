import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_runtime import CanonicalFixLoopRecorder, RunEventSpec, RunEventType, RunRuntime, RunStore  # noqa: E402
from run_runtime.errors import EventSequenceError, FixLoopRecordingError  # noqa: E402


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def event_types(runtime, run):
    return [event.type for event in runtime.events(run.run_id, limit=200).events]


def latest_events(runtime, run):
    return runtime.events(run.run_id, limit=200).events


# ---------------- start ----------------


def test_start_persists_fix_loop_started_with_correct_metadata(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    event = latest_events(runtime, run)[-1]
    assert event.type == RunEventType.FIX_LOOP_STARTED
    assert event.execution_id is None
    assert event.source == "fix_loop"
    assert event.correlation_id == "fix-1"
    assert event.payload == {"fix_loop_id": "fix-1"}


def test_start_twice_on_same_recorder_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    with pytest.raises(FixLoopRecordingError):
        recorder.start()


def test_loop_id_reuse_by_second_recorder_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1").start()
    second = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    with pytest.raises(ValueError):
        second.start()


def test_recorder_requires_running_run(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_CANCELLED, payload={})
    with pytest.raises(ValueError):
        CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")


# ---------------- attempt ordering ----------------


def test_attempt_started_before_loop_started_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    with pytest.raises(FixLoopRecordingError):
        recorder.attempt_started(
            fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
            worker_execution_id="exec-1", before_diff_sha256="a" * 64,
        )
    assert event_types(runtime, run) == [RunEventType.RUN_STARTED]


def test_attempt_started_is_committed_before_any_worker_side_effect_would_run(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.attempt_started(
        fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
        worker_execution_id="exec-1", before_diff_sha256="a" * 64,
    )
    event = latest_events(runtime, run)[-1]
    assert event.type == RunEventType.FIX_ATTEMPT_STARTED
    assert event.execution_id is None
    assert event.payload["worker_execution_id"] == "exec-1"
    assert event.payload["fix_loop_id"] == "fix-1"
    assert event.payload["attempt_index"] == 1


def test_attempt_completed(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.attempt_started(
        fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
        worker_execution_id="exec-1", before_diff_sha256="a" * 64,
    )
    recorder.attempt_completed(
        fix_attempt_id="a1", attempt_index=1, worker_execution_id="exec-1",
        before_diff_sha256="a" * 64, after_diff_sha256="c" * 64, changed=True,
    )
    event = latest_events(runtime, run)[-1]
    assert event.type == RunEventType.FIX_ATTEMPT_COMPLETED
    assert event.payload["changed"] is True


def test_invalid_attempt_order_second_attempt_before_first(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    with pytest.raises(FixLoopRecordingError):
        recorder.attempt_started(
            fix_attempt_id="a2", attempt_index=2, trigger_kind="verification_fail",
            worker_execution_id="exec-2", before_diff_sha256="a" * 64,
        )


def test_duplicate_attempt_index_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.attempt_started(
        fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
        worker_execution_id="exec-1", before_diff_sha256="a" * 64,
    )
    recorder.attempt_completed(
        fix_attempt_id="a1", attempt_index=1, worker_execution_id="exec-1",
        before_diff_sha256="a" * 64, after_diff_sha256="c" * 64, changed=True,
    )
    with pytest.raises(FixLoopRecordingError):
        recorder.attempt_started(
            fix_attempt_id="a1b", attempt_index=1, trigger_kind="verification_fail",
            worker_execution_id="exec-1b", before_diff_sha256="c" * 64,
        )


def test_no_duplicate_active_attempt(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.attempt_started(
        fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
        worker_execution_id="exec-1", before_diff_sha256="a" * 64,
    )
    with pytest.raises(FixLoopRecordingError):
        recorder.attempt_started(
            fix_attempt_id="a2", attempt_index=2, trigger_kind="verification_fail",
            worker_execution_id="exec-2", before_diff_sha256="a" * 64,
        )


def test_wrong_loop_or_attempt_id_on_completion_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.attempt_started(
        fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
        worker_execution_id="exec-1", before_diff_sha256="a" * 64,
    )
    with pytest.raises(FixLoopRecordingError):
        recorder.attempt_completed(
            fix_attempt_id="WRONG", attempt_index=1, worker_execution_id="exec-1",
            before_diff_sha256="a" * 64, after_diff_sha256="c" * 64, changed=True,
        )
    with pytest.raises(FixLoopRecordingError):
        recorder.attempt_completed(
            fix_attempt_id="a1", attempt_index=2, worker_execution_id="exec-1",
            before_diff_sha256="a" * 64, after_diff_sha256="c" * 64, changed=True,
        )


def test_attempt_completion_before_start_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    with pytest.raises(FixLoopRecordingError):
        recorder.attempt_completed(
            fix_attempt_id="a1", attempt_index=1, worker_execution_id="exec-1",
            before_diff_sha256="a" * 64, after_diff_sha256="c" * 64, changed=True,
        )


# ---------------- attempt interruption (infrastructure abort settlement) ----------------


def test_attempt_interrupted_settles_the_active_attempt(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.attempt_started(
        fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
        worker_execution_id="exec-1", before_diff_sha256="a" * 64,
    )
    assert recorder.has_active_attempt is True
    recorder.attempt_interrupted(reason="infrastructure_error")
    assert recorder.has_active_attempt is False
    event = latest_events(runtime, run)[-1]
    assert event.type == RunEventType.FIX_ATTEMPT_INTERRUPTED
    assert event.execution_id is None
    assert event.payload["fix_loop_id"] == "fix-1"
    assert event.payload["fix_attempt_id"] == "a1"
    assert event.payload["attempt_index"] == 1
    assert event.payload["worker_execution_id"] == "exec-1"
    assert event.payload["reason"] == "infrastructure_error"
    assert event.payload["outcome_unknown"] is True


def test_attempt_interrupted_without_active_attempt_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    with pytest.raises(FixLoopRecordingError):
        recorder.attempt_interrupted(reason="infrastructure_error")
    assert event_types(runtime, run) == [RunEventType.RUN_STARTED, RunEventType.FIX_LOOP_STARTED]


def test_attempt_interrupted_after_attempt_completed_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.attempt_started(
        fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
        worker_execution_id="exec-1", before_diff_sha256="a" * 64,
    )
    recorder.attempt_completed(
        fix_attempt_id="a1", attempt_index=1, worker_execution_id="exec-1",
        before_diff_sha256="a" * 64, after_diff_sha256="c" * 64, changed=True,
    )
    with pytest.raises(FixLoopRecordingError):
        recorder.attempt_interrupted(reason="infrastructure_error")


def test_loop_terminal_rejected_while_attempt_still_active(tmp_path):
    """3.E: fix_loop.completed/exhausted/failed must be rejected while a fix
    attempt is still active, unless that attempt is first completed or
    interrupted."""
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.attempt_started(
        fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
        worker_execution_id="exec-1", before_diff_sha256="a" * 64,
    )
    with pytest.raises(FixLoopRecordingError):
        recorder.failed(reason="infrastructure_error")
    with pytest.raises(FixLoopRecordingError):
        recorder.exhausted(reason="budget_exhausted", attempts_used=1, max_fix_attempts=1)
    with pytest.raises(FixLoopRecordingError):
        recorder.completed(attempts_used=1, final_execution_id="exec-1", verification_id="v", review_id="r", diff_sha256="a" * 64)
    assert event_types(runtime, run) == [
        RunEventType.RUN_STARTED, RunEventType.FIX_LOOP_STARTED, RunEventType.FIX_ATTEMPT_STARTED,
    ]
    # settling the attempt first must then allow a terminal to be recorded.
    recorder.attempt_interrupted(reason="infrastructure_error")
    recorder.failed(reason="infrastructure_error")
    assert latest_events(runtime, run)[-1].type == RunEventType.FIX_LOOP_FAILED


# ---------------- terminal outcomes ----------------


def test_completed_terminal(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.completed(
        attempts_used=1, final_execution_id="exec-1", verification_id="ver-1",
        review_id="rev-1", diff_sha256="c" * 64,
    )
    event = latest_events(runtime, run)[-1]
    assert event.type == RunEventType.FIX_LOOP_COMPLETED
    assert event.execution_id is None


def test_exhausted_terminal(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.exhausted(reason="budget_exhausted", attempts_used=2, max_fix_attempts=2)
    event = latest_events(runtime, run)[-1]
    assert event.type == RunEventType.FIX_LOOP_EXHAUSTED
    assert event.payload["reason"] == "budget_exhausted"


def test_failed_terminal(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.failed(reason="verification_timeout")
    event = latest_events(runtime, run)[-1]
    assert event.type == RunEventType.FIX_LOOP_FAILED
    assert event.payload["reason"] == "verification_timeout"


def test_duplicate_loop_terminal_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.failed(reason="verification_timeout")
    with pytest.raises(FixLoopRecordingError):
        recorder.failed(reason="verification_error")
    with pytest.raises(FixLoopRecordingError):
        recorder.completed(attempts_used=1, final_execution_id=None, verification_id="v", review_id="r", diff_sha256="a" * 64)


def test_event_after_terminal_rejected(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.failed(reason="verification_timeout")
    with pytest.raises(FixLoopRecordingError):
        recorder.attempt_started(
            fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
            worker_execution_id="exec-1", before_diff_sha256="a" * 64,
        )


def test_all_fix_events_have_execution_id_none_and_correct_source_correlation(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.attempt_started(
        fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
        worker_execution_id="exec-worker-1", before_diff_sha256="a" * 64,
    )
    recorder.attempt_completed(
        fix_attempt_id="a1", attempt_index=1, worker_execution_id="exec-worker-1",
        before_diff_sha256="a" * 64, after_diff_sha256="c" * 64, changed=True,
    )
    recorder.completed(
        attempts_used=1, final_execution_id="exec-worker-1", verification_id="ver-1",
        review_id="rev-1", diff_sha256="c" * 64,
    )
    fix_events = [event for event in latest_events(runtime, run) if event.source == "fix_loop"]
    assert len(fix_events) == 4  # started, attempt.started, attempt.completed, completed
    for event in fix_events:
        assert event.execution_id is None
        assert event.correlation_id == "fix-1"
    # worker_execution_id must exist only inside payloads, never at top level.
    started_attempt = next(e for e in fix_events if e.type == RunEventType.FIX_ATTEMPT_STARTED)
    assert started_attempt.payload["worker_execution_id"] == "exec-worker-1"
    assert started_attempt.execution_id is None


# ---------------- boundary sequence / interleaving ----------------


def test_boundary_sequence_conflict_surfaces_event_sequence_error(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()

    original_record = runtime.record

    def racing_record(**kwargs):
        # Simulate a concurrent writer racing between the recorder's
        # sequence read and its own append.
        if kwargs.get("type") == RunEventType.FIX_ATTEMPT_STARTED and not racing_record.done:
            racing_record.done = True
            original_record(run_id=run.run_id, type="future.event", payload={}, source="other")
        return original_record(**kwargs)

    racing_record.done = False
    runtime.record = racing_record
    try:
        with pytest.raises(EventSequenceError):
            recorder.attempt_started(
                fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
                worker_execution_id="exec-1", before_diff_sha256="a" * 64,
            )
    finally:
        runtime.record = original_record


def test_recorder_tolerates_worker_verification_reviewer_events_appended_between_boundaries(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    recorder = CanonicalFixLoopRecorder(runtime, run.run_id, fix_loop_id="fix-1")
    recorder.start()
    recorder.attempt_started(
        fix_attempt_id="a1", attempt_index=1, trigger_kind="verification_fail",
        worker_execution_id="exec-1", before_diff_sha256="a" * 64,
    )
    # Simulate the Worker/Verification/Reviewer adapters appending their own
    # canonical events between this recorder's own boundary writes.
    runtime.record(
        run_id=run.run_id, type=RunEventType.EXECUTION_STARTED, payload={"task": "x"},
        execution_id="exec-1", correlation_id="exec-1", source="native_agent",
    )
    runtime.record(
        run_id=run.run_id, type=RunEventType.EXECUTION_COMPLETED, payload={"final_text": "done"},
        execution_id="exec-1", correlation_id="exec-1", source="native_agent",
    )
    # This must NOT raise EventSequenceError even though other events were
    # appended after attempt_started's own commit.
    recorder.attempt_completed(
        fix_attempt_id="a1", attempt_index=1, worker_execution_id="exec-1",
        before_diff_sha256="a" * 64, after_diff_sha256="c" * 64, changed=True,
    )
    event = latest_events(runtime, run)[-1]
    assert event.type == RunEventType.FIX_ATTEMPT_COMPLETED
