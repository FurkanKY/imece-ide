"""run_runtime.service.RunRuntime / DurableEventTail — entegrasyon testleri."""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_runtime.bus import EventBus  # noqa: E402
from run_runtime.errors import (  # noqa: E402
    EventStreamClosedError,
    EventValidationError,
    RunNotFoundError,
    RunProjectionError,
)
from run_runtime.events import RunEventType  # noqa: E402
from run_runtime.service import RunRuntime  # noqa: E402
from run_runtime.store import RunStore  # noqa: E402


@pytest.fixture
def runtime(tmp_path) -> RunRuntime:
    return RunRuntime(RunStore(tmp_path / "runtime.sqlite3"))


def _seed(runtime, *, task_id="task_seed", run_id="run_seed"):
    task = runtime.create_task(project_root="/tmp/proj", prompt="do X", task_id=task_id)
    run = runtime.create_run(task_id=task.task_id, run_id=run_id)
    return task, run


# ---------------- commit-before-publish ----------------


def test_record_persists_before_publish_is_observed(runtime):
    """publish() çağrıldığı ANDA, karşılık gelen dayanıklı event VE Run
    izdüşümü RunStore'da ZATEN görünür olmalı (yayından ÖNCE COMMIT olmuş)."""
    _, run = _seed(runtime)
    observed = {}

    class SpyBus(EventBus):
        def publish(self, notice):
            page = runtime.store.events(notice.run_id, after_seq=0, limit=10)
            observed["event_count"] = len(page.events)
            observed["run_last_event_seq"] = runtime.store.get_run(notice.run_id).last_event_seq
            super().publish(notice)

    spy_runtime = RunRuntime(runtime.store, bus=SpyBus())
    event, updated_run = spy_runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})

    assert observed["event_count"] == 1
    assert observed["run_last_event_seq"] == event.seq
    assert updated_run.last_event_seq == event.seq


def test_failed_store_write_emits_no_notice_missing_run(runtime):
    calls = []

    class SpyBus(EventBus):
        def publish(self, notice):
            calls.append(notice)
            super().publish(notice)

    spy_runtime = RunRuntime(runtime.store, bus=SpyBus())
    with pytest.raises(RunNotFoundError):
        spy_runtime.record(run_id="run_does_not_exist", type=RunEventType.RUN_STARTED, payload={})
    assert calls == []


def test_failed_store_write_emits_no_notice_projection_error(runtime):
    _, run = _seed(runtime)
    calls = []

    class SpyBus(EventBus):
        def publish(self, notice):
            calls.append(notice)
            super().publish(notice)

    spy_runtime = RunRuntime(runtime.store, bus=SpyBus())
    with pytest.raises(RunProjectionError):
        spy_runtime.record(
            run_id=run.run_id, type=RunEventType.RUN_PHASE_CHANGED, payload={"phase": "banana"},
        )
    assert calls == []
    assert runtime.store.get_run(run.run_id).last_event_seq == 0


# ---------------- durable replay / live handoff ----------------


def test_open_event_tail_replays_existing_durable_history(runtime):
    _, run = _seed(runtime)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_PHASE_CHANGED, payload={"phase": "planning"})

    with runtime.open_event_tail(run.run_id) as tail:
        page = tail.next_page(timeout=1)
        assert page is not None
        assert [e.seq for e in page.events] == [1, 2]
        assert tail.cursor == 2


def test_live_append_after_subscription_is_observed(runtime):
    _, run = _seed(runtime)
    with runtime.open_event_tail(run.run_id) as tail:
        page = tail.next_page(timeout=0.2)
        assert page is None  # henüz event yok

        runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})

        page = tail.next_page(timeout=2)
        assert page is not None
        assert [e.seq for e in page.events] == [1]


def test_reconnect_tail_from_after_seq(runtime):
    _, run = _seed(runtime)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_PHASE_CHANGED, payload={"phase": "planning"})
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_PHASE_CHANGED, payload={"phase": "executing"})

    with runtime.open_event_tail(run.run_id, after_seq=1) as tail:
        page = tail.next_page(timeout=1)
        assert page is not None
        assert [e.seq for e in page.events] == [2, 3]
        assert tail.cursor == 3


def test_tail_pagination_respects_limit_and_advances_cursor(runtime):
    _, run = _seed(runtime)
    for _ in range(5):
        runtime.record(run_id=run.run_id, type=RunEventType.USAGE_RECORDED, payload={})

    with runtime.open_event_tail(run.run_id) as tail:
        page1 = tail.next_page(timeout=1, limit=2)
        assert [e.seq for e in page1.events] == [1, 2]
        assert tail.cursor == 2

        page2 = tail.next_page(timeout=1, limit=2)
        assert [e.seq for e in page2.events] == [3, 4]
        assert tail.cursor == 4

        page3 = tail.next_page(timeout=1, limit=2)
        assert [e.seq for e in page3.events] == [5]
        assert tail.cursor == 5


# ---------------- lost-wake race (item 9) ----------------


def test_race_free_subscribe_before_first_query_no_lost_wake(runtime, monkeypatch):
    """Klasik 'kayıp uyanma' senaryosu: subscribe olur, ilk (boş) sorgu
    yapılır, TÜKETİCİ gerçekten beklemeye başlamadan HEMEN ÖNCE bir başka
    thread event'i commit edip yayınlar. Tüketici yine de uyanıp event'i
    almalıdır (sonsuza kadar bloklanmamalı)."""
    _, run = _seed(runtime)
    tail = runtime.open_event_tail(run.run_id)  # subscribe BURADA gerçekleşir
    try:
        query_done = threading.Event()
        writer_done = threading.Event()
        original_wait = tail._subscription.wait

        def patched_wait(timeout=None):
            # next_page ilk (boş) sorgudan SONRA, gerçek wait() çağrılmadan
            # HEMEN önce buraya girer. Yazıcı thread'in commit+publish
            # yapmasını burada senkronize biçimde bekleriz — böylece event,
            # gerçek Condition.wait() BAŞLAMADAN ÖNCE (ama subscribe'dan
            # SONRA) yayınlanmış olur.
            query_done.set()
            assert writer_done.wait(timeout=5), "yazıcı thread zamanında commit etmedi"
            return original_wait(timeout=timeout)

        monkeypatch.setattr(tail._subscription, "wait", patched_wait)

        result_holder = {}

        def consumer():
            result_holder["page"] = tail.next_page(timeout=5)

        t = threading.Thread(target=consumer)
        t.start()

        assert query_done.wait(timeout=5), "tüketici ilk sorguyu zamanında yapmadı"
        runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
        writer_done.set()

        t.join(timeout=5)
        assert not t.is_alive()
        page = result_holder["page"]
        assert page is not None
        assert [e.seq for e in page.events] == [1]
    finally:
        tail.close()


# ---------------- coalescing does not drop durable events (item 10) ----------------


def test_coalesced_wakes_still_replay_every_durable_event_across_pagination(runtime):
    _, run = _seed(runtime)
    with runtime.open_event_tail(run.run_id) as tail:
        assert tail.next_page(timeout=0.2) is None  # henüz event yok

        # Tüketici HİÇ okumadan (bus tarafında COALESCE olacak şekilde) çok
        # sayıda event ekle.
        for _ in range(70):
            runtime.record(
                run_id=run.run_id, type=RunEventType.USAGE_RECORDED, payload={"prompt_tokens": 1},
            )

        collected = []
        while len(collected) < 70:
            page = tail.next_page(timeout=2, limit=25)
            assert page is not None, "beklenen event'ler gelmeden zaman aşımına uğradı"
            collected.extend(e.seq for e in page.events)

        assert collected == list(range(1, 71))  # boşluksuz, artan, hiçbiri kaybolmadı


# ---------------- close / EventStreamClosedError ----------------


def test_next_page_raises_after_close_even_with_no_backlog(runtime):
    _, run = _seed(runtime)
    tail = runtime.open_event_tail(run.run_id)
    tail.close()
    with pytest.raises(EventStreamClosedError):
        tail.next_page(timeout=1)


def test_next_page_raises_immediately_when_closed_even_with_unread_backlog(runtime):
    """close() SERT bir kaynak sınırıdır: SQLite'ta okunmamış dayanıklı
    event'ler olsa bile, kapalı bir kuyrukta next_page() HİÇBİR sorgu
    yapmadan HEMEN EventStreamClosedError fırlatır (backlog DRAIN EDİLMEZ)."""
    _, run = _seed(runtime)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    tail = runtime.open_event_tail(run.run_id)
    tail.close()
    with pytest.raises(EventStreamClosedError):
        tail.next_page(timeout=1)


def test_blocked_next_page_wakes_promptly_and_raises_on_close_from_another_thread(runtime):
    """Hâlihazırda bir bildirim bekleyen (bloklu) next_page(), başka bir
    thread close() çağırdığında PROMPT biçimde uyanmalı ve
    EventStreamClosedError ile çıkmalıdır — 30 saniyelik zaman aşımının
    TAMAMINI BEKLEMEMELİDİR."""
    _, run = _seed(runtime)
    tail = runtime.open_event_tail(run.run_id)

    entered_wait = threading.Event()
    original_wait = tail._subscription.wait

    def patched_wait(timeout=None):
        entered_wait.set()
        return original_wait(timeout=timeout)

    tail._subscription.wait = patched_wait

    result = {}

    def consumer():
        try:
            tail.next_page(timeout=30)
        except EventStreamClosedError as exc:
            result["raised"] = exc

    t = threading.Thread(target=consumer)
    start = time.monotonic()
    t.start()
    assert entered_wait.wait(timeout=5), "tüketici zamanında beklemeye girmedi"
    time.sleep(0.05)  # gerçekten Condition.wait() içinde olduğundan emin ol

    tail.close()

    t.join(timeout=5)
    elapsed = time.monotonic() - start
    assert not t.is_alive()
    assert "raised" in result  # EventStreamClosedError ile çıktı, sessizce dönmedi
    assert elapsed < 5.0, "30sn zaman aşımının tamamı beklendi; close() PROMPT uyandırmadı"


def test_tail_context_manager_closes_on_exit(runtime):
    _, run = _seed(runtime)
    with runtime.open_event_tail(run.run_id) as tail:
        pass
    with pytest.raises(EventStreamClosedError):
        tail.next_page(timeout=0.1)


def test_close_is_idempotent_on_tail(runtime):
    _, run = _seed(runtime)
    tail = runtime.open_event_tail(run.run_id)
    tail.close()
    tail.close()  # ikinci çağrı zararsız


# ---------------- open_event_tail must not leak a subscription for a missing Run ----------------


def test_open_event_tail_for_missing_run_leaves_no_subscription(runtime):
    """subscribe HER ZAMAN önce gerçekleşir (subscribe-before-replay korunur),
    ancak Run bulunamazsa yeni abonelik HEMEN kapatılır — EventBus kaydında
    asla kapatılmayacak yetim bir abonelik BIRAKILMAZ."""
    assert runtime.bus.subscriber_count("run_missing") == 0
    with pytest.raises(RunNotFoundError):
        runtime.open_event_tail("run_missing")
    assert runtime.bus.subscriber_count("run_missing") == 0


# ---------------- after_seq validation ----------------


def test_open_event_tail_rejects_negative_after_seq(runtime):
    _, run = _seed(runtime)
    with pytest.raises(EventValidationError):
        runtime.open_event_tail(run.run_id, after_seq=-1)
    assert runtime.bus.subscriber_count(run.run_id) == 0


def test_open_event_tail_rejects_boolean_after_seq(runtime):
    _, run = _seed(runtime)
    with pytest.raises(EventValidationError):
        runtime.open_event_tail(run.run_id, after_seq=True)
    assert runtime.bus.subscriber_count(run.run_id) == 0


def test_open_event_tail_rejects_string_after_seq(runtime):
    _, run = _seed(runtime)
    with pytest.raises(EventValidationError):
        runtime.open_event_tail(run.run_id, after_seq="1")
    assert runtime.bus.subscriber_count(run.run_id) == 0


def test_open_event_tail_accepts_after_seq_zero(runtime):
    _, run = _seed(runtime)
    with runtime.open_event_tail(run.run_id, after_seq=0) as tail:
        assert tail.cursor == 0
