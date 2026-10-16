from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services.stats_poll_db import CREATE_BOARD_CHECKS

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.services.event_store_settings import EventStoreConfig

logger = structlog.get_logger(__name__)

SCHEMA_VERSION = 5
BASELINE_INDEXES = (
    ("ts", "ts"),
    ("symphony_ts", "symphony, ts"),
    ("run_ts", "run_id, ts"),
    ("kind_ts", "kind, ts"),
)
FILTER_INDEXES = (
    ("card_ts", "card_id, ts"),
    ("source_ts", "source, ts"),
    ("level_ts", "level, ts"),
)
RETIRED_INDEXES = ("events_stage_ts",)
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
PERFORMER_INDEX = """CREATE UNIQUE INDEX IF NOT EXISTS events_performer_seq
    ON events (run_id, json_extract(data, '$.transcript_seq'))
    WHERE source='performer' AND run_id != ''
    AND json_type(data, '$.transcript_seq')='integer'"""
PERFORMER_REPLAY_GUARD = """CREATE TRIGGER IF NOT EXISTS events_performer_replay
    BEFORE INSERT ON events
    WHEN new.source='performer' AND new.run_id != ''
        AND json_type(new.data, '$.transcript_seq')='integer'
        AND EXISTS (SELECT 1 FROM events WHERE source='performer'
            AND run_id=new.run_id AND json_type(data, '$.transcript_seq')='integer'
            AND json_extract(data, '$.transcript_seq')=json_extract(new.data, '$.transcript_seq'))
    BEGIN SELECT RAISE(IGNORE); END"""
FTS_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, message) VALUES (new.id, new.message); END""",
    """CREATE TRIGGER IF NOT EXISTS events_fts_delete AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, message) VALUES ('delete', old.id, old.message); END""",
)


def _prepare_performer_migration(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS event_migration (
        id INTEGER PRIMARY KEY CHECK (id=1), last_id INTEGER NOT NULL,
        end_id INTEGER NOT NULL)""")
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='events_performer_seq'").fetchone()
    if exists:
        return
    end_id = db.execute("SELECT max(id) FROM events").fetchone()[0]
    if end_id is None:
        db.execute(PERFORMER_INDEX)
        return
    db.execute("INSERT OR IGNORE INTO event_migration VALUES (1,0,?)", (end_id,))
    db.execute("""CREATE TABLE IF NOT EXISTS event_migration_seen (
        run_id TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY (run_id, seq))""")
    db.execute(PERFORMER_REPLAY_GUARD)


def migrate_performer_batch(db: sqlite3.Connection, batch_size: int) -> tuple[int, bool]:
    """Scan at most one batch of legacy ids, preserving progress across restarts."""
    progress = db.execute("SELECT last_id,end_id FROM event_migration WHERE id=1").fetchone()
    if progress is None:
        return 0, False
    with db:
        rows = db.execute(
            """SELECT id,source,run_id,json_type(data,'$.transcript_seq'),
                json_extract(data,'$.transcript_seq') FROM events
                WHERE id > ? AND id <= ? ORDER BY id LIMIT ?""",
            (*progress, batch_size),
        ).fetchall()
        removed = _remove_replays(db, rows)
        last_id = rows[-1][0] if rows else progress[1]
        db.execute("UPDATE event_migration SET last_id=? WHERE id=1", (last_id,))
        active = last_id < progress[1]
        if not active:
            # The full index build is maintenance work too, never startup work.
            db.execute(PERFORMER_INDEX)
            db.execute("DROP TRIGGER events_performer_replay")
            db.execute("DROP TABLE event_migration_seen")
            db.execute("DELETE FROM event_migration WHERE id=1")
    return removed, active


def _remove_replays(db: sqlite3.Connection, rows: list[Any]) -> int:
    duplicates = []
    for row_id, source, run_id, seq_type, seq in rows:
        if source == "performer" and run_id and seq_type == "integer":
            inserted = db.execute("INSERT OR IGNORE INTO event_migration_seen VALUES (?,?)", (run_id, seq))
            if not inserted.rowcount:
                duplicates.append((row_id,))
    db.executemany("DELETE FROM events WHERE id=?", duplicates)
    return len(duplicates)


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
        db.execute(CREATE_BOARD_CHECKS)
        for name, columns in BASELINE_INDEXES:
            db.execute(f"CREATE INDEX IF NOT EXISTS events_{name} ON events ({columns})")
        _prepare_performer_migration(db)
        fts = _create_fts(db) if enable_fts else False
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return fts


def migrate_filter_index(db: sqlite3.Connection) -> bool:
    """Build one filter index per slice, committing progress in sqlite_master.

    A committed index survives restart; an interrupted build rolls back and is
    retried. Queries remain correct while any of these optional indexes is absent.
    Each build holds the writer for one index, so drain ingress between slices.
    """
    indexes = {row[1] for row in db.execute("PRAGMA index_list(events)")}
    for index in RETIRED_INDEXES:
        if index in indexes:
            _migrate_index(db, index, f"DROP INDEX {index}", "event_store.index_dropped")
            return True
    for name, columns in FILTER_INDEXES:
        index = f"events_{name}"
        if index in indexes:
            continue
        _migrate_index(
            db, index, f"CREATE INDEX IF NOT EXISTS {index} ON events ({columns})",
            "event_store.index_built",
        )
        return True
    return False


def _migrate_index(db: sqlite3.Connection, index: str, statement: str, event: str) -> None:
    started = time.monotonic()
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(statement)
    logger.info(event, index=index, duration_seconds=time.monotonic() - started)


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
        hours = db.execute(
            "SELECT symphony,hour FROM stats_board_checks WHERE hour < ? ORDER BY hour LIMIT ?",
            (cutoff[:13] + ":00:00+00:00", batch_size),
        ).fetchall()
        if not hours and not ids and path.stat().st_size > config.max_bytes:
            hours = db.execute(
                "SELECT symphony,hour FROM stats_board_checks ORDER BY hour LIMIT ?", (batch_size,),
            ).fetchall()
        db.executemany("DELETE FROM stats_board_checks WHERE symphony=? AND hour=?", hours)
    db.execute(f"PRAGMA incremental_vacuum({VACUUM_PAGES})").fetchall()
    return len(ids) + len(hours)
