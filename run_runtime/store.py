"""run_runtime.store — SQLite tabanlı RunStore.

Task/Run CRUD ve olay ekleme (append_event) burada yaşar. Kanonik yürütme
geçmişi run_events tablosudur (ekleme-only); runs tablosu bu akışın
hızlı-okuma izdüşümüdür (bkz. run_runtime.projector). Bir event'in eklenmesi
ile Run izdüşümünün güncellenmesi HER ZAMAN aynı SQLite işleminde (BEGIN
IMMEDIATE ... COMMIT) atomik olarak gerçekleşir — dayanıklı bir event, kendi
projeksiyon güncellemesi olmadan ASLA var olamaz.

Her işlem kendi kısa ömürlü bağlantısını açar (bkz. _connect); tek bir
process-global, thread-affine bağlantı PAYLAŞILMAZ (check_same_thread=False
KULLANILMAZ) — böylece store, gelecekteki Qt worker/araç thread'leri için
güvenli kalır. Eşzamanlı yazarlar arasındaki sıralama, BEGIN IMMEDIATE'ın
aldığı SQLite dosya kilidiyle sağlanır.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from run_runtime import schema
from run_runtime.errors import (
    EventConflictError,
    EventSequenceError,
    EventValidationError,
    RunNotFoundError,
    RunStoreError,
    TaskNotFoundError,
)
from run_runtime.events import CURRENT_SCHEMA_VERSION, RunEvent, build_event, validate_event_payload
from run_runtime.jsonutil import canonical_dict_copy, canonical_json_dumps
from run_runtime.models import RunPhase, RunRecord, RunStatus, TaskRecord, new_run_id, new_task_id, utcnow
from run_runtime.projector import project_run

DEFAULT_EVENT_PAGE_LIMIT = 50
MAX_EVENT_PAGE_LIMIT = 200

_ACTIVE_STATUSES = (RunStatus.CREATED.value, RunStatus.RUNNING.value, RunStatus.WAITING_USER.value)

# Bu store'un yazdığı satırların KENDİ bozduğu/decode edemediği durumları
# (JSON decode, enum dönüşümü, zaman damgası ayrıştırma) tekdüze biçimde
# RunStoreError'a çevirmek için kullanılan hata kümesi (bkz. _decode_row).
_ROW_DECODE_ERRORS = (ValueError, TypeError, json.JSONDecodeError)


@dataclasses.dataclass(frozen=True, slots=True)
class EventPage:
    events: tuple[RunEvent, ...]
    has_more: bool


def _iso(dt: datetime | None) -> str | None:
    """Kalıcı hale getirme (yazma) tarafı: yalnızca gerçekten aware bir datetime kabul eder."""
    if dt is None:
        return None
    if dt.utcoffset() is None:
        raise ValueError("naive datetime kalıcı hale getirilemez; tz-aware UTC gerekli.")
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None, *, field: str) -> datetime | None:
    """Okuma tarafı: saklı bir naive zaman damgasını SESSİZCE UTC saymaz — reddeder.

    Bu, v1 kanonik veritabanı için geçersiz bir kalıcı durumdur; çağıran
    (bkz. _decode_row) bunu tipli bir RunStoreError'a çevirir.
    """
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field}: geçersiz zaman damgası: {value!r} ({exc})") from exc
    if dt.utcoffset() is None:
        raise ValueError(f"{field}: saklı zaman damgası naive (tz-aware değil): {value!r}")
    return dt.astimezone(timezone.utc)


def _dumps(value: dict[str, Any]) -> str:
    return canonical_json_dumps(value)


def _decode_json_dict(value: str | None, *, field: str) -> dict[str, Any] | None:
    """Saklı JSON metnini KANONİK bir dict'e çözer.

    `_loads(value) or {}` gibi bir örüntü KASITLI OLARAK KULLANILMAZ: bu,
    '[]'/'""'/'false' gibi yanlış şekilleri sessizce {}'e çevirebilir. Burada
    şekil açıkça doğrulanır; dict OLMAYAN bir sonuç ValueError'dur.

    `json.loads` standart-dışı sabitleri (NaN, Infinity, -Infinity) SESSİZCE
    kabul eder — bu yüzden decode edilen değer AYRICA jsonutil'in kanonik JSON
    sözleşmesinden (canonical_dict_copy) geçirilir: tek kanonik JSON kaynağı
    hep jsonutil'dir, burada ayrı bir doğrulama mantığı TEKRARLANMAZ. Kanonik
    sözleşme ihlali (EventValidationError), bu fonksiyonun döndürdüğü HER ŞEYİN
    sözleşmeyi karşıladığını garanti edebilmek için ValueError'a çevrilir —
    böylece çağıranlar tek tip bozuk-satır işleme yolunu (_ROW_DECODE_ERRORS)
    kullanabilir ve yanlış katmandan (EventValidationError) bir hata asla
    RunStore'un genel okuma API'sinden dışarı sızmaz.
    """
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field}: geçersiz JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{field}: dict bekleniyordu, alındı: {type(decoded).__name__}")
    try:
        return canonical_dict_copy(decoded, field=field)
    except EventValidationError as exc:
        raise ValueError(f"{field}: kanonik JSON sözleşmesini ihlal ediyor: {exc}") from exc


@contextmanager
def _transaction(conn: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        project_root=row["project_root"],
        prompt=row["prompt"],
        created_at=_parse_iso(row["created_at"], field="created_at"),
    )


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    routing = _decode_json_dict(row["routing_json"], field="routing_json")
    if routing is None:
        raise ValueError("routing_json NULL olamaz.")
    return RunRecord(
        run_id=row["run_id"],
        task_id=row["task_id"],
        status=RunStatus(row["status"]),
        phase=RunPhase(row["phase"]),
        attempt=row["attempt"],
        retry_of_run_id=row["retry_of_run_id"],
        routing=routing,
        budget=_decode_json_dict(row["budget_json"], field="budget_json"),
        workspace_snapshot=_decode_json_dict(row["workspace_snapshot_json"], field="workspace_snapshot_json"),
        last_event_seq=row["last_event_seq"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        total_tokens=row["total_tokens"],
        cost_usd=row["cost_usd"],
        latency_s=row["latency_s"],
        created_at=_parse_iso(row["created_at"], field="created_at"),
        started_at=_parse_iso(row["started_at"], field="started_at"),
        finished_at=_parse_iso(row["finished_at"], field="finished_at"),
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _event_from_row(row: sqlite3.Row) -> RunEvent:
    payload = _decode_json_dict(row["payload_json"], field="payload_json")
    if payload is None:
        raise ValueError("payload_json NULL olamaz.")
    return RunEvent(
        event_id=row["event_id"],
        run_id=row["run_id"],
        seq=row["seq"],
        type=row["type"],
        schema_version=row["schema_version"],
        created_at=_parse_iso(row["created_at"], field="created_at"),
        execution_id=row["execution_id"],
        turn_id=row["turn_id"],
        item_id=row["item_id"],
        causation_id=row["causation_id"],
        correlation_id=row["correlation_id"],
        source=row["source"],
        payload=payload,
    )


def _run_params(record: RunRecord) -> tuple:
    return (
        record.run_id, record.task_id, record.status.value, record.phase.value,
        record.attempt, record.retry_of_run_id,
        _dumps(record.routing),
        _dumps(record.budget) if record.budget is not None else None,
        _dumps(record.workspace_snapshot) if record.workspace_snapshot is not None else None,
        record.last_event_seq,
        record.prompt_tokens, record.completion_tokens, record.total_tokens,
        record.cost_usd, record.latency_s,
        _iso(record.created_at), _iso(record.started_at), _iso(record.finished_at),
        record.error_code, record.error_message,
    )


def _insert_run(conn: sqlite3.Connection, record: RunRecord) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, task_id, status, phase, attempt, retry_of_run_id, "
        "routing_json, budget_json, workspace_snapshot_json, last_event_seq, "
        "prompt_tokens, completion_tokens, total_tokens, cost_usd, latency_s, "
        "created_at, started_at, finished_at, error_code, error_message) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        _run_params(record),
    )


def _update_run(conn: sqlite3.Connection, record: RunRecord) -> None:
    conn.execute(
        "UPDATE runs SET status=?, phase=?, attempt=?, retry_of_run_id=?, "
        "routing_json=?, budget_json=?, workspace_snapshot_json=?, last_event_seq=?, "
        "prompt_tokens=?, completion_tokens=?, total_tokens=?, cost_usd=?, latency_s=?, "
        "started_at=?, finished_at=?, error_code=?, error_message=? WHERE run_id=?",
        (
            record.status.value, record.phase.value, record.attempt, record.retry_of_run_id,
            _dumps(record.routing),
            _dumps(record.budget) if record.budget is not None else None,
            _dumps(record.workspace_snapshot) if record.workspace_snapshot is not None else None,
            record.last_event_seq,
            record.prompt_tokens, record.completion_tokens, record.total_tokens,
            record.cost_usd, record.latency_s,
            _iso(record.started_at), _iso(record.finished_at),
            record.error_code, record.error_message,
            record.run_id,
        ),
    )


class RunStore:
    """Task/Run/Event kalıcılığı için tek giriş noktası."""

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            schema.configure_connection(conn)
            schema.migrate(conn)
        except Exception:
            conn.close()
            raise
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # ---------------- tasks ----------------

    def create_task(
        self,
        *,
        project_root: str,
        prompt: str,
        task_id: str | None = None,
        created_at: datetime | None = None,
    ) -> TaskRecord:
        record = TaskRecord(
            task_id=task_id or new_task_id(),
            project_root=project_root,
            prompt=prompt,
            created_at=created_at or utcnow(),
        )
        try:
            with self._session() as conn, _transaction(conn, immediate=True):
                conn.execute(
                    "INSERT INTO tasks (task_id, project_root, prompt, created_at) VALUES (?,?,?,?)",
                    (record.task_id, record.project_root, record.prompt, _iso(record.created_at)),
                )
        except sqlite3.Error as exc:
            raise RunStoreError(f"Görev oluşturulamadı: {exc}") from exc
        return record

    def get_task(self, task_id: str) -> TaskRecord:
        try:
            with self._session() as conn:
                row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        except sqlite3.Error as exc:
            raise RunStoreError(f"Görev okunamadı: {exc}") from exc
        if row is None:
            raise TaskNotFoundError(task_id)
        try:
            return _task_from_row(row)
        except _ROW_DECODE_ERRORS as exc:
            raise RunStoreError(f"Görev satırı bozuk (task_id={task_id}): {exc}") from exc

    # ---------------- runs ----------------

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
        """Yeni bir Run'ı status=created, phase=created, last_event_seq=0 ile oluşturur."""
        record = RunRecord.new(
            run_id=run_id or new_run_id(),
            task_id=task_id,
            attempt=attempt,
            retry_of_run_id=retry_of_run_id,
            routing=routing,
            budget=budget,
            workspace_snapshot=workspace_snapshot,
            created_at=created_at,
        )
        try:
            with self._session() as conn, _transaction(conn, immediate=True):
                if conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone() is None:
                    raise TaskNotFoundError(task_id)
                _insert_run(conn, record)
        except sqlite3.Error as exc:
            raise RunStoreError(f"Koşu oluşturulamadı: {exc}") from exc
        return record

    def get_run(self, run_id: str) -> RunRecord:
        try:
            with self._session() as conn:
                row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        except sqlite3.Error as exc:
            raise RunStoreError(f"Koşu okunamadı: {exc}") from exc
        if row is None:
            raise RunNotFoundError(run_id)
        try:
            return _run_from_row(row)
        except _ROW_DECODE_ERRORS as exc:
            raise RunStoreError(f"Koşu satırı bozuk (run_id={run_id}): {exc}") from exc

    # ---------------- events ----------------

    def append_event(
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
        """Bir event'i DAYANIKLI biçimde ekler ve Run izdüşümünü ATOMİK olarak günceller.

        BEGIN IMMEDIATE ile açılan tek bir işlemde: mevcut Run + last_event_seq
        okunur, event seq = last_event_seq + 1 olarak atanır, event satırı
        yazılır ve projeksiyon güncellenir; herhangi bir adım başarısız olursa
        TÜMÜ geri alınır (ROLLBACK) — kalıcı bir event asla karşılık gelen bir
        projeksiyon güncellemesi olmadan var olamaz ve projeksiyon sırası asla
        dayanıklı event akışının ÖNÜNE geçemez.

        expected_last_event_seq verilirse (İYİMSER ÖN KOŞUL/optimistic
        precondition), mevcut Run yüklendikten HEMEN SONRA — event
        oluşturulmadan/yazılmadan ve projeksiyon güncellenmeden ÖNCE —
        current.last_event_seq ile karşılaştırılır. Uyuşmazlık, mevcut SQLite
        serileştirmesinin (BEGIN IMMEDIATE) veya UNIQUE(run_id, seq)
        kısıtının YERİNİ TUTMAZ; yalnızca bir çağıranın ELİNDEKİ anlık
        görüntünün bayat olup olmadığını (örn. kurtarma taramasından beri
        başka bir üretici ilerlemiş mi) tespit eder — bkz. run_runtime.recovery.
        """
        validate_event_payload(payload)  # DB'ye hiç dokunmadan erken reddet
        if expected_last_event_seq is not None:
            if isinstance(expected_last_event_seq, bool) or not isinstance(expected_last_event_seq, int):
                raise EventValidationError(
                    f"expected_last_event_seq bir tam sayı olmalı: {expected_last_event_seq!r}"
                )
            if expected_last_event_seq < 0:
                raise EventValidationError(
                    f"expected_last_event_seq >= 0 olmalı: {expected_last_event_seq}"
                )
        event: RunEvent | None = None
        updated: RunRecord | None = None
        try:
            with self._session() as conn, _transaction(conn, immediate=True):
                row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                if row is None:
                    raise RunNotFoundError(run_id)
                try:
                    current = _run_from_row(row)
                except _ROW_DECODE_ERRORS as exc:
                    raise RunStoreError(f"Koşu satırı bozuk (run_id={run_id}): {exc}") from exc

                if (
                    expected_last_event_seq is not None
                    and current.last_event_seq != expected_last_event_seq
                ):
                    # Hiçbir event oluşturulmadı/yazılmadı, projeksiyon dokunulmadı;
                    # ROLLBACK bu SELECT'ten başka bir şey geri almayacak.
                    raise EventSequenceError(
                        f"Bayat expected_last_event_seq (run_id={run_id}): "
                        f"beklenen={expected_last_event_seq}, gerçek={current.last_event_seq}"
                    )

                next_seq = current.last_event_seq + 1

                event = build_event(
                    run_id=run_id,
                    seq=next_seq,
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
                )

                # Bilinen event türleri için domain durumunu günceller; bilinmeyen
                # türler projeksiyonu değiştirmeden bırakır (project_run pure).
                projected = project_run(current, event)
                updated = dataclasses.replace(projected, last_event_seq=event.seq)

                try:
                    conn.execute(
                        "INSERT INTO run_events (event_id, run_id, seq, type, schema_version, "
                        "created_at, execution_id, turn_id, item_id, causation_id, correlation_id, "
                        "source, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            event.event_id, event.run_id, event.seq, event.type, event.schema_version,
                            _iso(event.created_at), event.execution_id, event.turn_id, event.item_id,
                            event.causation_id, event.correlation_id, event.source,
                            _dumps(event.payload),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    # PRIMARY KEY(event_id) veya UNIQUE(run_id, seq) çakışması olabilir —
                    # ikisini de kapsayan tek, genel bir tipli hata.
                    raise EventConflictError(
                        f"Event eklenemedi: event_id veya (run_id, seq) çakışması "
                        f"(run_id={run_id} seq={next_seq} event_id={event.event_id}): {exc}"
                    ) from exc

                _update_run(conn, updated)
        except sqlite3.Error as exc:
            raise RunStoreError(f"Event eklenemedi: {exc}") from exc
        assert event is not None and updated is not None  # yalnızca başarı yolunda buraya gelinir
        return event, updated

    def events(
        self, run_id: str, *, after_seq: int = 0, limit: int = DEFAULT_EVENT_PAGE_LIMIT
    ) -> EventPage:
        """`after_seq` HARİÇ tutulur: after_seq=50 -> seq=51'den başlayan sayfa döner.

        Sıralama HER ZAMAN artan seq'e göredir (zaman damgası veya UUID'e göre
        DEĞİL). limit varsayılan 50, sert üst sınır 200'dür.
        """
        if limit <= 0:
            raise RunStoreError(f"limit pozitif olmalı: {limit}")
        limit = min(limit, MAX_EVENT_PAGE_LIMIT)
        try:
            with self._session() as conn:
                if conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                    raise RunNotFoundError(run_id)
                rows = conn.execute(
                    "SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                    (run_id, after_seq, limit + 1),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RunStoreError(f"Event'ler okunamadı: {exc}") from exc
        has_more = len(rows) > limit
        try:
            events = tuple(_event_from_row(r) for r in rows[:limit])
        except _ROW_DECODE_ERRORS as exc:
            raise RunStoreError(f"Event satırı bozuk (run_id={run_id}): {exc}") from exc
        return EventPage(events=events, has_more=has_more)

    def list_runs(
        self,
        *,
        task_id: str | None = None,
        project_root: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        """Koşuları en yeniden en eskiye sıralı döndürür.

        `task_id` ve `project_root` BİRLİKTE verilemez (belirsiz filtre
        kombinasyonu) — bu tipli bir RunStoreError ile reddedilir; çağıran
        hangi filtreyi kastettiğini AÇIKÇA seçmelidir. `project_root`,
        tasks.project_root üzerinden bir JOIN ile filtreler; yeni bir tablo/
        indeks/şema migrasyonu GEREKMEZ.
        """
        if task_id is not None and project_root is not None:
            raise RunStoreError(
                "list_runs: task_id ve project_root birlikte belirtilemez (belirsiz filtre)."
            )
        try:
            with self._session() as conn:
                if project_root is not None:
                    rows = conn.execute(
                        "SELECT r.* FROM runs r JOIN tasks t ON t.task_id = r.task_id "
                        "WHERE t.project_root = ? ORDER BY r.created_at DESC LIMIT ?",
                        (project_root, limit),
                    ).fetchall()
                elif task_id is not None:
                    rows = conn.execute(
                        "SELECT * FROM runs WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
                        (task_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
                    ).fetchall()
        except sqlite3.Error as exc:
            raise RunStoreError(f"Koşular listelenemedi: {exc}") from exc
        try:
            return [_run_from_row(r) for r in rows]
        except _ROW_DECODE_ERRORS as exc:
            raise RunStoreError(f"Koşu satırı bozuk: {exc}") from exc

    def active_runs(self) -> list[RunRecord]:
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        try:
            with self._session() as conn:
                rows = conn.execute(
                    f"SELECT * FROM runs WHERE status IN ({placeholders}) ORDER BY created_at DESC",
                    _ACTIVE_STATUSES,
                ).fetchall()
        except sqlite3.Error as exc:
            raise RunStoreError(f"Etkin koşular listelenemedi: {exc}") from exc
        try:
            return [_run_from_row(r) for r in rows]
        except _ROW_DECODE_ERRORS as exc:
            raise RunStoreError(f"Koşu satırı bozuk: {exc}") from exc
