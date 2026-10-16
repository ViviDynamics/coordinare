from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from coordinare.services.event_store_query import (
    QUERY_PROGRESS_STEPS,
    QUERY_TIMEOUT_SECONDS,
    READ_BUSY_TIMEOUT_MS,
    EventQueryTooBroad,
)
from coordinare.services.stats_flow import RECENT_LIMIT
from coordinare.services.stats_measures import MAX_REASON_CHARS
from coordinare.services.stats_poll import BOARD_CHECK_KIND

if TYPE_CHECKING:
    from pathlib import Path

# Independent of the lifecycle/usage budget. At a 30 second poll interval this
# covers over 347 days per symphony, well beyond the longest rolling UI window.
MAX_BOARD_CHECK_ROWS = 1_000_000


def read_board_checks(
    path: Path, visible: frozenset[str], start: str | None, end: str,
) -> tuple[dict[str, Any], set[str]]:
    """Aggregate exact windowed counts in SQLite, returning only recent errors.

    Each symphony gets its own row bound and statement deadline. Older polls,
    hidden symphonies and unrelated event kinds cannot consume that allowance.
    No per-poll Python objects or shared event-read budget are used.
    """
    result: dict[str, Any] = {}
    limited: set[str] = set()
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
                checks, successes, errors = db.execute(
                    """WITH polls AS MATERIALIZED (
                        SELECT ts,message,json_extract(data,'$.success') AS success
                        FROM events WHERE kind=? AND symphony=? AND ts>=? AND ts<=?
                        ORDER BY ts DESC LIMIT ?
                    )
                    SELECT count(*), coalesce(sum(success=1),0),
                        (SELECT json_group_array(json_object('at',ts,'reason',substr(message,1,?)))
                         FROM (SELECT ts,message FROM polls WHERE success=0
                               ORDER BY ts DESC LIMIT ?))
                    FROM polls""",
                    (BOARD_CHECK_KIND, name, start or "", end, MAX_BOARD_CHECK_ROWS + 1,
                     MAX_REASON_CHARS, RECENT_LIMIT),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
                    raise EventQueryTooBroad from exc
                raise
            if checks > MAX_BOARD_CHECK_ROWS:
                limited.add(name)
            result[name] = {
                "checks": checks,
                "success_rate": successes / checks if checks and name not in limited else None,
                "recent_errors": json.loads(errors),
            }
    return result, limited
