from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping

BOARD_CHECK_KIND = "stats.board_check"
MIGRATION_BATCH_SIZE = 16
MIGRATION_WRITE_SECONDS = 0.01
MIGRATION_YIELD_SECONDS = 0.001
CREATE_BOARD_CHECKS = """CREATE TABLE IF NOT EXISTS stats_board_checks (
    symphony TEXT NOT NULL,
    hour TEXT NOT NULL,
    polls INTEGER NOT NULL,
    failures INTEGER NOT NULL,
    last_error_at TEXT,
    last_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (symphony, hour)
) WITHOUT ROWID"""
UPSERT_BOARD_CHECK = """INSERT INTO stats_board_checks
    (symphony,hour,polls,failures,last_error_at,last_reason) VALUES (?,?,1,?,?,?)
    ON CONFLICT(symphony,hour) DO UPDATE SET
        polls=polls+1, failures=failures+excluded.failures,
        last_error_at=CASE WHEN excluded.last_error_at >= coalesce(last_error_at,'')
            THEN excluded.last_error_at ELSE last_error_at END,
        last_reason=CASE WHEN excluded.last_error_at >= coalesce(last_error_at,'')
            THEN excluded.last_reason ELSE last_reason END"""


def _board_check_values(rows: Iterable[Mapping[str, Any]]) -> Iterable[tuple[str, str, int, str | None, str]]:
    for row in rows:
        failed = json.loads(row["data"]).get("success") is False
        yield (row["symphony"], row["ts"][:13] + ":00:00+00:00", int(failed),
               row["ts"] if failed else None, row["message"] if failed else "")


def record_board_checks(db: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> None:
    """Upsert redacted, UTC-normalized polls on the event store's writer thread.

    Only the hourly counters and last failure survive; no consumer needs the raw
    per-poll events. The caller owns the transaction, including rollback/retry.
    """
    db.executemany(UPSERT_BOARD_CHECK, _board_check_values(rows))


def migrate_board_checks_batch(db: sqlite3.Connection, batch_size: int) -> int:
    """Commit a short slice, then yield with no transaction held.

    Parse legacy JSON before opening the write transaction. Each poll's upsert
    and deletion stay atomic, even when the time budget ends a slice early.
    """
    rows = db.execute(
        "SELECT id,ts,symphony,message,data FROM events WHERE kind=? ORDER BY ts,id LIMIT ?",
        (BOARD_CHECK_KIND, min(batch_size, MIGRATION_BATCH_SIZE)),
    ).fetchall()
    if not rows:
        return 0
    values = list(_board_check_values(
        {"ts": ts, "symphony": name, "message": message, "data": data}
        for _, ts, name, message, data in rows
    ))
    moved = 0
    deadline = time.monotonic() + MIGRATION_WRITE_SECONDS
    with db:
        for row, value in zip(rows, values, strict=True):
            db.execute(UPSERT_BOARD_CHECK, value)
            db.execute("DELETE FROM events WHERE id=?", (row[0],))
            moved += 1
            if time.monotonic() >= deadline:
                break
    time.sleep(MIGRATION_YIELD_SECONDS)
    return moved
