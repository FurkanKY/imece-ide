"""run_runtime.legacy — project_runner legacy dict <-> kanonik event çevirisi testleri.

Gerçek geçici SQLite RunStore/RunRuntime kullanır (mock yok); tam kanonik
eşleme, worker sonlanma yerleşimi ve Apply/Reject akışlarını uçtan uca test eder.
"""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_runtime.errors import EventSequenceError, InvalidRunStateError  # noqa: E402
from run_runtime.events import RunEventType  # noqa: E402
from run_runtime.legacy import LegacyRunCoordinator  # noqa: E402
from run_runtime.models import RunPhase, RunStatus  # noqa: E402
from run_runtime.service import RunRuntime  # noqa: E402
from run_runtime.store import RunStore  # noqa: E402


@pytest.fixture
def runtime(tmp_path) -> RunRuntime:
    return RunRuntime(RunStore(tmp_path / "runtime.sqlite3"))


def _all_events(runtime, run_id):
    return runtime.store.events(run_id, limit=200).events


PLAN_STAGE = {"type": "stage", "stage": "plan", "provider": "claude"}
PLAN_METRIC = {"type": "metric", "stage": "plan", "provider": "claude", "model": "claude-3",
               "latency_s": 1.2, "tokens": 100, "cost_usd": 0.01}
PLAN_OUTPUT = {"type": "output", "stage": "plan", "text": "1. do x\nFILES:\n- a.py"}
PLAN_EVENT = {"type": "plan", "summary": "1. do x", "files": ["a.py"]}

CODE_STAGE = {"type": "stage", "stage": "code", "provider": "deepseek"}
CODE_METRIC = {"type": "metric", "stage": "code", "provider": "deepseek", "model": "deepseek-coder",
               "latency_s": 2.5, "tokens": 300, "cost_usd": 0.02}
DIFF_EVENT = {"type": "diff", "path": "a.py", "is_new": True, "diff": "+new content\n"}

REVIEW_STAGE = {"type": "stage", "stage": "review", "provider": "gemini"}
REVIEW_METRIC = {"type": "metric", "stage": "review", "provider": "gemini", "model": "gemini-pro",
                  "latency_s": 0.8, "tokens": 50, "cost_usd": 0.001}
REVIEW_OUTPUT = {"type": "output", "stage": "review", "text": "VERDICT: APPROVED"}
VERDICT_EVENT = {"type": "verdict", "verdict": "APPROVED", "note": "looks good"}

PROPOSAL_EVENT = {
    "type": "proposal",
    "proposals": [
        {"path": "a.py", "new": "new full content\nline2\n", "diff": "+new content\n", "is_new": True},
    ],
    "totals": {"cost_usd": 0.031, "latency_s": 4.5, "tokens": 450},
    "verdict": "APPROVED",
}


def _drive_successful_flow(coordinator):
    for ev in (
        PLAN_STAGE, PLAN_METRIC, PLAN_OUTPUT, PLAN_EVENT,
        CODE_STAGE, CODE_METRIC, DIFF_EVENT,
        REVIEW_STAGE, REVIEW_METRIC, REVIEW_OUTPUT, VERDICT_EVENT,
        PROPOSAL_EVENT,
    ):
        coordinator.handle_legacy_event(ev)


def test_full_successful_proposal_flow_produces_expected_semantic_order(runtime):
    coordinator = LegacyRunCoordinator.start(
        runtime, project_root="/tmp/proj", task="do X", routing={"planner": "custom"},
    )
    _drive_successful_flow(coordinator)

    events = _all_events(runtime, coordinator.run_id)
    types = [e.type for e in events]
    assert types == [
        "run.created", "run.started",
        "run.phase_changed", "execution.started", "usage.recorded", "execution.output", "plan.completed",
        "execution.completed", "run.phase_changed", "execution.started", "usage.recorded", "change.proposed",
        "execution.completed", "run.phase_changed", "execution.started", "usage.recorded",
        "execution.output", "review.completed",
        "execution.completed", "proposal.ready", "run.waiting_user",
    ]

    by_type = {}
    for e in events:
        by_type.setdefault(e.type, []).append(e)

    # exec_ ön eki
    for e in events:
        if e.execution_id is not None:
            assert e.execution_id.startswith("exec_")

    planner_started = by_type["execution.started"][0]
    worker_started = by_type["execution.started"][1]
    reviewer_started = by_type["execution.started"][2]
    assert planner_started.payload["role"] == "planner"
    assert worker_started.payload["role"] == "worker"
    assert worker_started.payload["legacy_role"] == "coder"
    assert reviewer_started.payload["role"] == "reviewer"

    # üç execution ID'si birbirinden FARKLI
    ids = {planner_started.execution_id, worker_started.execution_id, reviewer_started.execution_id}
    assert len(ids) == 3

    # her execution.completed, ilgili execution.started ile AYNI id'yi paylaşır
    completed = by_type["execution.completed"]
    assert completed[0].execution_id == planner_started.execution_id
    assert completed[1].execution_id == worker_started.execution_id
    assert completed[2].execution_id == reviewer_started.execution_id

    # stage event'leri (usage.recorded, execution.output, plan/change/review.completed)
    # ilgili execution ID'sini taşır
    assert by_type["usage.recorded"][0].execution_id == planner_started.execution_id
    assert by_type["usage.recorded"][1].execution_id == worker_started.execution_id
    assert by_type["usage.recorded"][2].execution_id == reviewer_started.execution_id
    assert by_type["plan.completed"][0].execution_id == planner_started.execution_id
    assert by_type["change.proposed"][0].execution_id == worker_started.execution_id
    assert by_type["review.completed"][0].execution_id == reviewer_started.execution_id

    # correlation_id == run_id her mapped event için
    for e in events:
        assert e.correlation_id == coordinator.run_id

    # tam proposal 'new' içeriği dayanıklı round-trip'te hayatta kalır
    proposal_ready = by_type["proposal.ready"][0]
    assert proposal_ready.payload["proposals"] == [
        {"path": "a.py", "new": "new full content\nline2\n", "diff": "+new content\n", "is_new": True},
    ]
    assert proposal_ready.payload["totals"] == {"cost_usd": 0.031, "latency_s": 4.5, "tokens": 450}
    assert proposal_ready.payload["verdict"] == "APPROVED"

    run = runtime.get_run(coordinator.run_id)
    assert run.status == RunStatus.WAITING_USER
    assert run.phase == RunPhase.READY


def test_original_legacy_event_dicts_are_not_mutated(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    originals = [copy.deepcopy(ev) for ev in (
        PLAN_STAGE, PLAN_METRIC, PLAN_OUTPUT, PLAN_EVENT,
        CODE_STAGE, CODE_METRIC, DIFF_EVENT,
        REVIEW_STAGE, REVIEW_METRIC, REVIEW_OUTPUT, VERDICT_EVENT, PROPOSAL_EVENT,
    )]
    live = [
        PLAN_STAGE, PLAN_METRIC, PLAN_OUTPUT, PLAN_EVENT,
        CODE_STAGE, CODE_METRIC, DIFF_EVENT,
        REVIEW_STAGE, REVIEW_METRIC, REVIEW_OUTPUT, VERDICT_EVENT, PROPOSAL_EVENT,
    ]
    for ev in live:
        coordinator.handle_legacy_event(ev)
    for original, ev in zip(originals, live):
        assert ev == original  # DEĞİŞMEDİ


def test_effective_routing_stored_even_with_partial_override(runtime):
    coordinator = LegacyRunCoordinator.start(
        runtime, project_root="/tmp/proj", task="do X", routing={"planner": "custom-model"},
    )
    run = runtime.get_run(coordinator.run_id)
    assert run.routing == {"planner": "custom-model", "coder": "deepseek", "reviewer": "gemini"}


def test_effective_routing_stored_with_no_override(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    run = runtime.get_run(coordinator.run_id)
    assert run.routing == {"planner": "claude", "coder": "deepseek", "reviewer": "gemini"}


def test_worker_finished_while_waiting_user_does_not_append_run_completed(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    _drive_successful_flow(coordinator)
    before_count = len(_all_events(runtime, coordinator.run_id))

    result = coordinator.finish_normal()

    assert result is None
    after = _all_events(runtime, coordinator.run_id)
    assert len(after) == before_count  # hiçbir yeni event eklenmedi
    run = runtime.get_run(coordinator.run_id)
    assert run.status == RunStatus.WAITING_USER
    assert run.phase == RunPhase.READY


def test_apply_settlement_projects_succeeded_applied_with_no_trailing_run_completed(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    _drive_successful_flow(coordinator)

    event, run = coordinator.record_proposal_applied(
        applied_paths=["a.py"], checkpoint_id="cp-123",
    )

    assert event.type == "proposal.applied"
    assert event.payload == {"applied": ["a.py"], "checkpoint_id": "cp-123"}
    assert run.status == RunStatus.SUCCEEDED
    assert run.phase == RunPhase.APPLIED

    types = [e.type for e in _all_events(runtime, coordinator.run_id)]
    assert types[-1] == "proposal.applied"
    assert "run.completed" not in types


def test_reject_settlement_projects_succeeded_rejected_with_no_trailing_run_completed(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    _drive_successful_flow(coordinator)

    event, run = coordinator.record_proposal_rejected(rejected_paths=["a.py"])

    assert event.type == "proposal.rejected"
    assert event.payload == {"rejected": ["a.py"]}
    assert run.status == RunStatus.SUCCEEDED
    assert run.phase == RunPhase.REJECTED

    types = [e.type for e in _all_events(runtime, coordinator.run_id)]
    assert types[-1] == "proposal.rejected"
    assert "run.completed" not in types


def test_no_proposal_path_does_not_wait_for_user_and_worker_finish_completes_run(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    coordinator.handle_legacy_event(PLAN_STAGE)
    coordinator.handle_legacy_event(PLAN_METRIC)
    coordinator.handle_legacy_event(PLAN_EVENT)
    coordinator.handle_legacy_event(CODE_STAGE)
    coordinator.handle_legacy_event(CODE_METRIC)
    # coder üretilebilir bir dosya bloğu vermedi: değişiklik yok, review atlanır.
    empty_proposal = {"type": "proposal", "proposals": [], "totals": {"cost_usd": 0.03, "latency_s": 3.7, "tokens": 400}, "verdict": None}
    coordinator.handle_legacy_event(empty_proposal)

    events = _all_events(runtime, coordinator.run_id)
    types = [e.type for e in events]
    assert "run.waiting_user" not in types
    proposal_ready = [e for e in events if e.type == "proposal.ready"][0]
    assert proposal_ready.payload["proposals"] == []

    run_before = runtime.get_run(coordinator.run_id)
    assert run_before.status == RunStatus.RUNNING

    event = coordinator.finish_normal()
    assert event is not None
    assert event.type == "run.completed"

    run = runtime.get_run(coordinator.run_id)
    assert run.status == RunStatus.SUCCEEDED
    assert run.phase == RunPhase.DONE


def test_worker_failure_settles_active_execution_and_projects_failed_error(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    coordinator.handle_legacy_event(PLAN_STAGE)  # execution açık kalır (planner)

    event = coordinator.finish_failed("provider timed out")

    events = _all_events(runtime, coordinator.run_id)
    types = [e.type for e in events]
    assert "execution.failed" in types
    assert types[-1] == "run.failed"
    assert event.payload["error_code"] == "legacy_worker_error"
    assert event.payload["error_message"] == "provider timed out"

    run = runtime.get_run(coordinator.run_id)
    assert run.status == RunStatus.FAILED
    assert run.phase == RunPhase.ERROR
    assert run.error_code == "legacy_worker_error"
    assert run.error_message == "provider timed out"


def test_worker_cancellation_settles_active_execution_and_projects_cancelled_done(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    coordinator.handle_legacy_event(PLAN_STAGE)

    event = coordinator.finish_cancelled()

    events = _all_events(runtime, coordinator.run_id)
    types = [e.type for e in events]
    assert "execution.failed" in types
    assert types[-1] == "run.cancelled"
    assert event.type == "run.cancelled"

    run = runtime.get_run(coordinator.run_id)
    assert run.status == RunStatus.CANCELLED
    assert run.phase == RunPhase.DONE


def test_unknown_legacy_event_is_persisted_forward_compatibly(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    before = runtime.get_run(coordinator.run_id)

    unknown = {"type": "future_thing", "some_field": 42, "nested": {"a": [1, 2, 3]}}
    coordinator.handle_legacy_event(unknown)

    events = _all_events(runtime, coordinator.run_id)
    last = events[-1]
    assert last.type == "legacy.event"
    assert last.payload == {"event": unknown}

    after = runtime.get_run(coordinator.run_id)
    # projeksiyon (last_event_seq hariç) değişmedi — bilinmeyen event türü
    # projector'da bir no-op'tur.
    assert after.status == before.status
    assert after.phase == before.phase
    assert after.last_event_seq == before.last_event_seq + 1


# ==================== 1A/1B: adapter durumu YALNIZCA dayanıklı commit'ten SONRA ilerler ====================


def test_partial_multi_spec_translation_failure_leaves_adapter_state_consistent(runtime, monkeypatch):
    """stage=code çevirisi 3 spec üretir: execution.completed(E1), run.phase_changed,
    execution.started(E2). 2. spec'in kalıcılığını zorla başarısız kılarız:
    yalnızca 1. spec (E1'i kapatan) commit olmalı; adapter E2'yi HİÇ açık
    saymamalı (E2 asla başlamadı)."""
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    coordinator.handle_legacy_event(PLAN_STAGE)
    planner_execution_id = coordinator._adapter.current_execution_id
    assert planner_execution_id is not None

    original_record = runtime.record
    call_count = {"n": 0}

    def flaky_record(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated failure on spec #2 (run.phase_changed)")
        return original_record(*args, **kwargs)

    monkeypatch.setattr(runtime, "record", flaky_record)

    with pytest.raises(RuntimeError):
        coordinator.handle_legacy_event(CODE_STAGE)

    # Yalnızca 1. spec (execution.completed(E1)) gerçekten commit oldu.
    events = _all_events(runtime, coordinator.run_id)
    types = [e.type for e in events]
    assert types == [
        "run.created", "run.started", "run.phase_changed", "execution.started",
        "execution.completed",
    ]
    assert events[-1].execution_id == planner_execution_id

    # Adapter durumu: E1 kapandı, E2 asla başlamadığından hiçbir execution AÇIK DEĞİL.
    assert coordinator._adapter.current_execution_id is None

    # finish_failed, HİÇ başlamamış E2 için bir execution.failed EKLEMEMELİ.
    coordinator.finish_failed("worker crashed mid-transition")
    events2 = _all_events(runtime, coordinator.run_id)
    types2 = [e.type for e in events2]
    assert types2[len(types):] == ["run.failed"]  # execution.failed YOK, doğrudan run.failed
    run = runtime.get_run(coordinator.run_id)
    assert run.status == RunStatus.FAILED
    assert run.phase == RunPhase.ERROR


def test_execution_failed_persistence_failure_does_not_clear_adapter_state(runtime, monkeypatch):
    """execution.failed'ın KENDİSİNİN kalıcılığı başarısız olursa, adapter açık
    execution'ı SESSİZCE TEMİZLEMEMELİDİR — dayanıklı gerçeklik, execution'ın
    HENÜZ yerleşmediğidir (settled değildir)."""
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    coordinator.handle_legacy_event(PLAN_STAGE)
    execution_id = coordinator._adapter.current_execution_id
    assert execution_id is not None

    original_record = runtime.record

    def failing_execution_failed(*args, **kwargs):
        if kwargs.get("type") == RunEventType.EXECUTION_FAILED:
            raise RuntimeError("simulated execution.failed persistence failure")
        return original_record(*args, **kwargs)

    monkeypatch.setattr(runtime, "record", failing_execution_failed)

    with pytest.raises(RuntimeError):
        coordinator.finish_failed("worker crashed")

    # Adapter, execution.failed HİÇ commit olmadığı için açık execution'ı
    # SESSİZCE TEMİZLEMEMİŞ olmalı.
    assert coordinator._adapter.current_execution_id == execution_id

    # run.failed de HENÜZ eklenmemiş olmalı (execution.failed başarısız
    # olduğunda finish_failed erken/istisna ile çıkar).
    events = _all_events(runtime, coordinator.run_id)
    assert events[-1].type == "execution.started"
    run = runtime.get_run(coordinator.run_id)
    assert run.status == RunStatus.RUNNING


def test_original_legacy_dicts_not_mutated_even_under_partial_failure(runtime, monkeypatch):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    coordinator.handle_legacy_event(PLAN_STAGE)
    original = copy.deepcopy(CODE_STAGE)

    original_record = runtime.record
    call_count = {"n": 0}

    def flaky_record(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("boom")
        return original_record(*args, **kwargs)

    monkeypatch.setattr(runtime, "record", flaky_record)

    with pytest.raises(RuntimeError):
        coordinator.handle_legacy_event(CODE_STAGE)

    assert CODE_STAGE == original


# ==================== 8: terminal yerleşim yöntemleri İDEMPOTENTTİR ====================


def test_finish_normal_is_idempotent_after_success(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    coordinator.handle_legacy_event(PLAN_STAGE)
    coordinator.handle_legacy_event(PLAN_METRIC)
    coordinator.handle_legacy_event(PLAN_EVENT)
    coordinator.handle_legacy_event(CODE_STAGE)
    coordinator.handle_legacy_event(CODE_METRIC)
    empty_proposal = {"type": "proposal", "proposals": [], "totals": {}, "verdict": None}
    coordinator.handle_legacy_event(empty_proposal)

    first = coordinator.finish_normal()
    assert first is not None
    before_count = len(_all_events(runtime, coordinator.run_id))

    second = coordinator.finish_normal()
    assert second is None
    assert len(_all_events(runtime, coordinator.run_id)) == before_count


def test_finish_failed_is_idempotent_after_success(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    coordinator.handle_legacy_event(PLAN_STAGE)

    first = coordinator.finish_failed("boom")
    assert first is not None
    before_count = len(_all_events(runtime, coordinator.run_id))

    second = coordinator.finish_failed("boom again")
    assert second is None
    assert len(_all_events(runtime, coordinator.run_id)) == before_count


def test_finish_cancelled_is_idempotent_after_success(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    coordinator.handle_legacy_event(PLAN_STAGE)

    first = coordinator.finish_cancelled()
    assert first is not None
    before_count = len(_all_events(runtime, coordinator.run_id))

    second = coordinator.finish_cancelled()
    assert second is None
    assert len(_all_events(runtime, coordinator.run_id)) == before_count


def test_terminal_status_is_never_reverted_by_a_different_terminal_method(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    coordinator.handle_legacy_event(PLAN_STAGE)
    coordinator.finish_failed("boom")
    before_count = len(_all_events(runtime, coordinator.run_id))

    result = coordinator.finish_cancelled()

    assert result is None
    assert len(_all_events(runtime, coordinator.run_id)) == before_count
    run = runtime.get_run(coordinator.run_id)
    assert run.status == RunStatus.FAILED  # CANCELLED'a GERİ DÖNÜŞTÜRÜLMEDİ


def test_finish_normal_remains_noop_for_waiting_user(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    _drive_successful_flow(coordinator)
    before_count = len(_all_events(runtime, coordinator.run_id))

    result = coordinator.finish_normal()

    assert result is None
    assert len(_all_events(runtime, coordinator.run_id)) == before_count
    run = runtime.get_run(coordinator.run_id)
    assert run.status == RunStatus.WAITING_USER


# ==================== 5: Apply/Reject WAITING_USER + iyimser sıra ön koşulları ====================


def test_apply_from_running_is_rejected_with_zero_new_events(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    coordinator.handle_legacy_event(PLAN_STAGE)  # hâlâ RUNNING
    before_count = len(_all_events(runtime, coordinator.run_id))

    with pytest.raises(InvalidRunStateError):
        coordinator.record_proposal_applied(applied_paths=["a.py"], checkpoint_id="cp-1")

    assert len(_all_events(runtime, coordinator.run_id)) == before_count
    assert runtime.get_run(coordinator.run_id).status == RunStatus.RUNNING


def test_reject_from_running_is_rejected_with_zero_new_events(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    coordinator.handle_legacy_event(PLAN_STAGE)
    before_count = len(_all_events(runtime, coordinator.run_id))

    with pytest.raises(InvalidRunStateError):
        coordinator.record_proposal_rejected(rejected_paths=["a.py"])

    assert len(_all_events(runtime, coordinator.run_id)) == before_count
    assert runtime.get_run(coordinator.run_id).status == RunStatus.RUNNING


def test_apply_twice_second_call_rejected_event_count_unchanged(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    _drive_successful_flow(coordinator)
    coordinator.record_proposal_applied(applied_paths=["a.py"], checkpoint_id="cp-1")
    before_count = len(_all_events(runtime, coordinator.run_id))

    with pytest.raises(InvalidRunStateError):
        coordinator.record_proposal_applied(applied_paths=["a.py"], checkpoint_id="cp-2")

    assert len(_all_events(runtime, coordinator.run_id)) == before_count
    assert runtime.get_run(coordinator.run_id).status == RunStatus.SUCCEEDED


def test_reject_twice_second_call_rejected(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    _drive_successful_flow(coordinator)
    coordinator.record_proposal_rejected(rejected_paths=["a.py"])
    before_count = len(_all_events(runtime, coordinator.run_id))

    with pytest.raises(InvalidRunStateError):
        coordinator.record_proposal_rejected(rejected_paths=["a.py"])

    assert len(_all_events(runtime, coordinator.run_id)) == before_count


def test_apply_then_reject_is_rejected(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    _drive_successful_flow(coordinator)
    coordinator.record_proposal_applied(applied_paths=["a.py"], checkpoint_id="cp-1")

    with pytest.raises(InvalidRunStateError):
        coordinator.record_proposal_rejected(rejected_paths=["a.py"])


def test_reject_then_apply_is_rejected(runtime):
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    _drive_successful_flow(coordinator)
    coordinator.record_proposal_rejected(rejected_paths=["a.py"])

    with pytest.raises(InvalidRunStateError):
        coordinator.record_proposal_applied(applied_paths=["a.py"], checkpoint_id="cp-1")


def test_two_decisions_from_same_observed_seq_exactly_one_settles(runtime):
    """Aynı gözlenen last_event_seq'ten başlayan iki 'karar' (Apply/Reject),
    mevcut expected_last_event_seq makinesiyle YARIŞ korumasına sahiptir:
    ikisi de aynı anlık görüntüden hareket etse bile yalnızca BİRİ yerleşebilir."""
    coordinator = LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")
    _drive_successful_flow(coordinator)
    observed = runtime.get_run(coordinator.run_id)
    assert observed.status == RunStatus.WAITING_USER

    event1, run1 = runtime.record(
        run_id=coordinator.run_id, type=RunEventType.PROPOSAL_APPLIED,
        payload={"applied": ["a.py"], "checkpoint_id": "cp-1"},
        source="webhost.run", correlation_id=coordinator.run_id,
        expected_last_event_seq=observed.last_event_seq,
    )
    assert run1.status == RunStatus.SUCCEEDED
    assert run1.phase == RunPhase.APPLIED

    with pytest.raises(EventSequenceError):
        runtime.record(
            run_id=coordinator.run_id, type=RunEventType.PROPOSAL_REJECTED,
            payload={"rejected": ["a.py"]},
            source="webhost.run", correlation_id=coordinator.run_id,
            expected_last_event_seq=observed.last_event_seq,  # artık BAYAT
        )

    final = runtime.get_run(coordinator.run_id)
    assert final.status == RunStatus.SUCCEEDED
    assert final.phase == RunPhase.APPLIED  # REJECTED'e geçmedi


# ==================== 3: LegacyRunCoordinator.start başlangıç sertleştirmesi ====================


def test_start_lifecycle_failure_settles_run_failed_and_reraises(runtime, monkeypatch):
    """run.created/run.started kalıcılığı başarısız olursa: en iyi çaba bir
    run.failed yerleştirilir VE orijinal istisna AYNEN yukarı fırlatılır —
    kanonik geçmiş SİLİNMEZ."""
    original_create_run = runtime.create_run

    captured_run_id = {}

    def spy_create_run(*args, **kwargs):
        record = original_create_run(*args, **kwargs)
        captured_run_id["id"] = record.run_id
        return record

    monkeypatch.setattr(runtime, "create_run", spy_create_run)

    original_record = runtime.record

    def failing_run_created(*args, **kwargs):
        if kwargs.get("type") == RunEventType.RUN_CREATED:
            raise RuntimeError("simulated run.created persistence failure")
        return original_record(*args, **kwargs)

    monkeypatch.setattr(runtime, "record", failing_run_created)

    with pytest.raises(RuntimeError):
        LegacyRunCoordinator.start(runtime, project_root="/tmp/proj", task="do X")

    run_id = captured_run_id["id"]
    run = runtime.get_run(run_id)  # Task/Run kalıcılığı SİLİNMEDİ
    assert run.status == RunStatus.FAILED
    assert run.error_code == "legacy_lifecycle_start_failed"
