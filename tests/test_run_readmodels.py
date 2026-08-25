"""run_runtime.readmodels — kanonik Receipt/HistoryItem izdüşümü testleri.

Gerçek geçici SQLite RunStore kullanır (mock yok).
"""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_runtime.errors import RunNotFoundError, RunStoreError  # noqa: E402
from run_runtime.events import RunEventType  # noqa: E402
from run_runtime.legacy import LegacyRunCoordinator  # noqa: E402
from run_runtime.readmodels import (  # noqa: E402
    HISTORY_MAX_ITEMS,
    RunReadService,
    RunReadSnapshot,
    build_history_item,
    build_receipt,
    canonical_status_string,
    load_full_event_history,
    merge_canonical_and_legacy_history,
)
from run_runtime.events import RunEventSpec  # noqa: E402
from run_runtime.service import RunRuntime  # noqa: E402
from run_runtime.store import RunStore  # noqa: E402


@pytest.fixture
def runtime(tmp_path) -> RunRuntime:
    return RunRuntime(RunStore(tmp_path / "runtime.sqlite3"))


@pytest.fixture
def read_service(runtime) -> RunReadService:
    return RunReadService(runtime.store)


PROJECT_ROOT = "/tmp/proj"

PLAN_STAGE = {"type": "stage", "stage": "plan", "provider": "claude"}
PLAN_METRIC = {"type": "metric", "stage": "plan", "provider": "claude", "model": "claude-3",
               "latency_s": 1.2, "tokens": 100, "cost_usd": 0.01}
PLAN_EVENT = {"type": "plan", "summary": "1. do x", "files": ["a.py"]}
CODE_STAGE = {"type": "stage", "stage": "code", "provider": "deepseek"}
CODE_METRIC = {"type": "metric", "stage": "code", "provider": "deepseek", "model": "deepseek-coder",
               "latency_s": 2.5, "tokens": 300, "cost_usd": 0.02}
DIFF_EVENT = {"type": "diff", "path": "a.py", "is_new": True, "diff": "+new content\n"}
REVIEW_STAGE = {"type": "stage", "stage": "review", "provider": "gemini"}
REVIEW_METRIC = {"type": "metric", "stage": "review", "provider": "gemini", "model": "gemini-pro",
                  "latency_s": 0.8, "tokens": 50, "cost_usd": 0.001}
VERDICT_EVENT = {"type": "verdict", "verdict": "APPROVED", "note": "looks good"}
PROPOSAL_EVENT = {
    "type": "proposal",
    "proposals": [
        {"path": "a.py", "new": "full new content\nline2\n", "diff": "+new content\n", "is_new": True},
    ],
    "totals": {"cost_usd": 0.031, "latency_s": 4.5, "tokens": 450},
    "verdict": "APPROVED",
}


def _start(runtime, *, task="do X", routing=None):
    return LegacyRunCoordinator.start(runtime, project_root=PROJECT_ROOT, task=task, routing=routing)


def _drive_successful_flow(coordinator):
    for ev in (
        PLAN_STAGE, PLAN_METRIC, PLAN_EVENT,
        CODE_STAGE, CODE_METRIC, DIFF_EVENT,
        REVIEW_STAGE, REVIEW_METRIC, VERDICT_EVENT,
        PROPOSAL_EVENT,
    ):
        coordinator.handle_legacy_event(ev)


# ==================== 1-5: temel Receipt durum eşlemeleri ====================


def test_waiting_user_run_receipt_fields(runtime, read_service):
    coordinator = _start(runtime, routing={"planner": "custom"})
    _drive_successful_flow(coordinator)

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)

    assert receipt["id"] == coordinator.run_id
    assert receipt["id"].startswith("run_")
    assert receipt["status"] == "proposed"
    assert receipt["task"] == "do X"
    assert receipt["routing"] == {"planner": "custom", "coder": "deepseek", "reviewer": "gemini"}
    assert receipt["plan"] == {"summary": "1. do x", "files": ["a.py"]}
    assert receipt["review"] == {"verdict": "APPROVED", "note": "looks good"}
    assert receipt["proposals"] == [{"path": "a.py", "is_new": True, "diff": "+new content\n"}]
    assert "new" not in receipt["proposals"][0]  # tam içerik SIZMAZ
    assert receipt["applied"] == []
    assert receipt["rejected"] == []
    assert receipt["checkpointId"] is None
    assert receipt["metrics"]["tokens"] == 450  # RunRecord.total_tokens (usage.recorded toplamı)
    assert receipt["metrics"]["cost_usd"] == pytest.approx(0.031)
    assert receipt["metrics"]["latency_s"] == pytest.approx(4.5)
    assert receipt["verification"] == {
        "status": "not_run", "detail": "Bu koşuda doğrulama komutu çalıştırılmadı.",
    }
    assert isinstance(receipt["createdAt"], float)
    assert isinstance(receipt["finishedAt"], float)
    assert "error" not in receipt


def test_applied_run_receipt_status_and_fields(runtime, read_service):
    coordinator = _start(runtime)
    _drive_successful_flow(coordinator)
    coordinator.record_proposal_applied(applied_paths=["a.py"], checkpoint_id="cp-1")

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)

    assert receipt["status"] == "applied"
    assert receipt["applied"] == ["a.py"]
    assert receipt["checkpointId"] == "cp-1"
    assert receipt["rejected"] == []


def test_rejected_run_receipt_status_and_fields(runtime, read_service):
    coordinator = _start(runtime)
    _drive_successful_flow(coordinator)
    coordinator.record_proposal_rejected(rejected_paths=["a.py"])

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)

    assert receipt["status"] == "rejected"
    assert receipt["rejected"] == ["a.py"]
    assert receipt["applied"] == []


def test_failed_run_receipt_status_and_error(runtime, read_service):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)
    coordinator.finish_failed("provider timed out")

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)

    assert receipt["status"] == "failed"
    assert receipt["error"] == "provider timed out"


def test_cancelled_run_receipt_status(runtime, read_service):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)
    coordinator.finish_cancelled()

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)

    assert receipt["status"] == "cancelled"


def test_active_running_run_produces_a_read_model(runtime, read_service):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)

    assert receipt["status"] == "running"
    assert receipt["plan"] is None
    assert receipt["proposals"] == []


def test_created_status_string_before_run_started_conceptually():
    from run_runtime.models import RunPhase, RunRecord, RunStatus

    run = RunRecord.new(run_id="run_x", task_id="task_x")
    assert run.status == RunStatus.CREATED
    assert canonical_status_string(run) == "created"
    assert run.phase == RunPhase.CREATED


# ==================== 6-9: latest-wins semantics ====================


def test_latest_plan_completed_wins(runtime, read_service):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)
    coordinator.handle_legacy_event({"type": "plan", "summary": "first plan", "files": ["a.py"]})
    coordinator.handle_legacy_event({"type": "plan", "summary": "revised plan", "files": ["a.py", "b.py"]})

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)
    assert receipt["plan"] == {"summary": "revised plan", "files": ["a.py", "b.py"]}


def test_latest_review_completed_wins(runtime, read_service):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)
    coordinator.handle_legacy_event({"type": "verdict", "verdict": "NEEDS_FIX", "note": "first pass"})
    coordinator.handle_legacy_event({"type": "verdict", "verdict": "APPROVED", "note": "second pass"})

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)
    assert receipt["review"] == {"verdict": "APPROVED", "note": "second pass"}


def test_latest_proposal_ready_wins(runtime, read_service):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)
    first_proposal = {
        "type": "proposal",
        "proposals": [{"path": "a.py", "new": "v1", "diff": "diff-v1", "is_new": True}],
        "totals": {}, "verdict": None,
    }
    coordinator.handle_legacy_event(first_proposal)
    # yeniden dene: yeni bir proposal.ready yeniden ele alınmış gibi davranır
    second_proposal = {
        "type": "proposal",
        "proposals": [{"path": "a.py", "new": "v2", "diff": "diff-v2", "is_new": True}],
        "totals": {}, "verdict": "APPROVED",
    }
    coordinator.handle_legacy_event(second_proposal)

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)
    assert receipt["proposals"] == [{"path": "a.py", "is_new": True, "diff": "diff-v2"}]


def test_proposal_ready_strips_new_content_from_receipt(runtime, read_service):
    coordinator = _start(runtime)
    _drive_successful_flow(coordinator)

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)
    for proposal in receipt["proposals"]:
        assert set(proposal.keys()) == {"path", "is_new", "diff"}

    # Ancak tam 'new' içeriği kanonik run_events'te (durable event stream)
    # HÂLÂ mevcuttur — yalnızca Receipt izdüşümünden çıkarılmıştır.
    events = load_full_event_history(runtime.store, coordinator.run_id)
    proposal_ready = [e for e in events if e.type == "proposal.ready"][0]
    assert proposal_ready.payload["proposals"][0]["new"] == "full new content\nline2\n"


def test_draft_change_proposed_fallback_before_proposal_ready(runtime, read_service):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)
    coordinator.handle_legacy_event(CODE_STAGE)
    coordinator.handle_legacy_event({"type": "diff", "path": "a.py", "is_new": True, "diff": "diff-a-v1"})
    coordinator.handle_legacy_event({"type": "diff", "path": "b.py", "is_new": False, "diff": "diff-b"})
    coordinator.handle_legacy_event({"type": "diff", "path": "a.py", "is_new": True, "diff": "diff-a-v2"})

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)
    # a.py TEKRARLANMAZ; en son hâli (diff-a-v2) kazanır; ilk-görülme sırası korunur.
    assert receipt["proposals"] == [
        {"path": "a.py", "is_new": True, "diff": "diff-a-v2"},
        {"path": "b.py", "is_new": False, "diff": "diff-b"},
    ]


# ==================== 12-14: metrikler, bilinmeyen event'ler, sayfalama ====================


def test_run_record_metrics_used_correctly(runtime, read_service):
    coordinator = _start(runtime)
    _drive_successful_flow(coordinator)

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)
    # PLAN_METRIC(100) + CODE_METRIC(300) + REVIEW_METRIC(50) = 450
    assert receipt["metrics"]["tokens"] == 100 + 300 + 50
    assert receipt["metrics"]["cost_usd"] == pytest.approx(0.01 + 0.02 + 0.001)
    assert receipt["metrics"]["latency_s"] == pytest.approx(1.2 + 2.5 + 0.8)


def test_unknown_durable_events_do_not_break_projection(runtime, read_service):
    coordinator = _start(runtime)
    _drive_successful_flow(coordinator)
    coordinator.handle_legacy_event({"type": "future_thing", "some_field": 42})

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)
    assert receipt["status"] == "proposed"
    assert receipt["plan"] == {"summary": "1. do x", "files": ["a.py"]}


def test_more_than_100_durable_events_fully_paged_late_page_event_seen(runtime, read_service):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)
    for i in range(120):
        coordinator.handle_legacy_event(
            {"type": "metric", "stage": "plan", "provider": "claude", "model": "claude-3",
             "latency_s": 0.0, "tokens": 1, "cost_usd": 0.0}
        )
    # Son sayfada (küçük bir page_size ile ZORLANMIŞ sayfalamada) görünecek
    # bir semantic event (plan.completed) ekle.
    coordinator.handle_legacy_event({"type": "plan", "summary": "late plan", "files": ["z.py"]})

    events = load_full_event_history(runtime.store, coordinator.run_id, page_size=10)
    assert len(events) > 100
    assert events[-1].type == "plan.completed"
    assert [e.seq for e in events] == list(range(1, len(events) + 1))  # boşluksuz, artan

    receipt = read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)
    assert receipt["plan"] == {"summary": "late plan", "files": ["z.py"]}
    assert receipt["metrics"]["tokens"] == 120  # tüm sayfalardaki usage.recorded toplandı


def test_load_full_event_history_ordering_is_seq_based(runtime):
    coordinator = _start(runtime)
    for _ in range(15):
        coordinator.handle_legacy_event(
            {"type": "metric", "stage": None, "provider": "x", "model": "y",
             "latency_s": 0.1, "tokens": 1, "cost_usd": 0.0}
        )
    events = load_full_event_history(runtime.store, coordinator.run_id, page_size=4)
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert seqs == list(range(1, len(events) + 1))


# ==================== 15-16: proje izolasyonu ====================


def test_canonical_receipt_refuses_different_project_root(runtime, read_service):
    coordinator = _start(runtime)
    _drive_successful_flow(coordinator)

    with pytest.raises(RunNotFoundError):
        read_service.get_receipt(coordinator.run_id, project_root="/tmp/other-project")


def test_history_list_is_project_scoped(runtime, read_service):
    coordinator_a = LegacyRunCoordinator.start(runtime, project_root="/tmp/project-a", task="task a")
    coordinator_a.handle_legacy_event(PLAN_STAGE)
    coordinator_b = LegacyRunCoordinator.start(runtime, project_root="/tmp/project-b", task="task b")
    coordinator_b.handle_legacy_event(PLAN_STAGE)

    items_a = read_service.list_history(project_root="/tmp/project-a")
    assert [i["receipt_id"] for i in items_a] == [coordinator_a.run_id]

    items_b = read_service.list_history(project_root="/tmp/project-b")
    assert [i["receipt_id"] for i in items_b] == [coordinator_b.run_id]


def test_history_list_newest_first(runtime, read_service):
    import time as _time

    coordinator1 = _start(runtime, task="first")
    _time.sleep(0.01)
    coordinator2 = _start(runtime, task="second")

    items = read_service.list_history(project_root=PROJECT_ROOT)
    assert [i["receipt_id"] for i in items] == [coordinator2.run_id, coordinator1.run_id]


def test_history_item_receipt_id_equals_run_id_and_includes_active_runs(runtime, read_service):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)  # hâlâ RUNNING, terminal DEĞİL

    items = read_service.list_history(project_root=PROJECT_ROOT)
    assert len(items) == 1
    assert items[0]["receipt_id"] == coordinator.run_id
    assert items[0]["status"] == "running"  # aktif/takılı koşular geçmişten DIŞLANMAZ


# ==================== 19: event payload'ları mutasyona uğratılmaz ====================


def test_no_mutation_of_event_payloads_during_receipt_construction(runtime, read_service):
    coordinator = _start(runtime)
    _drive_successful_flow(coordinator)
    before = load_full_event_history(runtime.store, coordinator.run_id)
    before_copy = [copy.deepcopy(e.payload) for e in before]

    read_service.get_receipt(coordinator.run_id, project_root=PROJECT_ROOT)
    read_service.list_history(project_root=PROJECT_ROOT)

    after = load_full_event_history(runtime.store, coordinator.run_id)
    for a, b in zip(after, before_copy):
        assert a.payload == b


# ==================== deterministik legacy/canonical geçmiş birleştirme ====================


def test_merge_no_canonical_returns_legacy():
    legacy = [{"ts": 100, "task": "old"}, {"ts": 90, "task": "older"}]
    merged = merge_canonical_and_legacy_history([], legacy, limit=100)
    assert merged == legacy


def test_merge_full_canonical_page_excludes_legacy():
    canonical = [{"ts": 100 - i, "receipt_id": f"run_{i}"} for i in range(100)]
    legacy = [{"ts": 5, "task": "ancient"}]
    merged = merge_canonical_and_legacy_history(canonical, legacy, limit=100)
    assert merged == canonical
    assert len(merged) == 100


def test_merge_canonical_plus_older_legacy():
    canonical = [{"ts": 200, "receipt_id": "run_new"}]
    legacy = [{"ts": 150, "task": "mid"}, {"ts": 50, "task": "old"}]
    merged = merge_canonical_and_legacy_history(canonical, legacy, limit=100)
    assert merged == [
        {"ts": 200, "receipt_id": "run_new"},
        {"ts": 150, "task": "mid"},
        {"ts": 50, "task": "old"},
    ]


def test_merge_legacy_at_or_after_cutoff_excluded():
    canonical = [{"ts": 100, "receipt_id": "run_a"}]
    legacy = [
        {"ts": 100, "task": "same-instant-as-cutoff"},  # ts == cutoff -> DIŞLANIR (KESİNLİKLE daha eski değil)
        {"ts": 150, "task": "newer-than-cutoff"},        # ts > cutoff -> DIŞLANIR
        {"ts": 50, "task": "older-than-cutoff"},         # ts < cutoff -> DAHİL
    ]
    merged = merge_canonical_and_legacy_history(canonical, legacy, limit=100)
    assert merged == [
        {"ts": 100, "receipt_id": "run_a"},
        {"ts": 50, "task": "older-than-cutoff"},
    ]


def test_merge_result_capped_at_limit_and_newest_first():
    canonical = [{"ts": 100, "receipt_id": "run_a"}]
    legacy = [{"ts": 90 - i, "task": f"legacy-{i}"} for i in range(10)]
    merged = merge_canonical_and_legacy_history(canonical, legacy, limit=5)
    assert len(merged) == 5
    ts_values = [item["ts"] for item in merged]
    assert ts_values == sorted(ts_values, reverse=True)
    assert merged[0] == {"ts": 100, "receipt_id": "run_a"}


# ==================== hardening: through_seq boundary ====================


def test_through_seq_zero_returns_empty_history(runtime):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)
    events = load_full_event_history(runtime.store, coordinator.run_id, through_seq=0)
    assert events == ()


def test_through_seq_n_returns_exactly_1_to_n_even_when_more_events_exist(runtime):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)  # run.created, run.started, phase_changed, execution.started = seq 1..4
    coordinator.handle_legacy_event(PLAN_METRIC)  # seq 5
    coordinator.handle_legacy_event(PLAN_EVENT)  # seq 6
    coordinator.handle_legacy_event(CODE_STAGE)  # more events beyond N

    events = load_full_event_history(runtime.store, coordinator.run_id, through_seq=6, page_size=2)
    assert [e.seq for e in events] == [1, 2, 3, 4, 5, 6]
    assert events[-1].type == "plan.completed"


def test_through_seq_beyond_durable_history_raises_run_store_error(runtime):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)
    current = runtime.get_run(coordinator.run_id)
    with pytest.raises(RunStoreError):
        load_full_event_history(
            runtime.store, coordinator.run_id, through_seq=current.last_event_seq + 1000,
        )


@pytest.mark.parametrize("bad_through_seq", [-1, True, "3"])
def test_through_seq_input_validation(runtime, bad_through_seq):
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)
    with pytest.raises(RunStoreError):
        load_full_event_history(runtime.store, coordinator.run_id, through_seq=bad_through_seq)


# ==================== hardening: consistent snapshot under concurrent append ====================


def test_concurrent_append_cannot_contaminate_older_run_record_snapshot(runtime, monkeypatch):
    """RunReadService.load_snapshot, RunRecord'u okuduktan SONRA (ama event
    akışı okunmadan ÖNCE) başka bir 'thread' event eklese bile, o yeni
    event'ler snapshot'a SIZMAMALIDIR. Deterministik biçimde, store.get_run'ı
    (adapter/coordinator'ın DEĞİL, doğrudan RunStore'un) sarmalayıp, RunRecord
    okunduktan HEMEN SONRA ek bir event commit ederek yarışı simüle ederiz."""
    read_service = RunReadService(runtime.store)
    coordinator = _start(runtime)
    coordinator.handle_legacy_event(PLAN_STAGE)
    coordinator.handle_legacy_event({"type": "plan", "summary": "old plan", "files": ["a.py"]})

    original_get_run = runtime.store.get_run

    def racing_get_run(run_id):
        record = original_get_run(run_id)
        # RunRecord ZATEN okundu (last_event_seq burada sabitlendi); ŞİMDİ
        # başka bir 'yazıcı' yeni bir event commit eder.
        coordinator.handle_legacy_event({"type": "plan", "summary": "new plan", "files": ["b.py"]})
        return record

    monkeypatch.setattr(runtime.store, "get_run", racing_get_run)

    snapshot = read_service.load_snapshot(coordinator.run_id)

    # Snapshot, YARIŞTAN ÖNCEKİ (eski) last_event_seq ile SINIRLI kalmalı.
    assert snapshot.run.last_event_seq == snapshot.events[-1].seq
    receipt = build_receipt(snapshot)
    assert receipt["plan"] == {"summary": "old plan", "files": ["a.py"]}

    # Yarıştan SONRA taze bir okuma (artık patched olmayan store ile) yeni
    # planı görebilir — bu, sınırın YALNIZCA o ANLIK GÖRÜNTÜYE özgü olduğunu kanıtlar.
    monkeypatch.setattr(runtime.store, "get_run", original_get_run)
    fresh = read_service.load_snapshot(coordinator.run_id)
    fresh_receipt = build_receipt(fresh)
    assert fresh_receipt["plan"] == {"summary": "new plan", "files": ["b.py"]}


# ==================== hardening: project ownership BEFORE event loading ====================


def test_foreign_project_receipt_lookup_never_calls_store_events(runtime, monkeypatch):
    read_service = RunReadService(runtime.store)
    coordinator = _start(runtime)
    _drive_successful_flow(coordinator)

    calls = {"n": 0}
    original_events = runtime.store.events

    def spy_events(*args, **kwargs):
        calls["n"] += 1
        return original_events(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "events", spy_events)

    with pytest.raises(RunNotFoundError):
        read_service.get_receipt(coordinator.run_id, project_root="/tmp/other-project")

    assert calls["n"] == 0  # event akışı HİÇ okunmadı


# ==================== hardening: malformed legacy history entries ====================


@pytest.mark.parametrize(
    "malformed",
    [None, "text", {}, {"ts": "bad"}, {"ts": None}, [1, 2, 3], {"ts": True}],
    ids=["null", "string", "empty-dict", "string-ts", "none-ts", "list", "bool-ts"],
)
def test_malformed_legacy_history_entries_do_not_break_merge(malformed):
    canonical: list[dict] = []
    legacy = [malformed, {"ts": 42, "task": "valid legacy entry"}]
    merged = merge_canonical_and_legacy_history(canonical, legacy, limit=100)
    assert merged == [{"ts": 42, "task": "valid legacy entry"}]


def test_malformed_legacy_entries_excluded_alongside_valid_precanonical_merge():
    canonical = [{"ts": 100, "receipt_id": "run_a"}]
    legacy = [
        {"ts": 50, "task": "valid older"},
        {"ts": "bad", "task": "malformed"},
        None,
        {},
    ]
    merged = merge_canonical_and_legacy_history(canonical, legacy, limit=100)
    assert merged == [
        {"ts": 100, "receipt_id": "run_a"},
        {"ts": 50, "task": "valid older"},
    ]


def test_malformed_legacy_entries_not_mutated():
    malformed = {"ts": "bad", "task": "x"}
    original = dict(malformed)
    merge_canonical_and_legacy_history([], [malformed], limit=100)
    assert malformed == original


# ==================== native semantic Reviewer: latest-attempt read model ====================


def _seed_running_run(runtime, *, project_root=PROJECT_ROOT):
    task = runtime.create_task(project_root=project_root, prompt="review me")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return task, run


def _review_receipt_verdict(runtime, task, run):
    current = runtime.get_run(run.run_id)
    snapshot = RunReadSnapshot(
        task=task, run=current, events=runtime.events(run.run_id, limit=200).events,
    )
    receipt = build_receipt(snapshot)
    history_verdict = build_history_item(snapshot)["verdict"]
    assert receipt["review"]["verdict"] == history_verdict
    return receipt["review"]


def test_legacy_review_completed_without_review_started_still_works(runtime):
    task, run = _seed_running_run(runtime)
    runtime.record(
        run_id=run.run_id, type=RunEventType.REVIEW_COMPLETED,
        payload={"verdict": "APPROVED", "note": "legacy verdict"},
    )
    review = _review_receipt_verdict(runtime, task, run)
    assert review == {"verdict": "APPROVED", "note": "legacy verdict"}


def test_native_review_running_shows_unknown(runtime):
    task, run = _seed_running_run(runtime)
    runtime.record(
        run_id=run.run_id, type=RunEventType.REVIEW_STARTED, payload={"review_id": "rev-1"},
        correlation_id="rev-1", source="reviewer",
    )
    review = _review_receipt_verdict(runtime, task, run)
    assert review == {"verdict": "UNKNOWN", "note": "Review is running."}


def test_native_review_failed_shows_unknown(runtime):
    task, run = _seed_running_run(runtime)
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(RunEventType.REVIEW_STARTED, {"review_id": "rev-1"}, correlation_id="rev-1", source="reviewer"),
        RunEventSpec(RunEventType.REVIEW_FAILED, {"review_id": "rev-1", "error_type": "X", "error_message": "boom"}, correlation_id="rev-1", source="reviewer"),
    ))
    review = _review_receipt_verdict(runtime, task, run)
    assert review == {"verdict": "UNKNOWN", "note": "Review failed."}


def test_native_review_interrupted_shows_unknown(runtime):
    task, run = _seed_running_run(runtime)
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(RunEventType.REVIEW_STARTED, {"review_id": "rev-1"}, correlation_id="rev-1", source="reviewer"),
        RunEventSpec(RunEventType.REVIEW_INTERRUPTED, {"review_id": "rev-1", "reason": "process_restart"}, correlation_id="rev-1", source="reviewer"),
    ))
    review = _review_receipt_verdict(runtime, task, run)
    assert review == {"verdict": "UNKNOWN", "note": "Review was interrupted."}


def test_native_review_completed_shows_verdict_and_summary(runtime):
    task, run = _seed_running_run(runtime)
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(RunEventType.REVIEW_STARTED, {"review_id": "rev-1"}, correlation_id="rev-1", source="reviewer"),
        RunEventSpec(RunEventType.REVIEW_COMPLETED, {
            "review_id": "rev-1", "verdict": "NEEDS_FIX", "note": "Found a bug.", "summary": "Found a bug.",
            "findings": [{"severity": "major", "message": "m", "path": None, "start_line": None, "end_line": None}],
            "repository_fingerprint": "a" * 64, "diff_sha256": "b" * 64,
            "verification_id": None, "verification_status": None,
        }, correlation_id="rev-1", source="reviewer"),
    ))
    review = _review_receipt_verdict(runtime, task, run)
    assert review == {"verdict": "NEEDS_FIX", "note": "Found a bug."}


def test_stale_older_review_verdict_never_leaks_once_newer_attempt_started(runtime):
    task, run = _seed_running_run(runtime)
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(RunEventType.REVIEW_STARTED, {"review_id": "rev-1"}, correlation_id="rev-1", source="reviewer"),
        RunEventSpec(RunEventType.REVIEW_COMPLETED, {
            "review_id": "rev-1", "verdict": "APPROVED", "note": "old ok", "summary": "old ok",
            "findings": [], "repository_fingerprint": "a" * 64, "diff_sha256": "b" * 64,
            "verification_id": None, "verification_status": None,
        }, correlation_id="rev-1", source="reviewer"),
    ))
    runtime.record(
        run_id=run.run_id, type=RunEventType.REVIEW_STARTED, payload={"review_id": "rev-2"},
        correlation_id="rev-2", source="reviewer",
    )
    review = _review_receipt_verdict(runtime, task, run)
    assert review["verdict"] != "APPROVED"
    assert review == {"verdict": "UNKNOWN", "note": "Review is running."}
