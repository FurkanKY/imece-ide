"""run_runtime.service — RunStore (dayanıklı) + EventBus (canlı) birleşimi.

Bu, canlı bildirim EKLEYEN ince bir birleştirme (composition) katmanıdır;
GENİŞ bir yönetici sınıf DEĞİLDİR. Zorunlu sıralama:

    1. RunStore.append_event() TAMAMEN COMMIT olur (event + projeksiyon,
       aynı SQLite işleminde atomik olarak).
    2. YALNIZCA BUNDAN SONRA EventBus.publish() çağrılır.

append_event başarısız olursa (herhangi bir tipli hata) publish() ASLA
çağrılmaz. Bellek-içi bildirim gerçek event içeriğini TAŞIMAZ; tüketiciler
her zaman RunStore'u yeniden sorgular (bkz. DurableEventTail).

DurableEventTail.next_page(), KRİTİK bir sıralamayı korur: ÖNCE subscribe,
SONRA dayanıklı geçmişi sorgula/replay et. Tersi (önce replay, sonra
subscribe) subscribe ile ilk sorgu arasında yayınlanan bir event'in
kaybolmasına yol açacak bir pencere (lost-event window) yaratır.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from run_runtime.bus import EventBus, EventNotice, EventSubscription
from run_runtime.errors import EventStreamClosedError, EventValidationError
from run_runtime.events import CURRENT_SCHEMA_VERSION, RunEvent, RunEventSpec
from run_runtime.models import RunRecord, TaskRecord
from run_runtime.store import DEFAULT_EVENT_PAGE_LIMIT, EventPage, RunStore


class RunRuntime:
    """RunStore (dayanıklı yazma/okuma) + EventBus (canlı "yeniden sorgula" bildirimi) birleşimi."""

    def __init__(self, store: RunStore, bus: EventBus | None = None):
        self._store = store
        self._bus = bus if bus is not None else EventBus()

    @property
    def store(self) -> RunStore:
        return self._store

    @property
    def bus(self) -> EventBus:
        return self._bus

    # ---------------- basit delege yardımcıları (küçük, kasıtlı olarak sınırlı) ----------------

    def create_task(
        self,
        *,
        project_root: str,
        prompt: str,
        task_id: str | None = None,
        created_at: datetime | None = None,
    ) -> TaskRecord:
        return self._store.create_task(
            project_root=project_root, prompt=prompt, task_id=task_id, created_at=created_at,
        )

    def create_run(
        self,
        *,
        task_id: str,
        run_id: str | None = None,
        attempt: int = 1,
        retry_of_run_id: str | None = None,
        routing: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        workspace_snapshot: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> RunRecord:
        return self._store.create_run(
            task_id=task_id, run_id=run_id, attempt=attempt, retry_of_run_id=retry_of_run_id,
            routing=routing, budget=budget, workspace_snapshot=workspace_snapshot,
            created_at=created_at,
        )

    def get_run(self, run_id: str) -> RunRecord:
        return self._store.get_run(run_id)

    def events(
        self, run_id: str, *, after_seq: int = 0, limit: int = DEFAULT_EVENT_PAGE_LIMIT
    ) -> EventPage:
        return self._store.events(run_id, after_seq=after_seq, limit=limit)

    # ---------------- kanonik yazma yolu ----------------

    def record_many(
        self,
        *,
        run_id: str,
        specs: tuple[RunEventSpec, ...] | list[RunEventSpec],
        expected_last_event_seq: int | None = None,
    ) -> tuple[tuple[RunEvent, ...], RunRecord]:
        """Atomically append a batch, then publish one coalesced latest-seq notice."""
        events, run = self._store.append_events(
            run_id=run_id,
            specs=specs,
            expected_last_event_seq=expected_last_event_seq,
        )
        self._bus.publish(EventNotice(run_id=run_id, latest_seq=events[-1].seq))
        return events, run

    def record(
        self,
        *,
        run_id: str,
        type: str,
        payload: dict[str, Any],
        schema_version: int = CURRENT_SCHEMA_VERSION,
        execution_id: str | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        source: str = "system",
        created_at: datetime | None = None,
        event_id: str | None = None,
        expected_last_event_seq: int | None = None,
    ) -> tuple[RunEvent, RunRecord]:
        """Bir event'i dayanıklı biçimde ekler, SONRA (ve YALNIZCA başarılıysa) canlı bir bildirim yayınlar."""
        event, run = self._store.append_event(
            run_id=run_id,
            type=type,
            payload=payload,
            schema_version=schema_version,
            execution_id=execution_id,
            turn_id=turn_id,
            item_id=item_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            source=source,
            created_at=created_at,
            event_id=event_id,
            expected_last_event_seq=expected_last_event_seq,
        )
        # BURADAN ÖNCEKİ satır (append_event) TAMAMEN COMMIT olmuştur —
        # publish() yalnızca bundan SONRA çağrılır. append_event bir istisna
        # fırlatırsa (RunNotFoundError/EventSequenceError/EventConflictError/
        # RunProjectionError/RunStoreError/...), publish() HİÇ ÇAĞRILMAZ.
        self._bus.publish(EventNotice(run_id=run_id, latest_seq=event.seq))
        return event, run

    def open_event_tail(self, run_id: str, *, after_seq: int = 0) -> "DurableEventTail":
        """Yarış içermeyen (race-free) canlı+dayanıklı bir event kuyruğu açar.

        KRİTİK sıralama: ÖNCE bus.subscribe(), SONRA Run'ın var olduğu
        doğrulanır (next_page() burada dayanıklı geçmişi HENÜZ SORGULAMAZ —
        yalnızca varlık doğrulanır). Bu sıra tersine çevrilirse (önce
        doğrula/replay, sonra subscribe), subscribe ile ilk sorgu arasında
        yayınlanan bir event kaybolabilir (lost-event window).

        Run bulunamazsa (veya doğrulama başka bir sebeple başarısız olursa),
        yeni açılan abonelik HEMEN kapatılır ki EventBus kaydında YETİM
        (asla kapatılmayacak) bir abonelik bırakılmasın.
        """
        if isinstance(after_seq, bool) or not isinstance(after_seq, int):
            raise EventValidationError(f"after_seq bir tam sayı olmalı: {after_seq!r}")
        if after_seq < 0:
            raise EventValidationError(f"after_seq >= 0 olmalı: {after_seq}")

        subscription = self._bus.subscribe(run_id)
        try:
            self._store.get_run(run_id)
        except Exception:
            subscription.close()
            raise
        return DurableEventTail(store=self._store, subscription=subscription, after_seq=after_seq)


class DurableEventTail:
    """Bir Run için durumlu, dayanıklı+canlı event kuyruğu.

    Kanonik gerçeği HER ZAMAN SQLite'tan (RunStore.events) okur; EventBus
    yalnızca "yeniden sorgula" sinyali verir. EventNotice.latest_seq'ten
    HİÇBİR ZAMAN event UYDURULMAZ — bayat/birleşmiş (coalesced) bir bildirim
    yalnızca fazladan bir SQLite sorgusuna yol açabilir; bu, doğruluk
    uğruna kabul edilebilir bir bedeldir.
    """

    def __init__(self, *, store: RunStore, subscription: EventSubscription, after_seq: int = 0):
        self._store = store
        self._subscription = subscription
        self._cursor = after_seq
        self._closed = False

    @property
    def run_id(self) -> str:
        return self._subscription.run_id

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def closed(self) -> bool:
        return self._closed

    def next_page(
        self, *, timeout: float | None = None, limit: int = DEFAULT_EVENT_PAGE_LIMIT
    ) -> EventPage | None:
        """Bir sonraki dayanıklı event sayfasını döndürür; (timeout içinde) yeni bir şey yoksa None döner.

        close() SERT bir kaynak sınırıdır: kapalı bir kuyrukta next_page()
        çağrılırsa, SQLite'ta okunmamış dayanıklı event'ler olsa BİLE,
        HİÇBİR sorgu yapılmadan HEMEN EventStreamClosedError fırlatılır.
        Kapalılık kontrolü, HER İTERASYONDA ilk store.events sorgusundan
        ÖNCE yapılır — bu yüzden hâlihazırda bir bildirim bekleyen (blocked)
        next_page() çağrısı, başka bir thread close() çağırdığında (ki bu,
        aboneliğin Condition'ını notify eder) PROMPT biçimde uyanır ve
        (backlog'u sorgulamadan) EventStreamClosedError ile çıkar.

        close() ile ZATEN çalışmakta olan bir SQLite sorgusu arasındaki bir
        yarış tam anlamıyla lineerleştirilebilir (linearizable) bir iptal
        DEĞİLDİR (o sorgu tamamlanabilir) — bu kabul edilebilir bir
        sınırlamadır; garanti edilen yalnızca: (a) ÖNCEDEN kapalı bir
        çağrının HER ZAMAN hemen hata vermesi ve (b) bloklu bir beklemenin
        close() ile PROMPT biçimde uyanması.

        DÖNGÜ: kapalıysa hemen hata ver -> SQLite'ı sorgula -> event varsa
        imleci ilerlet ve döndür -> kalan zaman aşımıyla abonelikte bekle ->
        uyanınca (zaman aşımı/bildirim/kapanma fark etmeksizin) döngü
        başına dönüp yeniden kapalılığı kontrol et.

        time.monotonic tabanlı sabit bir deadline kullanılır; böylece
        art arda gelen sahte/gereksiz uyanışlar çağıranın zaman aşımını
        UZATMAZ.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._closed:
                raise EventStreamClosedError(f"Event kuyruğu kapalı (run_id={self.run_id}).")

            page = self._store.events(self.run_id, after_seq=self._cursor, limit=limit)
            if page.events:
                self._cursor = page.events[-1].seq
                return page

            if deadline is None:
                remaining = None
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None

            # Dönüş değeri (EventNotice/None) KASITLI OLARAK yok sayılır: hiçbir
            # event EventNotice'ten UYDURULMAZ. Uyanış nedeni ne olursa olsun
            # (gerçek bildirim, zaman aşımı veya kapanma), döngü başına dönüp
            # önce kapalılığı, sonra (kapalı değilse) SQLite'ı yeniden kontrol eder.
            self._subscription.wait(timeout=remaining)

    def close(self) -> None:
        """İdempotenttir. SERT bir kaynak sınırıdır: bundan sonraki her
        next_page() çağrısı, okunmamış dayanıklı event olsa bile HEMEN
        EventStreamClosedError fırlatır (backlog DRAIN EDİLMEZ)."""
        if self._closed:
            return
        self._closed = True
        self._subscription.close()

    def __enter__(self) -> "DurableEventTail":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
