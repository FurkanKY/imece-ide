"""run_runtime.bus.EventBus / EventSubscription — süreç-yerel bildirim testleri."""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_runtime.bus import EventBus, EventNotice  # noqa: E402
from run_runtime.errors import EventValidationError  # noqa: E402


def test_event_notice_validates_latest_seq():
    EventNotice(run_id="run_x", latest_seq=1)  # geçerli
    with pytest.raises(EventValidationError):
        EventNotice(run_id="run_x", latest_seq=0)
    with pytest.raises(EventValidationError):
        EventNotice(run_id="run_x", latest_seq=-1)
    with pytest.raises(EventValidationError):
        EventNotice(run_id="run_x", latest_seq=True)
    with pytest.raises(EventValidationError):
        EventNotice(run_id="run_x", latest_seq="1")
    with pytest.raises(EventValidationError):
        EventNotice(run_id="", latest_seq=1)


def test_subscription_is_run_scoped():
    bus = EventBus()
    sub_a = bus.subscribe("run_a")
    sub_b = bus.subscribe("run_b")
    try:
        bus.publish(EventNotice(run_id="run_a", latest_seq=5))
        notice_a = sub_a.wait(timeout=1)
        assert notice_a is not None and notice_a.latest_seq == 5
        notice_b = sub_b.wait(timeout=0.05)
        assert notice_b is None  # run_b'ye ait abone HİÇ uyanmadı
    finally:
        sub_a.close()
        sub_b.close()


def test_publishes_coalesce_to_latest_seq():
    bus = EventBus()
    sub = bus.subscribe("run_x")
    try:
        bus.publish(EventNotice(run_id="run_x", latest_seq=10))
        bus.publish(EventNotice(run_id="run_x", latest_seq=11))
        bus.publish(EventNotice(run_id="run_x", latest_seq=12))
        notice = sub.wait(timeout=1)
        assert notice is not None
        assert notice.latest_seq == 12  # tek bir uyanış yeterli, en yükseğe birleşti
        assert sub.wait(timeout=0.05) is None  # ikinci bir bildirim YOK
    finally:
        sub.close()


def test_independent_subscribers_to_same_run_each_get_notified():
    bus = EventBus()
    sub1 = bus.subscribe("run_x")
    sub2 = bus.subscribe("run_x")
    try:
        bus.publish(EventNotice(run_id="run_x", latest_seq=7))
        n1 = sub1.wait(timeout=1)
        n2 = sub2.wait(timeout=1)
        assert n1 is not None and n1.latest_seq == 7
        assert n2 is not None and n2.latest_seq == 7
    finally:
        sub1.close()
        sub2.close()


def test_wait_times_out_when_no_publish():
    bus = EventBus()
    sub = bus.subscribe("run_x")
    try:
        start = time.monotonic()
        result = sub.wait(timeout=0.1)
        elapsed = time.monotonic() - start
        assert result is None
        assert elapsed < 2.0
    finally:
        sub.close()


def test_close_wakes_blocked_waiter_promptly():
    bus = EventBus()
    sub = bus.subscribe("run_x")
    result_holder = {}
    entered_wait = threading.Event()

    def waiter():
        entered_wait.set()
        result_holder["result"] = sub.wait(timeout=30)
        result_holder["done_at"] = time.monotonic()

    t = threading.Thread(target=waiter)
    start = time.monotonic()
    t.start()
    assert entered_wait.wait(timeout=5)
    time.sleep(0.05)  # waiter'ın gerçekten Condition.wait() içine girdiğinden emin ol
    sub.close()
    t.join(timeout=5)
    assert not t.is_alive()
    assert result_holder["result"] is None
    assert result_holder["done_at"] - start < 5.0  # 30sn zaman aşımını BEKLEMEDİ


def test_close_is_idempotent():
    bus = EventBus()
    sub = bus.subscribe("run_x")
    sub.close()
    sub.close()  # ikinci çağrı zararsız, hata fırlatmaz


def test_subscription_context_manager_closes_on_exit():
    bus = EventBus()
    with bus.subscribe("run_x") as sub:
        assert bus.subscriber_count("run_x") == 1
    assert sub.closed
    assert bus.subscriber_count("run_x") == 0


def test_bus_does_not_retain_state_when_no_subscribers_remain():
    bus = EventBus()
    sub = bus.subscribe("run_x")
    assert bus.subscriber_count("run_x") == 1
    sub.close()
    assert bus.subscriber_count("run_x") == 0
    assert "run_x" not in bus._subscriptions  # iç durum tamamen temizlendi


def test_publish_with_no_subscribers_is_a_no_op():
    bus = EventBus()
    bus.publish(EventNotice(run_id="run_nobody_listens", latest_seq=1))  # patlamaz


def test_publish_for_one_run_does_not_wake_another_runs_subscribers():
    bus = EventBus()
    sub_a = bus.subscribe("run_a")
    sub_b = bus.subscribe("run_b")
    try:
        bus.publish(EventNotice(run_id="run_a", latest_seq=1))
        bus.publish(EventNotice(run_id="run_a", latest_seq=2))
        assert sub_a.wait(timeout=1) is not None
        assert sub_b.wait(timeout=0.05) is None
    finally:
        sub_a.close()
        sub_b.close()


def test_thread_safe_concurrent_publish_from_many_threads_coalesces_to_max():
    """Aynı Run'a birçok thread'den GERÇEK eşzamanlı publish() çağrıları
    yapılsa bile, tüketici tam olarak en yüksek seq'i görür (sıra ne olursa
    olsun) — bu, _notify'ın kilit altında doğru çalıştığını kanıtlar."""
    bus = EventBus()
    sub = bus.subscribe("run_x")
    try:
        def publisher(seq):
            bus.publish(EventNotice(run_id="run_x", latest_seq=seq))

        threads = [threading.Thread(target=publisher, args=(seq,)) for seq in range(1, 51)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive()

        notice = sub.wait(timeout=2)
        assert notice is not None
        assert notice.latest_seq == 50
        assert sub.wait(timeout=0.05) is None
    finally:
        sub.close()


def test_no_unbounded_queue_repeated_publishes_do_not_accumulate():
    """Abonelik durumu bir kuyruk DEĞİL, yalnızca dirty+latest_seq bayrağıdır:
    tüketici hiç okumadan binlerce publish() çağrısı, tek bir uyanıştan
    fazlasını üretmez."""
    bus = EventBus()
    sub = bus.subscribe("run_x")
    try:
        for seq in range(1, 2001):
            bus.publish(EventNotice(run_id="run_x", latest_seq=seq))
        notice = sub.wait(timeout=1)
        assert notice is not None
        assert notice.latest_seq == 2000
        assert sub.wait(timeout=0.05) is None  # tek bir bildirimden fazlası YOK
    finally:
        sub.close()


def test_wait_survives_spurious_wakeup_without_dirty_or_close():
    """threading.Condition.wait() SAHTE biçimde (dirty ayarlanmadan bir
    notify_all() ile, gerçek zaman aşımı olmadan) uyanabilir. wait(), dirty
    hâlâ False iken böyle bir sahte uyanıştan sonra bile ERKEN None
    DÖNMEMELİ; gerçek bir publish/close/timeout'a kadar beklemeye DEVAM
    ETMELİDİR (standart yüklem/predicate deseni — bkz. EventSubscription.wait)."""
    bus = EventBus()
    sub = bus.subscribe("run_x")
    try:
        entered_wait = threading.Event()
        result = {}

        def waiter():
            entered_wait.set()  # gerçek Condition.wait() çağrısına GİRMEDEN hemen önce
            result["notice"] = sub.wait(timeout=2)
            result["done_at"] = time.monotonic()

        start = time.monotonic()
        t = threading.Thread(target=waiter)
        t.start()
        assert entered_wait.wait(timeout=5)
        time.sleep(0.1)  # waiter'ın GERÇEKTEN Condition.wait() içine girdiğinden emin ol

        # Sahte bir uyanış simüle et: dirty'yi AYARLAMADAN doğrudan notify_all().
        with sub._condition:
            sub._condition.notify_all()

        # Sahte uyanıştan sonra bile wait() henüz dönmemiş olmalı.
        time.sleep(0.1)
        assert "notice" not in result

        # Şimdi GERÇEK bir publish gönder; wait() ancak bundan sonra dönmeli.
        bus.publish(EventNotice(run_id="run_x", latest_seq=1))
        t.join(timeout=5)

        assert not t.is_alive()
        assert result["notice"] is not None
        assert result["notice"].latest_seq == 1
        assert result["done_at"] - start < 2.0  # 2sn zaman aşımını BEKLEMEDİ
    finally:
        sub.close()
