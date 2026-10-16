from __future__ import annotations

import contextlib
import sqlite3
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from coordinare.services.event_store_query import (
    QUERY_PROGRESS_STEPS,
    QUERY_TIMEOUT_SECONDS,
    READ_BUSY_TIMEOUT_MS,
    EventQueryTooBroad,
)
from coordinare.services.stats_flow import RECENT_LIMIT
from coordinare.services.stats_poll_db import BOARD_CHECK_KIND

if TYPE_CHECKING:
    from pathlib import Path

# An independent per-symphony bound for all-retained reads. Rolling windows
# touch at most 720 rows, using the (symphony, hour) primary key.
MAX_BOARD_CHECK_ROWS = 1_000_000


def unavailable_board_checks(reason: str) -> dict[str, Any]:
    return {"checks": None, "success_rate": None, "recent_errors": [], "reason": reason}


def _history_reasons(
    db: sqlite3.Connection, name: str, start_hour: str, end_hour: str,
) -> list[str]:
    reasons = []
    retention_days = db.execute("SELECT retention_days FROM stats_board_check_policy WHERE id=1").fetchone()[0]
    if start_hour and (
        datetime.fromisoformat(end_hour) - datetime.fromisoformat(start_hour)
        > timedelta(days=retention_days)
    ):
        reasons.append(f"event_store.retention_days ({retention_days} days) is shorter than the requested window")
    pruned = db.execute(
        """SELECT 1 FROM stats_board_check_pruning
        WHERE symphony=? AND ((last_hour>=? AND last_hour<?)
            OR (previous_hour>=? AND previous_hour<? AND previous_hour!=''))""",
        (name, start_hour, end_hour, start_hour, end_hour),
    ).fetchone()
    if pruned:
        reasons.append("event_store.max_bytes size cap removed hourly board check totals inside the requested window")
    return reasons


def read_board_checks(
    path: Path, visible: frozenset[str], start: str | None, end: str,
) -> tuple[dict[str, Any], set[str]]:
    """Read exact counters for completed UTC hours in the selected window.

    Excluding the incomplete current hour keeps a 30-day read to 720 rows.
    Each symphony has an independent deadline and failure result.
    """
    result: dict[str, Any] = {}
    limited: set[str] = set()
    end_hour = datetime.fromisoformat(end).replace(minute=0, second=0, microsecond=0).isoformat()
    start_hour = datetime.fromisoformat(start).replace(minute=0, second=0, microsecond=0).isoformat() if start else ""
    with contextlib.closing(sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=QUERY_TIMEOUT_SECONDS,
    )) as db:
        db.execute(f"PRAGMA busy_timeout={READ_BUSY_TIMEOUT_MS}")
        db.execute("BEGIN")
        for name in sorted(visible):
            deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS

            def expired(cutoff: float = deadline) -> int:
                return int(time.monotonic() >= cutoff)

            db.set_progress_handler(expired, QUERY_PROGRESS_STEPS)
            try:
                pending = db.execute(
                    "SELECT 1 FROM events WHERE kind=? AND symphony=? LIMIT 1",
                    (BOARD_CHECK_KIND, name),
                ).fetchone()
                if pending:
                    result[name] = unavailable_board_checks("board_check_rollup_pending")
                    limited.add(name)
                    continue
                rows = db.execute(
                    """SELECT polls,failures,last_error_at,last_reason
                    FROM stats_board_checks WHERE symphony=? AND hour>=? AND hour<?
                    ORDER BY hour DESC LIMIT ?""",
                    (name, start_hour, end_hour, MAX_BOARD_CHECK_ROWS + 1),
                ).fetchall()
                if len(rows) > MAX_BOARD_CHECK_ROWS:
                    raise EventQueryTooBroad
                reasons = _history_reasons(db, name, start_hour, end_hour)
            except sqlite3.Error as exc:
                limited.add(name)
                result[name] = unavailable_board_checks("board_check_read_unavailable: " + type(exc).__name__)
                continue
            checks = sum(row[0] for row in rows)
            failures = sum(row[1] for row in rows)
            result[name] = {
                "checks": checks,
                "success_rate": (checks - failures) / checks if checks else None,
                "recent_errors": [
                    {"at": at, "reason": reason} for _, _, at, reason in rows if at is not None
                ][:RECENT_LIMIT],
            }
            if reasons:
                result[name].update(partial=True, partial_reasons=reasons)
    return result, limited
