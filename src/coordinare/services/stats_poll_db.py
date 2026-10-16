from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping

BOARD_CHECK_KIND = "stats.board_check"
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


def record_board_checks(db: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> None:
    """Upsert redacted, UTC-normalized polls on the event store's writer thread.

    Only the hourly counters and last failure survive; no consumer needs the raw
    per-poll events. The caller owns the transaction, including rollback/retry.
    """
    def values() -> Iterable[tuple[str, str, int, str | None, str]]:
        for row in rows:
            failed = json.loads(row["data"]).get("success") is False
            yield (row["symphony"], row["ts"][:13] + ":00:00+00:00", int(failed),
                   row["ts"] if failed else None, row["message"] if failed else "")

    db.executemany(UPSERT_BOARD_CHECK, values())


def migrate_board_checks_batch(db: sqlite3.Connection, batch_size: int) -> int:
    """Move one bounded batch of legacy polls atomically, outside startup."""
    with db:
        rows = db.execute(
            "SELECT id,ts,symphony,message,data FROM events WHERE kind=? ORDER BY ts,id LIMIT ?",
            (BOARD_CHECK_KIND, batch_size),
        ).fetchall()
        record_board_checks(db, (
            {"ts": ts, "symphony": name, "message": message, "data": data}
            for _, ts, name, message, data in rows
        ))
        db.executemany("DELETE FROM events WHERE id=?", ((row[0],) for row in rows))
    return len(rows)
