"""run_runtime.schema — SQLite şema tanımı + PRAGMA user_version tabanlı migrasyon.

ORM veya harici migrasyon bağımlılığı KULLANILMAZ; şema sürümü doğrudan
`PRAGMA user_version` ile izlenir. Migrasyonlar (DDL dahil) tek bir SQLite
işleminde (BEGIN IMMEDIATE ... COMMIT) atomik olarak uygulanır — SQLite'ta
DDL de tam anlamıyla işlemseldir, bu yüzden yarım kalan bir migrasyon asla
kalıcı hale gelmez.

run_events.run_id -> runs.run_id kasıtlı olarak ON DELETE CASCADE KULLANMAZ:
bu kanonik, ekleme-only bir yürütme geçmişidir; bir Run'ın yanlışlıkla
silinmesi kendi olay tarihçesini SESSİZCE silmemelidir. Varsayılan
NO ACTION/RESTRICT semantiği (foreign_keys=ON ile) olay kaydı olan bir Run'ın
silinmesini reddeder. Şu an ortak bir "run sil" genel API'si yoktur.
"""

from __future__ import annotations

import sqlite3

from run_runtime.errors import RunStoreError

SCHEMA_VERSION = 1

_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY,
        project_root TEXT NOT NULL,
        prompt TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_tasks_project_root_created_at ON tasks(project_root, created_at DESC)",
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        status TEXT NOT NULL,
        phase TEXT NOT NULL,
        attempt INTEGER NOT NULL DEFAULT 1,
        retry_of_run_id TEXT REFERENCES runs(run_id),
        routing_json TEXT NOT NULL DEFAULT '{}',
        budget_json TEXT,
        workspace_snapshot_json TEXT,
        last_event_seq INTEGER NOT NULL DEFAULT 0,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        latency_s REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        error_code TEXT,
        error_message TEXT
    )
    """,
    "CREATE INDEX idx_runs_task_id_created_at ON runs(task_id, created_at DESC)",
    "CREATE INDEX idx_runs_status ON runs(status)",
    """
    CREATE TABLE run_events (
        event_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        seq INTEGER NOT NULL,
        type TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        execution_id TEXT,
        turn_id TEXT,
        item_id TEXT,
        causation_id TEXT,
        correlation_id TEXT,
        source TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        UNIQUE(run_id, seq)
    )
    """,
    # UNIQUE(run_id, seq) yukarıda zaten (run_id, seq) için bir destek indeksi
    # oluşturur; ikinci, aynı işi yapan bir CREATE INDEX EKLENMEZ.
    "CREATE INDEX idx_run_events_run_id_type ON run_events(run_id, type)",
)


def configure_connection(conn: sqlite3.Connection) -> None:
    """Her bağlantı açılışında uygulanması gereken PRAGMA'lar.

    journal_mode ve foreign_keys bir işlem (transaction) içindeyken
    değiştirilemez/no-op olur; bu yüzden migrate()'den ÖNCE, herhangi bir
    BEGIN açılmamışken çağrılmalıdır.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")


def migrate(conn: sqlite3.Connection) -> None:
    """Şemayı SCHEMA_VERSION'a taşır.

    Taze DB: user_version 0 -> 1. Zaten 1 ise no-op (idempotent). Bu ikilinin
    anladığından daha yeni bir user_version görülürse (örn. veritabanı daha
    yeni bir Imece IDE sürümü tarafından oluşturulmuşsa) RunStoreError ile
    başarısız olur — asla sessizce alt sürüme indirilmez veya üzerine
    yazılmaz.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current == SCHEMA_VERSION:
        return
    if current > SCHEMA_VERSION:
        raise RunStoreError(
            f"Veritabanı şeması bu sürümün anladığından daha yeni "
            f"(user_version={current}, desteklenen={SCHEMA_VERSION}); "
            "muhtemelen daha yeni bir Imece IDE sürümü tarafından oluşturuldu."
        )
    if current != 0:
        raise RunStoreError(f"Bilinmeyen ara şema sürümü: {current}")

    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _V1_STATEMENTS:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
