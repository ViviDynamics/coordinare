from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.services.event_store_settings import EventStoreConfig

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 1000
VACUUM_PAGES = 256
COLUMNS = (
    "ts",
    "symphony",
    "card_id",
    "run_id",
    "stage",
    "source",
    "kind",
    "level",
    "message",
    "data",
)
CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symphony TEXT NOT NULL DEFAULT '',
    card_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'daemon',
    kind TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}'
)
"""
FTS_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, message) VALUES (new.id, new.message); END""",
    """CREATE TRIGGER IF NOT EXISTS events_fts_delete AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, message) VALUES ('delete', old.id, old.message); END""",
)


def _create_fts(db: sqlite3.Connection) -> bool:
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='events_fts'").fetchone()
    try:
        db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(message, content='events', content_rowid='id')",
        )
    except sqlite3.OperationalError:
        return False
    for statement in FTS_TRIGGERS:
        db.execute(statement)
    if not exists:
        db.execute("INSERT INTO events_fts(events_fts) VALUES ('rebuild')")
    return True


def create_schema(db: sqlite3.Connection, *, enable_fts: bool = True) -> bool:
    """Migrate independently of state_store. Never downgrade an unknown schema."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        message = "event store schema is newer than this daemon"
        raise ValueError(message)
    # NFS cannot safely use WAL shared-memory locking. One daemon writer uses
    # the rollback journal instead. Set auto_vacuum before creating any tables.
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if version == 0:
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(CREATE_TABLE)
        for name, columns in (
            ("ts", "ts"),
            ("symphony_ts", "symphony, ts"),
            ("run_ts", "run_id, ts"),
            ("kind_ts", "kind, ts"),
        ):
            db.execute(f"CREATE INDEX IF NOT EXISTS events_{name} ON events ({columns})")
        fts = _create_fts(db) if enable_fts else False
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return fts


def disk_stats(db: sqlite3.Connection, path: Path, rows: int | None = None) -> dict[str, Any]:
    if rows is None:
        rows = db.execute("SELECT count(*) FROM events").fetchone()[0]
    oldest = db.execute("SELECT min(ts) FROM events").fetchone()[0]
    return {"size_bytes": path.stat().st_size, "rows": rows, "oldest_ts": oldest}


def prune_batch(
    db: sqlite3.Connection, path: Path, config: EventStoreConfig, batch_size: int,
) -> int:
    """One bounded deletion and incremental page reclamation on the writer thread."""
    cutoff = (datetime.now(UTC) - timedelta(days=config.retention_days)).isoformat()
    with db:
        ids = db.execute(
            "SELECT id FROM events WHERE ts < ? ORDER BY ts, id LIMIT ?", (cutoff, batch_size),
        ).fetchall()
        if not ids and path.stat().st_size > config.max_bytes:
            ids = db.execute(
                "SELECT id FROM events ORDER BY ts, id LIMIT ?", (batch_size,),
            ).fetchall()
        db.executemany("DELETE FROM events WHERE id=?", ids)
    # SQLite incremental_vacuum can yield after reclaiming just one page.
    # Step explicitly, bounding the work even on a large mostly-empty file.
    for _ in range(VACUUM_PAGES):
        if not db.execute("PRAGMA freelist_count").fetchone()[0]:
            break
        db.execute("PRAGMA incremental_vacuum(1)").fetchall()
    return len(ids)
