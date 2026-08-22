"""run_runtime.projector — saf, deterministik projeksiyon mantığı testleri (SQLite yok)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_runtime.errors import RunProjectionError  # noqa: E402
from run_runtime.events import RunEvent, RunEventType, build_event  # noqa: E402
from run_runtime.models import RunPhase, RunRecord, RunStatus  # noqa: E402
from run_runtime.projector import project_run  # noqa: E402

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _record(**overrides):
    base = RunRecord.new(run_id="run_x", task_id="task_x", created_at=_T0)
    if overrides:
        import dataclasses

        base = dataclasses.replace(base, **overrides)
    return base


def _raw_event(type_, payload, *, seq=1, created_at=None):
    """RunEvent.__post_init__'i (ve dolayısıyla jsonutil doğrulamasını) TAMAMEN
    baypas ederek, object.__new__ + object.__setattr__ ile bilerek BOZUK bir
    RunEvent üretir.

    YALNIZCA TEST KODUNDA kullanılır — üretim kodunda geçersiz bir RunEvent
    oluşturmanın hiçbir yolu YOKTUR (normal `RunEvent(...)` çağrısı bile artık
    __post_init__ üzerinden aynı katı doğrulamadan geçer). Bu, projector'ın
    NaN/Infinity'ye karşı kendi savunmasını, üst katman (event/payload)
    doğrulamasından TAMAMEN bağımsız olarak kanıtlamak için kasıtlı bir arka
    kapıdır.
    """
    event = object.__new__(RunEvent)
    object.__setattr__(event, "event_id", "evt_raw")
    object.__setattr__(event, "run_id", "run_x")
    object.__setattr__(event, "seq", seq)
    object.__setattr__(event, "type", type_)
    object.__setattr__(event, "schema_version", 1)
    object.__setattr__(event, "created_at", created_at or (_T0 + timedelta(seconds=seq)))
    object.__setattr__(event, "execution_id", None)
    object.__setattr__(event, "turn_id", None)
    object.__setattr__(event, "item_id", None)
    object.__setattr__(event, "causation_id", None)
    object.__setattr__(event, "correlation_id", None)
    object.__setattr__(event, "source", "system")
    object.__setattr__(event, "payload", payload)
    return event


def _event(type_, payload=None, *, seq=1, created_at=None):
    return build_event(
        run_id="run_x", seq=seq, type=type_, payload=payload or {},
        created_at=created_at or (_T0 + timedelta(seconds=seq)),
    )


def test_created_event_sets_created_status_and_phase():
    run = _record()
    projected = project_run(run, _event(RunEventType.RUN_CREATED))
    assert projected.status == RunStatus.CREATED
    assert projected.phase == RunPhase.CREATED


def test_started_event_transitions_running_starting_and_sets_started_at():
    run = _record()
    ev = _event(RunEventType.RUN_STARTED)
    projected = project_run(run, ev)
    assert projected.status == RunStatus.RUNNING
    assert projected.phase == RunPhase.STARTING
    assert projected.started_at == ev.created_at


def test_started_event_does_not_overwrite_existing_started_at():
    first_start = _T0 + timedelta(seconds=1)
    run = _record(started_at=first_start)
    ev = _event(RunEventType.RUN_STARTED, seq=5)
    projected = project_run(run, ev)
    assert projected.started_at == first_start  # ilk değer korunur


@pytest.mark.parametrize(
    "phase", ["planning", "executing", "reviewing", "verifying", "applying", "ready"]
)
def test_phase_changed_updates_phase_for_valid_values(phase):
    run = _record(status=RunStatus.RUNNING, phase=RunPhase.STARTING)
    projected = project_run(run, _event(RunEventType.RUN_PHASE_CHANGED, {"phase": phase}))
    assert projected.phase == RunPhase(phase)
    assert projected.status == RunStatus.RUNNING  # phase_changed status'e dokunmaz


def test_phase_changed_rejects_invalid_phase():
    run = _record()
    with pytest.raises(RunProjectionError):
        project_run(run, _event(RunEventType.RUN_PHASE_CHANGED, {"phase": "banana"}))


def test_phase_changed_rejects_non_string_phase():
    run = _record()
    with pytest.raises(RunProjectionError):
        project_run(run, _event(RunEventType.RUN_PHASE_CHANGED, {"phase": 123}))


def test_waiting_user_sets_waiting_status_and_ready_phase():
    run = _record(status=RunStatus.RUNNING, phase=RunPhase.EXECUTING)
    projected = project_run(run, _event(RunEventType.RUN_WAITING_USER))
    assert projected.status == RunStatus.WAITING_USER
    assert projected.phase == RunPhase.READY


def test_completed_sets_succeeded_done_and_finished_at():
    run = _record(status=RunStatus.RUNNING, phase=RunPhase.REVIEWING)
    ev = _event(RunEventType.RUN_COMPLETED)
    projected = project_run(run, ev)
    assert projected.status == RunStatus.SUCCEEDED
    assert projected.phase == RunPhase.DONE
    assert projected.finished_at == ev.created_at


def test_failed_sets_failed_error_and_error_fields():
    run = _record(status=RunStatus.RUNNING, phase=RunPhase.EXECUTING)
    ev = _event(
        RunEventType.RUN_FAILED,
        {"error_code": "provider_timeout", "error_message": "timed out"},
    )
    projected = project_run(run, ev)
    assert projected.status == RunStatus.FAILED
    assert projected.phase == RunPhase.ERROR
    assert projected.finished_at == ev.created_at
    assert projected.error_code == "provider_timeout"
    assert projected.error_message == "timed out"


def test_failed_without_error_fields_leaves_them_none():
    run = _record()
    projected = project_run(run, _event(RunEventType.RUN_FAILED, {}))
    assert projected.error_code is None
    assert projected.error_message is None


def test_failed_rejects_non_string_error_code():
    run = _record()
    with pytest.raises(RunProjectionError):
        project_run(run, _event(RunEventType.RUN_FAILED, {"error_code": 42}))


def test_cancelled_sets_cancelled_done_and_finished_at():
    run = _record(status=RunStatus.RUNNING, phase=RunPhase.PLANNING)
    ev = _event(RunEventType.RUN_CANCELLED)
    projected = project_run(run, ev)
    assert projected.status == RunStatus.CANCELLED
    assert projected.phase == RunPhase.DONE
    assert projected.finished_at == ev.created_at


def test_interrupted_sets_interrupted_error_and_finished_at():
    run = _record(status=RunStatus.RUNNING, phase=RunPhase.EXECUTING)
    ev = _event(RunEventType.RUN_INTERRUPTED)
    projected = project_run(run, ev)
    assert projected.status == RunStatus.INTERRUPTED
    assert projected.phase == RunPhase.ERROR
    assert projected.finished_at == ev.created_at


def test_proposal_applied_sets_succeeded_applied():
    run = _record(status=RunStatus.WAITING_USER, phase=RunPhase.READY)
    ev = _event(RunEventType.PROPOSAL_APPLIED)
    projected = project_run(run, ev)
    assert projected.status == RunStatus.SUCCEEDED
    assert projected.phase == RunPhase.APPLIED
    assert projected.finished_at == ev.created_at


def test_proposal_rejected_sets_succeeded_rejected():
    run = _record(status=RunStatus.WAITING_USER, phase=RunPhase.READY)
    ev = _event(RunEventType.PROPOSAL_REJECTED)
    projected = project_run(run, ev)
    assert projected.status == RunStatus.SUCCEEDED
    assert projected.phase == RunPhase.REJECTED
    assert projected.finished_at == ev.created_at


def test_checkpoint_restored_sets_succeeded_restored_even_from_terminal_state():
    """Terminal bir durumdan (failed) sonra bile checkpoint.restored sunum aşamasını günceller."""
    run = _record(status=RunStatus.FAILED, phase=RunPhase.ERROR, finished_at=_T0)
    projected = project_run(run, _event(RunEventType.CHECKPOINT_RESTORED))
    assert projected.status == RunStatus.SUCCEEDED
    assert projected.phase == RunPhase.RESTORED


def test_usage_recorded_accumulates_across_multiple_events():
    run = _record()
    ev1 = _event(
        RunEventType.USAGE_RECORDED,
        {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
         "cost_usd": 0.01, "latency_s": 1.5},
        seq=1,
    )
    after1 = project_run(run, ev1)
    assert after1.prompt_tokens == 100
    assert after1.completion_tokens == 50
    assert after1.total_tokens == 150
    assert after1.cost_usd == pytest.approx(0.01)
    assert after1.latency_s == pytest.approx(1.5)

    ev2 = _event(
        RunEventType.USAGE_RECORDED,
        {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
         "cost_usd": 0.002, "latency_s": 0.5},
        seq=2,
    )
    after2 = project_run(after1, ev2)
    assert after2.prompt_tokens == 120
    assert after2.completion_tokens == 60
    assert after2.total_tokens == 180
    assert after2.cost_usd == pytest.approx(0.012)
    assert after2.latency_s == pytest.approx(2.0)


def test_usage_recorded_defaults_missing_fields_to_zero():
    run = _record()
    projected = project_run(run, _event(RunEventType.USAGE_RECORDED, {}))
    assert projected.prompt_tokens == 0
    assert projected.total_tokens == 0
    assert projected.cost_usd == 0.0


@pytest.mark.parametrize(
    "field", ["prompt_tokens", "completion_tokens", "total_tokens", "cost_usd", "latency_s"]
)
def test_usage_recorded_rejects_negative_values(field):
    run = _record()
    with pytest.raises(RunProjectionError):
        project_run(run, _event(RunEventType.USAGE_RECORDED, {field: -1}))


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "total_tokens"])
def test_usage_recorded_rejects_boolean_masquerading_as_integer(field):
    run = _record()
    with pytest.raises(RunProjectionError):
        project_run(run, _event(RunEventType.USAGE_RECORDED, {field: True}))


def test_usage_recorded_rejects_non_numeric_cost():
    run = _record()
    with pytest.raises(RunProjectionError):
        project_run(run, _event(RunEventType.USAGE_RECORDED, {"cost_usd": "free"}))


@pytest.mark.parametrize("field", ["cost_usd", "latency_s"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
def test_usage_recorded_rejects_non_finite_cost_and_latency(field, bad):
    """Projector, üst katman (event/payload) JSON doğrulamasından BAĞIMSIZ olarak
    NaN/Infinity'yi kendi savunması ile reddeder (bkz. _raw_event); asla sessizce
    kabul edip SQLite'a kadar sızmasına izin vermez."""
    run = _record()
    with pytest.raises(RunProjectionError):
        project_run(run, _raw_event(RunEventType.USAGE_RECORDED, {field: bad}))


def test_unknown_event_type_leaves_run_completely_unchanged():
    run = _record(status=RunStatus.RUNNING, phase=RunPhase.EXECUTING, prompt_tokens=5)
    ev = _event("execution.output", {"chunk": "hello"})
    projected = project_run(run, ev)
    assert projected == run  # last_event_seq de dahil hiçbir alan projector tarafından değişmez


def test_project_run_does_not_mutate_current_record():
    run = _record(status=RunStatus.RUNNING, phase=RunPhase.EXECUTING)
    project_run(run, _event(RunEventType.RUN_COMPLETED))
    assert run.status == RunStatus.RUNNING  # orijinal kayıt değişmeden kalır
    assert run.phase == RunPhase.EXECUTING


def test_projector_never_touches_last_event_seq():
    """last_event_seq'i ilerletmek yalnızca kalıcılık katmanının (RunStore) sorumluluğudur."""
    run = _record(last_event_seq=7)
    projected = project_run(run, _event(RunEventType.RUN_STARTED, seq=8))
    assert projected.last_event_seq == 7
