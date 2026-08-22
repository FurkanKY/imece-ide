"""run_runtime.store.RunStore — SQLite entegrasyon testleri (tmp_path veritabanları)."""

import json
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_runtime import schema  # noqa: E402
from run_runtime.errors import (  # noqa: E402
    EventConflictError,
    EventSequenceError,
    EventValidationError,
    RunNotFoundError,
    RunProjectionError,
    RunStoreError,
    TaskNotFoundError,
)
from run_runtime.events import RunEventType  # noqa: E402
from run_runtime.store import MAX_EVENT_PAGE_LIMIT, RunStore  # noqa: E402


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "runtime.sqlite3"


@pytest.fixture
def store(db_path) -> RunStore:
    return RunStore(db_path)


def _seed_task_and_run(store: RunStore, *, task_id="task_seed", run_id="run_seed"):
    task = store.create_task(project_root="/tmp/proj", prompt="do X", task_id=task_id)
    run = store.create_run(task_id=task.task_id, run_id=run_id)
    return task, run


# ---------------- migration ----------------


def test_fresh_migration_reaches_version_1(db_path):
    RunStore(db_path).create_task(project_root="/tmp", prompt="p")
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"tasks", "runs", "run_events"} <= tables
    finally:
        conn.close()


def test_reopening_version_1_is_idempotent(db_path):
    RunStore(db_path).create_task(project_root="/tmp", prompt="p")
    # İkinci açılış (yeni bir RunStore/bağlantı) hatasız ve no-op olmalı.
    RunStore(db_path).create_task(project_root="/tmp", prompt="p2")
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert count == 2
    finally:
        conn.close()


def test_newer_unsupported_user_version_is_rejected(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        RunStore(db_path).create_task(project_root="/tmp", prompt="p")
    # Reddedilen açılış şemayı sessizce alt sürüme indirmemeli/üzerine yazmamalı.
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 999
    finally:
        conn.close()


def test_migrate_is_transactional_all_or_nothing(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        schema.configure_connection(conn)
        schema.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_migration_rolls_back_earlier_ddl_when_a_later_statement_fails(db_path):
    """Gerçek bir kısmi-migrasyon senaryosu: 'tasks' + index'i migrasyonun İLK
    adımlarıdır ve başarıyla oluşturulur; migrasyon 'runs' CREATE TABLE'a
    geldiğinde (önceden var olan, uyumsuz bir 'runs' tablosuyla çakışarak)
    başarısız olur. Tüm işlem (daha önce başarıyla oluşturulmuş 'tasks' dahil)
    geri alınmalı; önceden var olan uyumsuz 'runs' tablosu OLDUĞU GİBİ kalmalı."""
    conn = sqlite3.connect(str(db_path))
    try:
        schema.configure_connection(conn)
        conn.execute("CREATE TABLE runs (bogus_column TEXT)")
        conn.commit()

        with pytest.raises(sqlite3.OperationalError):
            schema.migrate(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "tasks" not in tables  # migrasyonun ilk adımı da geri alındı
        assert "runs" in tables  # önceden var olan tablo dokunulmadan kaldı
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        assert cols == {"bogus_column"}
    finally:
        conn.close()

    # Aynı bozuk DB üzerinden genel RunStore API'si de tipli RunStoreError vermeli.
    with pytest.raises(RunStoreError):
        RunStore(db_path).create_task(project_root="/tmp", prompt="p")


# ---------------- tasks ----------------


def test_task_create_and_get_round_trip(store):
    created = store.create_task(project_root="/tmp/proj", prompt="görevi yap")
    fetched = store.get_task(created.task_id)
    assert fetched == created


def test_get_missing_task_raises_typed_error(store):
    with pytest.raises(TaskNotFoundError):
        store.get_task("task_does_not_exist")


# ---------------- runs ----------------


def test_run_create_and_get_round_trip(store):
    task = store.create_task(project_root="/tmp/proj", prompt="p")
    created = store.create_run(task_id=task.task_id)
    fetched = store.get_run(created.run_id)
    assert fetched == created
    assert fetched.status.value == "created"
    assert fetched.phase.value == "created"
    assert fetched.last_event_seq == 0


def test_get_missing_run_raises_typed_error(store):
    with pytest.raises(RunNotFoundError):
        store.get_run("run_does_not_exist")


def test_create_run_for_missing_task_raises_typed_error(store):
    with pytest.raises(TaskNotFoundError):
        store.create_run(task_id="task_does_not_exist")


def test_routing_budget_workspace_snapshot_json_round_trip(store):
    task = store.create_task(project_root="/tmp/proj", prompt="p")
    routing = {"planner": "claude", "coder": "deepseek"}
    budget = {"max_usd": 1.5}
    snapshot = {"run_id": "wsrun", "snapshot_commit": "abc123"}
    run = store.create_run(
        task_id=task.task_id, routing=routing, budget=budget, workspace_snapshot=snapshot,
    )
    fetched = store.get_run(run.run_id)
    assert fetched.routing == routing
    assert fetched.budget == budget
    assert fetched.workspace_snapshot == snapshot


# ---------------- events: sequencing ----------------


def test_first_appended_event_has_seq_1(store):
    _, run = _seed_task_and_run(store)
    event, updated = store.append_event(run_id=run.run_id, type=RunEventType.RUN_CREATED, payload={})
    assert event.seq == 1
    assert updated.last_event_seq == 1


def test_sequential_appends_produce_strictly_increasing_seq(store):
    _, run = _seed_task_and_run(store)
    seqs = []
    for i in range(5):
        event, _ = store.append_event(
            run_id=run.run_id, type=RunEventType.USAGE_RECORDED, payload={"prompt_tokens": i},
        )
        seqs.append(event.seq)
    assert seqs == [1, 2, 3, 4, 5]


# ---------------- expected_last_event_seq (iyimser ön koşul) ----------------


def test_append_event_with_matching_expected_seq_succeeds(store):
    _, run = _seed_task_and_run(store)
    event, updated = store.append_event(
        run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={}, expected_last_event_seq=0,
    )
    assert event.seq == 1
    assert updated.last_event_seq == 1

    event2, updated2 = store.append_event(
        run_id=run.run_id, type=RunEventType.RUN_PHASE_CHANGED, payload={"phase": "planning"},
        expected_last_event_seq=1,
    )
    assert event2.seq == 2
    assert updated2.last_event_seq == 2


def test_append_event_with_stale_expected_seq_raises_event_sequence_error(store):
    _, run = _seed_task_and_run(store)
    store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    with pytest.raises(EventSequenceError):
        store.append_event(
            run_id=run.run_id, type=RunEventType.RUN_PHASE_CHANGED, payload={"phase": "planning"},
            expected_last_event_seq=0,  # bayat: gerçek last_event_seq artık 1
        )


def test_stale_expected_seq_failure_changes_nothing(store):
    _, run = _seed_task_and_run(store)
    store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    before = store.get_run(run.run_id)
    before_event_count = len(store.events(run.run_id, limit=200).events)

    with pytest.raises(EventSequenceError):
        store.append_event(
            run_id=run.run_id, type=RunEventType.RUN_PHASE_CHANGED, payload={"phase": "planning"},
            expected_last_event_seq=0,
        )

    after = store.get_run(run.run_id)
    assert after == before  # run_events değişmedi, projeksiyon değişmedi, last_event_seq değişmedi
    assert len(store.events(run.run_id, limit=200).events) == before_event_count


def test_expected_seq_rejects_negative_boolean_and_string(store):
    _, run = _seed_task_and_run(store)
    with pytest.raises(EventValidationError):
        store.append_event(
            run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={}, expected_last_event_seq=-1,
        )
    with pytest.raises(EventValidationError):
        store.append_event(
            run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={}, expected_last_event_seq=True,
        )
    with pytest.raises(EventValidationError):
        store.append_event(
            run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={}, expected_last_event_seq="0",
        )
    # Yukarıdaki hiçbiri gerçekleşmedi:
    assert store.get_run(run.run_id).last_event_seq == 0


def test_expected_seq_zero_accepted_for_untouched_run(store):
    _, run = _seed_task_and_run(store)
    event, _ = store.append_event(
        run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={}, expected_last_event_seq=0,
    )
    assert event.seq == 1


def test_two_writers_same_expected_seq_exactly_one_succeeds(store, db_path):
    """İki 'yazıcı', AYNI (bir tanesi için bayat olacak) last_event_seq anlık
    görüntüsünü expected_last_event_seq olarak kullanarak GERÇEK eşzamanlı
    yazmaya çalışırsa, TAM OLARAK biri başarılı olur; diğeri EventSequenceError
    alır — mevcut SQLite serileştirmesi (BEGIN IMMEDIATE) korunur."""
    _, run = _seed_task_and_run(store)
    expected = store.get_run(run.run_id).last_event_seq  # 0

    results = {}
    barrier = threading.Barrier(2)

    def writer(name):
        local_store = RunStore(db_path)
        barrier.wait(timeout=5)
        try:
            local_store.append_event(
                run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={},
                expected_last_event_seq=expected,
            )
            results[name] = "ok"
        except EventSequenceError:
            results[name] = "stale"

    t1 = threading.Thread(target=writer, args=("a",))
    t2 = threading.Thread(target=writer, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    outcomes = list(results.values())
    assert outcomes.count("ok") == 1
    assert outcomes.count("stale") == 1
    final = store.get_run(run.run_id)
    assert final.last_event_seq == 1
    assert len(store.events(run.run_id, limit=200).events) == 1


def test_append_event_updates_run_projection(store):
    _, run = _seed_task_and_run(store)
    _, updated = store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    assert updated.status.value == "running"
    assert updated.phase.value == "starting"
    fetched = store.get_run(run.run_id)
    assert fetched.status.value == "running"
    assert fetched.last_event_seq == 1


def test_append_event_for_missing_run_raises_typed_error(store):
    with pytest.raises(RunNotFoundError):
        store.append_event(run_id="run_missing", type=RunEventType.RUN_CREATED, payload={})


# ---------------- events: pagination ----------------


def test_pagination_middle_page(store):
    _, run = _seed_task_and_run(store)
    for i in range(150):
        store.append_event(
            run_id=run.run_id, type=RunEventType.USAGE_RECORDED, payload={"prompt_tokens": 1},
        )
    page = store.events(run.run_id, after_seq=50, limit=25)
    assert [e.seq for e in page.events] == list(range(51, 76))
    assert page.has_more is True


def test_pagination_final_page_has_more_false(store):
    _, run = _seed_task_and_run(store)
    for i in range(150):
        store.append_event(
            run_id=run.run_id, type=RunEventType.USAGE_RECORDED, payload={"prompt_tokens": 1},
        )
    page = store.events(run.run_id, after_seq=125, limit=25)
    assert [e.seq for e in page.events] == list(range(126, 151))
    assert page.has_more is False


def test_pagination_default_limit_is_50(store):
    _, run = _seed_task_and_run(store)
    for i in range(60):
        store.append_event(run_id=run.run_id, type=RunEventType.USAGE_RECORDED, payload={})
    page = store.events(run.run_id)
    assert len(page.events) == 50
    assert page.has_more is True


def test_pagination_limit_is_hard_capped(store):
    _, run = _seed_task_and_run(store)
    for i in range(MAX_EVENT_PAGE_LIMIT + 10):
        store.append_event(run_id=run.run_id, type=RunEventType.USAGE_RECORDED, payload={})
    page = store.events(run.run_id, limit=MAX_EVENT_PAGE_LIMIT + 50)
    assert len(page.events) == MAX_EVENT_PAGE_LIMIT


def test_pagination_after_seq_is_exclusive(store):
    _, run = _seed_task_and_run(store)
    for i in range(5):
        store.append_event(run_id=run.run_id, type=RunEventType.USAGE_RECORDED, payload={})
    page = store.events(run.run_id, after_seq=0, limit=5)
    assert [e.seq for e in page.events] == [1, 2, 3, 4, 5]
    page2 = store.events(run.run_id, after_seq=5, limit=5)
    assert page2.events == ()
    assert page2.has_more is False


def test_events_ordered_by_seq_not_timestamp_or_uuid(store):
    _, run = _seed_task_and_run(store)
    for i in range(10):
        store.append_event(run_id=run.run_id, type=RunEventType.USAGE_RECORDED, payload={})
    page = store.events(run.run_id, limit=200)
    seqs = [e.seq for e in page.events]
    assert seqs == sorted(seqs)


def test_events_for_missing_run_raises_typed_error(store):
    with pytest.raises(RunNotFoundError):
        store.events("run_missing")


# ---------------- events: unknown types & malformed payloads ----------------


def test_unknown_event_type_can_be_durably_stored(store):
    _, run = _seed_task_and_run(store)
    event, updated = store.append_event(
        run_id=run.run_id, type="future.something_new", payload={"x": 1},
    )
    assert event.seq == 1
    assert updated.last_event_seq == 1  # last_event_seq store tarafından her zaman ilerletilir
    page = store.events(run.run_id)
    assert page.events[0].type == "future.something_new"
    assert page.events[0].payload == {"x": 1}


def test_malformed_payload_rejected_with_no_row_written(store):
    _, run = _seed_task_and_run(store)

    class Unserializable:
        pass

    with pytest.raises(EventValidationError):
        store.append_event(
            run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={"bad": Unserializable()},
        )
    page = store.events(run.run_id)
    assert page.events == ()
    assert store.get_run(run.run_id).last_event_seq == 0


def test_non_dict_payload_rejected_with_no_row_written(store):
    _, run = _seed_task_and_run(store)
    with pytest.raises(EventValidationError):
        store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload="not a dict")
    assert store.events(run.run_id).events == ()


def test_nan_payload_rejected_with_no_row_written(store):
    _, run = _seed_task_and_run(store)
    with pytest.raises(EventValidationError):
        store.append_event(
            run_id=run.run_id, type=RunEventType.USAGE_RECORDED, payload={"cost_usd": float("nan")},
        )
    assert store.events(run.run_id).events == ()
    assert store.get_run(run.run_id).last_event_seq == 0


# ---------------- run metadata validation (create_run) ----------------


def test_create_run_rejects_non_dict_routing_with_typed_error(store):
    task = store.create_task(project_root="/tmp", prompt="p")
    with pytest.raises(EventValidationError):
        store.create_run(task_id=task.task_id, routing=[])


def test_create_run_rejects_non_string_routing_keys(store):
    task = store.create_task(project_root="/tmp", prompt="p")
    with pytest.raises(EventValidationError):
        store.create_run(task_id=task.task_id, routing={1: "model"})


def test_create_run_rejects_path_value_in_routing(store):
    task = store.create_task(project_root="/tmp", prompt="p")
    with pytest.raises(EventValidationError):
        store.create_run(task_id=task.task_id, routing={"x": Path("/tmp")})


def test_create_run_rejects_nan_in_budget(store):
    task = store.create_task(project_root="/tmp", prompt="p")
    with pytest.raises(EventValidationError):
        store.create_run(task_id=task.task_id, budget={"max": float("nan")})


def test_create_run_rejects_tuple_in_workspace_snapshot(store):
    task = store.create_task(project_root="/tmp", prompt="p")
    with pytest.raises(EventValidationError):
        store.create_run(task_id=task.task_id, workspace_snapshot={"x": (1, 2)})


def test_create_run_nested_metadata_round_trips_exactly(store):
    task = store.create_task(project_root="/tmp", prompt="p")
    routing = {"planner": "claude", "nested": {"a": [1, 2, {"b": True}], "c": None}}
    budget = {"max_usd": 1.5, "tags": ["x", "y"]}
    snapshot = {"run_id": "wsrun", "meta": {"deep": {"value": [1, 2, 3]}}}
    run = store.create_run(
        task_id=task.task_id, routing=routing, budget=budget, workspace_snapshot=snapshot,
    )
    fetched = store.get_run(run.run_id)
    assert fetched.routing == routing
    assert fetched.budget == budget
    assert fetched.workspace_snapshot == snapshot


# ---------------- event conflict semantics ----------------


def test_duplicate_event_id_raises_conflict_and_leaves_state_unchanged(store):
    _, run = _seed_task_and_run(store)
    store.append_event(
        run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={}, event_id="evt_fixed",
    )
    before = store.get_run(run.run_id)
    before_event_count = len(store.events(run.run_id).events)

    with pytest.raises(EventConflictError):
        store.append_event(
            run_id=run.run_id, type=RunEventType.RUN_PHASE_CHANGED,
            payload={"phase": "planning"}, event_id="evt_fixed",
        )

    after = store.get_run(run.run_id)
    assert after == before
    assert after.last_event_seq == before.last_event_seq
    assert len(store.events(run.run_id).events) == before_event_count


# ---------------- foreign keys ----------------


def test_foreign_key_integrity_is_enforced_at_sqlite_level(db_path):
    RunStore(db_path).create_task(project_root="/tmp", prompt="p")  # şemayı oluştur/migrasyonu tetikle
    conn = sqlite3.connect(str(db_path))
    try:
        schema.configure_connection(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runs (run_id, task_id, status, phase, created_at) "
                "VALUES ('run_bad', 'task_does_not_exist', 'created', 'created', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.commit()
    finally:
        conn.close()


def test_deleting_run_with_canonical_events_is_rejected_by_foreign_key(store, db_path):
    """run_events -> runs, ON DELETE CASCADE KULLANMAZ: kanonik geçmiş, ana Run
    kazara silinse bile sessizce silinmemelidir (varsayılan NO ACTION/RESTRICT)."""
    _, run = _seed_task_and_run(store)
    store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})

    conn = sqlite3.connect(str(db_path))
    try:
        schema.configure_connection(conn)  # foreign_keys=ON
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run.run_id,))
            conn.commit()
    finally:
        conn.close()

    assert len(store.events(run.run_id).events) == 1  # kanonik geçmiş sapasağlam


# ---------------- atomicity ----------------


def test_event_and_projection_transaction_is_atomic_on_projector_failure(store):
    _, run = _seed_task_and_run(store)
    before = store.get_run(run.run_id)

    with pytest.raises(RunProjectionError):
        store.append_event(
            run_id=run.run_id, type=RunEventType.RUN_PHASE_CHANGED, payload={"phase": "banana"},
        )

    after = store.get_run(run.run_id)
    assert after == before  # projeksiyon TAMAMEN değişmedi
    assert after.last_event_seq == 0
    assert store.events(run.run_id).events == ()  # hiçbir event satırı commit edilmedi


def test_atomicity_holds_across_multiple_prior_successful_appends(store):
    _, run = _seed_task_and_run(store)
    store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    before = store.get_run(run.run_id)
    assert before.last_event_seq == 1

    with pytest.raises(RunProjectionError):
        store.append_event(
            run_id=run.run_id, type=RunEventType.RUN_PHASE_CHANGED, payload={"phase": "nonsense"},
        )

    after = store.get_run(run.run_id)
    assert after == before
    assert after.last_event_seq == 1
    assert len(store.events(run.run_id).events) == 1


def test_rollback_after_event_insert_succeeds_but_run_update_fails(store, db_path):
    """Gerçek bir kısmi-yazım senaryosu: run_events INSERT'i BAŞARILI olur, ardından
    runs UPDATE'i (bir SQLite tetikleyicisiyle simüle edilen bir hata yüzünden)
    BAŞARISIZ olur. Bütün işlem geri alınmalı — ne yarım bir event satırı ne de
    ilerlemiş last_event_seq kalmamalı."""
    _, run = _seed_task_and_run(store)
    before = store.get_run(run.run_id)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TRIGGER blow_up_on_run_update BEFORE UPDATE ON runs "
            "BEGIN SELECT RAISE(ABORT, 'simulated run update failure'); END"
        )
        conn.commit()
    finally:
        conn.close()

    try:
        with pytest.raises(RunStoreError):
            store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    finally:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DROP TRIGGER blow_up_on_run_update")
            conn.commit()
        finally:
            conn.close()

    after = store.get_run(run.run_id)
    assert after == before  # Run izdüşümünün HİÇBİR alanı değişmedi
    assert after.last_event_seq == 0
    assert store.events(run.run_id).events == ()  # run_events'e YAZILAN satır da geri alındı


# ---------------- concurrency ----------------


def test_concurrent_appends_to_same_run_are_sequential_gap_free_and_unique(store, db_path):
    _, run = _seed_task_and_run(store)
    n = 20
    results: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        try:
            # Her thread kendi RunStore/bağlantısını kullanır — paylaşılan tek bir
            # sqlite3 bağlantısı YOK; sıralama BEGIN IMMEDIATE kilidiyle sağlanır.
            local_store = RunStore(db_path)
            event, _ = local_store.append_event(
                run_id=run.run_id, type=RunEventType.USAGE_RECORDED, payload={"prompt_tokens": 1},
            )
            with lock:
                results.append(event.seq)
        except BaseException as exc:  # her thread ya açıkça başarır ya açıkça başarısız olur
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"beklenmeyen hatalar: {errors}"
    assert sorted(results) == list(range(1, n + 1))  # 1..20, boşluksuz, benzersiz

    final = store.get_run(run.run_id)
    assert final.last_event_seq == n
    page = store.events(run.run_id, limit=200)
    assert [e.seq for e in page.events] == list(range(1, n + 1))


# ---------------- list_runs / active_runs ----------------


def test_list_runs_orders_newest_first_and_filters_by_task(store):
    task_a = store.create_task(project_root="/tmp/a", prompt="a")
    task_b = store.create_task(project_root="/tmp/b", prompt="b")
    run_a1 = store.create_run(task_id=task_a.task_id, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    run_a2 = store.create_run(task_id=task_a.task_id, created_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    store.create_run(task_id=task_b.task_id, created_at=datetime(2026, 1, 3, tzinfo=timezone.utc))

    runs_for_a = store.list_runs(task_id=task_a.task_id)
    assert [r.run_id for r in runs_for_a] == [run_a2.run_id, run_a1.run_id]


def test_list_runs_filters_by_project_root_and_stays_newest_first(store):
    """İki farklı proje kökü altında görevler/koşular varsa, project_root
    filtresi yalnızca o projeye ait koşuları (JOIN tasks üzerinden), en
    yeniden en eskiye sıralı döndürmelidir — diğer projenin koşuları asla
    sızmamalıdır."""
    task_a1 = store.create_task(project_root="/tmp/project-a", prompt="a1")
    task_a2 = store.create_task(project_root="/tmp/project-a", prompt="a2")
    task_b = store.create_task(project_root="/tmp/project-b", prompt="b")

    run_a1 = store.create_run(task_id=task_a1.task_id, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    run_a2 = store.create_run(task_id=task_a2.task_id, created_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    run_b = store.create_run(task_id=task_b.task_id, created_at=datetime(2026, 1, 2, tzinfo=timezone.utc))

    runs_for_a = store.list_runs(project_root="/tmp/project-a")
    assert [r.run_id for r in runs_for_a] == [run_a2.run_id, run_a1.run_id]  # newest-first
    assert run_b.run_id not in {r.run_id for r in runs_for_a}

    runs_for_b = store.list_runs(project_root="/tmp/project-b")
    assert [r.run_id for r in runs_for_b] == [run_b.run_id]
    assert run_a1.run_id not in {r.run_id for r in runs_for_b}
    assert run_a2.run_id not in {r.run_id for r in runs_for_b}


def test_list_runs_project_root_respects_limit(store):
    task = store.create_task(project_root="/tmp/project-a", prompt="p")
    for i in range(5):
        store.create_run(task_id=task.task_id, created_at=datetime(2026, 1, 1 + i, tzinfo=timezone.utc))

    runs = store.list_runs(project_root="/tmp/project-a", limit=2)
    assert len(runs) == 2


def test_list_runs_task_id_behavior_unchanged_alongside_project_root_support(store):
    """task_id filtresi (project_root VERİLMEDİĞİNDE) hâlâ eskisi gibi çalışır — regresyon yok."""
    task_a = store.create_task(project_root="/tmp/a", prompt="a")
    task_b = store.create_task(project_root="/tmp/a", prompt="b")  # AYNI proje kökü, farklı görev
    run_a = store.create_run(task_id=task_a.task_id)
    store.create_run(task_id=task_b.task_id)

    runs_for_task_a = store.list_runs(task_id=task_a.task_id)
    assert [r.run_id for r in runs_for_task_a] == [run_a.run_id]


def test_list_runs_rejects_ambiguous_task_id_and_project_root_combination(store):
    task = store.create_task(project_root="/tmp/a", prompt="p")
    with pytest.raises(RunStoreError):
        store.list_runs(task_id=task.task_id, project_root="/tmp/a")


def test_active_runs_returns_only_non_terminal_statuses(store):
    task = store.create_task(project_root="/tmp", prompt="p")
    running = store.create_run(task_id=task.task_id)
    store.append_event(run_id=running.run_id, type=RunEventType.RUN_STARTED, payload={})

    done = store.create_run(task_id=task.task_id)
    store.append_event(run_id=done.run_id, type=RunEventType.RUN_STARTED, payload={})
    store.append_event(run_id=done.run_id, type=RunEventType.RUN_COMPLETED, payload={})

    active_ids = {r.run_id for r in store.active_runs()}
    assert running.run_id in active_ids
    assert done.run_id not in active_ids


# ---------------- canonical UTC datetimes (store round-trip) ----------------


def test_aware_non_utc_created_at_round_trips_as_equivalent_utc(store):
    plus3 = timezone(timedelta(hours=3))
    local_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=plus3)
    task = store.create_task(project_root="/tmp", prompt="p", created_at=local_time)
    assert task.created_at == datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
    fetched = store.get_task(task.task_id)
    assert fetched.created_at == datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
    assert fetched.created_at.tzinfo == timezone.utc


def test_naive_created_at_input_rejected_before_reaching_store(store):
    with pytest.raises(ValueError):
        store.create_task(project_root="/tmp", prompt="p", created_at=datetime(2026, 1, 1))


def test_persisted_and_reloaded_timestamp_is_always_utc(store):
    _, run = _seed_task_and_run(store)
    store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    fetched = store.get_run(run.run_id)
    assert fetched.started_at.tzinfo == timezone.utc
    assert fetched.created_at.tzinfo == timezone.utc


def test_manually_corrupted_naive_timestamp_in_run_produces_typed_error(store, db_path):
    _, run = _seed_task_and_run(store)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE runs SET created_at = ? WHERE run_id = ?",
            ("2026-01-01T00:00:00", run.run_id),  # naive — ofset yok
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        store.get_run(run.run_id)


def test_manually_corrupted_naive_timestamp_in_task_produces_typed_error(store, db_path):
    task, _ = _seed_task_and_run(store)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE task_id = ?",
            ("2026-01-01T00:00:00", task.task_id),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        store.get_task(task.task_id)


# ---------------- corrupt stored data must not leak raw decode errors ----------------


def test_corrupt_status_enum_value_raises_typed_error(store, db_path):
    _, run = _seed_task_and_run(store)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE runs SET status = 'not-a-status' WHERE run_id = ?", (run.run_id,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        store.get_run(run.run_id)


def test_corrupt_routing_json_wrong_shape_raises_typed_error(store, db_path):
    """routing_json = '[]' — geçerli JSON ama YANLIŞ ŞEKİL (dict değil); sessizce {} olmamalı."""
    _, run = _seed_task_and_run(store)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE runs SET routing_json = '[]' WHERE run_id = ?", (run.run_id,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        store.get_run(run.run_id)


def test_corrupt_event_payload_invalid_json_raises_typed_error(store, db_path):
    _, run = _seed_task_and_run(store)
    event, _ = store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE run_events SET payload_json = 'not valid json{' WHERE event_id = ?",
            (event.event_id,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        store.events(run.run_id)


def test_corrupt_event_payload_wrong_shape_raises_typed_error(store, db_path):
    _, run = _seed_task_and_run(store)
    event, _ = store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE run_events SET payload_json = '[1, 2, 3]' WHERE event_id = ?",
            (event.event_id,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        store.events(run.run_id)


def test_corrupt_event_payload_nan_constant_raises_run_store_error_not_event_validation_error(
    store, db_path
):
    """json.loads NaN'ı sessizce kabul eder; _decode_json_dict bunu kanonik JSON
    sözleşmesinden geçirip ValueError'a çevirmeli — RunEvent.__post_init__'in
    fırlattığı EventValidationError (YANLIŞ soyutlama katmanı) asla RunStore'un
    genel okuma API'sinden dışarı sızmamalı."""
    _, run = _seed_task_and_run(store)
    event, _ = store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE run_events SET payload_json = ? WHERE event_id = ?",
            ('{"x": NaN}', event.event_id),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        store.events(run.run_id)


def test_corrupt_routing_json_infinity_constant_raises_run_store_error(store, db_path):
    """routing_json = '{"x": Infinity}' — geçerli JSON, geçerli dict şekli, ama
    kanonik JSON sözleşmesini ihlal eder (sonlu olmayan sayı); yine de tipli
    RunStoreError ile reddedilmeli, ham bir hata sızmamalı."""
    _, run = _seed_task_and_run(store)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE runs SET routing_json = ? WHERE run_id = ?",
            ('{"x": Infinity}', run.run_id),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        store.get_run(run.run_id)


def test_corrupt_budget_json_negative_infinity_raises_run_store_error(store, db_path):
    _, run = _seed_task_and_run(store)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE runs SET budget_json = ? WHERE run_id = ?",
            ('{"max": -Infinity}', run.run_id),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        store.get_run(run.run_id)


def test_corrupt_nested_non_canonical_value_raises_run_store_error(store, db_path):
    """Sonlu-olmayan sabit, doğrudan üst seviyede değil, iç içe bir yapının
    DERİNLİĞİNDE olsa bile jsonutil'in özyinelemeli doğrulaması yakalamalı."""
    _, run = _seed_task_and_run(store)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE runs SET workspace_snapshot_json = ? WHERE run_id = ?",
            ('{"meta": {"nested": [1, 2, {"deep": NaN}]}}', run.run_id),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        store.get_run(run.run_id)


def test_corrupt_row_in_list_runs_raises_typed_error(store, db_path):
    task, run = _seed_task_and_run(store)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE runs SET phase = 'not-a-phase' WHERE run_id = ?", (run.run_id,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        store.list_runs(task_id=task.task_id)


def test_corrupt_row_in_active_runs_raises_typed_error(store, db_path):
    _, run = _seed_task_and_run(store)  # varsayılan status=created -> active_runs'da görünür
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE runs SET budget_json = 'not valid json{' WHERE run_id = ?", (run.run_id,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RunStoreError):
        store.active_runs()
