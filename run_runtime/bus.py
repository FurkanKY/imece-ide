"""run_runtime.bus — süreç-yerel, iş parçacığı güvenli EVENT NOTICE veri yolu.

Bu YENİ bir event mağazası DEĞİLDİR. Kanonik dayanıklı event'ler yalnızca
SQLite'ta (run_events tablosu) yaşar. Bellek-içi bir bildirim yalnızca şunu
ifade eder:

    "Bu Run'ın SQLite'ta daha yeni dayanıklı satırları OLABİLİR."

Tüketiciler HER ZAMAN RunStore'u kendi dayanıklı seq imleçleriyle yeniden
sorgulamalıdır (bkz. run_runtime.service.DurableEventTail). Birden fazla
bildirim BİRLEŞEBİLİR (coalesce) çünkü kalıcılığı SQLite garanti eder,
bildirim kuyruğu DEĞİL — bir bildirimin "kaybolması" (bir sonrakiyle
birleşmesi) veri kaybı DEĞİLDİR.

Üretici (publish çağıran) kod ASLA rastgele abone/uygulama kodu çalıştırmaz:
geri çağırma (callback) tabanlı abonelik YOKTUR — yalnızca subscribe()/wait()
ile her aboneliğin kendi threading.Condition'ı üzerinde bekleme vardır.
asyncio event loop veya gizli bir arka plan iş parçacığı KULLANILMAZ; yalnızca
standart kütüphane iş parçacığı ilkel öğeleri (threading.Lock/Condition)
kullanılır.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from run_runtime.errors import EventValidationError


@dataclass(frozen=True, slots=True)
class EventNotice:
    """"Bu Run'ın daha yeni dayanıklı satırları olabilir" bildirimi.

    Kasıtlı olarak KÜÇÜKTÜR: RunEvent payload/gövdesini TAŞIMAZ. Tüketiciler
    latest_seq'i yalnızca "en azından bu kadar ilerle" ipucu olarak kullanır;
    gerçek event'ler her zaman RunStore'dan okunur.
    """

    run_id: str
    latest_seq: int

    def __post_init__(self) -> None:
        if not self.run_id:
            raise EventValidationError("run_id boş olamaz.")
        if isinstance(self.latest_seq, bool) or not isinstance(self.latest_seq, int):
            raise EventValidationError(f"latest_seq bir tam sayı olmalı: {self.latest_seq!r}")
        if self.latest_seq < 1:
            raise EventValidationError(f"latest_seq >= 1 olmalı: {self.latest_seq}")


class EventSubscription:
    """Tek bir Run'a özel, bağımsız durumlu tek bir abonelik.

    Yalnızca EventBus.subscribe() tarafından oluşturulur. Durumu kasıtlı
    olarak KÜÇÜKTÜR (bir kuyruk DEĞİL): yalnızca bir dirty bayrağı + en son
    görülen seq. Art arda publish() çağrıları, tüketici henüz bir wake
    tüketmeden önce BİRLEŞİR (coalesce) — yalnızca en yüksek seq saklanır.
    """

    def __init__(self, run_id: str, *, on_close: Callable[["EventSubscription"], None] | None = None):
        self._run_id = run_id
        self._condition = threading.Condition()
        self._dirty = False
        self._latest_seq = 0
        self._closed = False
        self._on_close = on_close

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def _notify(self, latest_seq: int) -> None:
        """Yalnızca EventBus.publish() tarafından çağrılır. Rastgele kod ÇALIŞTIRMAZ."""
        with self._condition:
            if self._closed:
                return
            if latest_seq > self._latest_seq:
                self._latest_seq = latest_seq
            self._dirty = True
            self._condition.notify_all()

    def wait(self, timeout: float | None = None) -> EventNotice | None:
        """dirty ise (bekleyen bir bildirim varsa) bir EventNotice döndürür ve dirty'yi temizler.

        Zaman aşımında None döner. Kapatılmışsa (ve bekleyen bir bildirim
        yoksa) HEMEN None ile uyanır — sonsuza kadar bloklanmaz.

        threading.Condition.wait() SAHTE (spurious) biçimde, ne notify_all()
        ne de zaman aşımı olmadan uyanabilir; bu yüzden tek bir wait() çağrısı
        YETERLİ DEĞİLDİR. Standart yüklem (predicate) deseni kullanılır:
        `dirty veya closed` doğru olana ya da monotonic deadline'a ulaşılana
        kadar döngüyle beklenir — sahte bir uyanış, dirty=False iken erken
        None döndürmez.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while not self._dirty and not self._closed:
                if deadline is None:
                    self._condition.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._condition.wait(timeout=remaining)
            if self._dirty:
                self._dirty = False
                return EventNotice(run_id=self._run_id, latest_seq=self._latest_seq)
            return None

    def close(self) -> None:
        """İdempotenttir; aboneliği kaydından çıkarır ve blok olmuş her bekleyeni uyandırır."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        if self._on_close is not None:
            # Kilit DIŞINDA çağrılır: EventBus'ın kendi kilidiyle olası bir
            # kilitlenmeyi (deadlock) önler.
            self._on_close(self)

    def __enter__(self) -> "EventSubscription":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class EventBus:
    """Süreç-yerel, run_id'ye göre kapsamlı, iş parçacığı güvenli bildirim veri yolu."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: dict[str, list[EventSubscription]] = {}

    def subscribe(self, run_id: str) -> EventSubscription:
        if not run_id:
            raise EventValidationError("run_id boş olamaz.")
        subscription = EventSubscription(run_id, on_close=self._unsubscribe)
        with self._lock:
            self._subscriptions.setdefault(run_id, []).append(subscription)
        return subscription

    def _unsubscribe(self, subscription: EventSubscription) -> None:
        with self._lock:
            subs = self._subscriptions.get(subscription.run_id)
            if not subs:
                return
            try:
                subs.remove(subscription)
            except ValueError:
                pass
            if not subs:
                # Abone kalmayan bir run_id için HİÇBİR durum TUTULMAZ.
                del self._subscriptions[subscription.run_id]

    def publish(self, notice: EventNotice) -> None:
        """Yalnızca `notice.run_id`'ye abone olanları uyandırır — başka hiçbir Run'ı ETKİLEMEZ.

        Abone listesinin anlık görüntüsü kilit ALTINDA alınır, ancak asıl
        uyandırma (her aboneliğin KENDİ Condition'ı üzerinden) kilit DIŞINDA
        yapılır — böylece yavaş/bloklu bir abone, başka aboneleri veya yeni
        subscribe()/publish() çağrılarını GECİKTİRMEZ. Hiçbir abone/uygulama
        callback'i ÇALIŞTIRILMAZ.
        """
        with self._lock:
            subs = tuple(self._subscriptions.get(notice.run_id, ()))
        for subscription in subs:
            subscription._notify(notice.latest_seq)

    def subscriber_count(self, run_id: str) -> int:
        with self._lock:
            return len(self._subscriptions.get(run_id, ()))
