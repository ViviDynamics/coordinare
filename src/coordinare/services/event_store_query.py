from __future__ import annotations

import base64
import binascii
import contextlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_CURSOR_LENGTH = 512
EXACT_FILTERS = ("symphony", "card_id", "run_id", "stage", "source", "kind", "level")
QUERY_TIMEOUT_SECONDS = 0.250
READ_BUSY_TIMEOUT_MS = 50
QUERY_PROGRESS_STEPS = 1000
QUERY_TOO_BROAD_MESSAGE = "query too broad, narrow the filters"


class EventQueryTooBroad(sqlite3.OperationalError):
    """The statement exceeded its read budget and released its transaction."""

    def __init__(self) -> None:
        super().__init__(QUERY_TOO_BROAD_MESSAGE)


@dataclass(frozen=True)
class EventFilters:
    start: str | None = None
    end: str | None = None
    symphony: str | None = None
    card_id: str | None = None
    run_id: str | None = None
    stage: str | None = None
    source: str | None = None
    kind: str | None = None
    level: str | None = None
    text: str | None = None


def utc_timestamp(value: str) -> str:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        message = "event timestamps must include a timezone"
        raise ValueError(message)
    return stamp.astimezone(UTC).isoformat()


def _decode_cursor(cursor: str) -> tuple[str | None, int]:
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise ValueError
    ts, row_id = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
    if (ts is not None and not isinstance(ts, str)) or type(row_id) is not int or row_id < 1:
        raise ValueError
    return utc_timestamp(ts) if ts is not None else None, row_id


def _cursor_values(cursor: str) -> tuple[str | None, int]:
    try:
        return _decode_cursor(cursor)
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        message = "invalid event cursor"
        raise ValueError(message) from exc


def _conditions(
    filters: EventFilters, cursor: str | None, *, fts: bool,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    for column in EXACT_FILTERS:
        value = getattr(filters, column)
        if value is not None:
            clauses.append(f"{column} = ?")
            values.append(value)
    for value, comparison in ((filters.start, ">="), (filters.end, "<=")):
        if value is not None:
            clauses.append(f"ts {comparison} ?")
            values.append(utc_timestamp(value))
    if cursor:
        ts, row_id = _cursor_values(cursor)
        if filters.text:
            clauses.append("events_fts.rowid < ?" if fts else "id < ?")
            values.append(row_id)
        else:
            if ts is None:
                message = "text search cursor requires a text filter"
                raise ValueError(message)
            clauses.append("(ts, id) < (?, ?)")
            values.extend((ts, row_id))
    if filters.text:
        clause, value = _text_condition(filters.text, fts=fts)
        clauses.append(clause)
        values.append(value)
    return clauses, values


def _text_condition(text: str, *, fts: bool) -> tuple[str, str]:
    if fts:
        # Treat input as a literal phrase, never FTS operators or SQL syntax.
        return "events_fts MATCH ?", '"' + text.replace(
            '"', '""',
        ) + '"'
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "message LIKE ? ESCAPE '\\'", f"%{escaped}%"


def _read_rows(
    path: Path, filters: EventFilters, cursor: str | None, limit: int,
) -> list[sqlite3.Row]:
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    # mode=ro never creates a database as a side effect of a query.
    with contextlib.closing(sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=QUERY_TIMEOUT_SECONDS,
    )) as db:
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={READ_BUSY_TIMEOUT_MS}")
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), QUERY_PROGRESS_STEPS)
        db.execute("BEGIN")
        fts = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='events_fts'").fetchone())
        clauses, values = _conditions(filters, cursor, fts=fts)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit + 1)
        if filters.text and fts:
            statement = " ".join((
                "SELECT events.* FROM events_fts JOIN events ON events.id = events_fts.rowid",
                where, "ORDER BY events_fts.rowid DESC LIMIT ?",
            ))
        else:
            order = "id DESC" if filters.text else "ts DESC, id DESC"
            # A filter index could otherwise force a sort of every LIKE match.
            select = "SELECT * FROM events NOT INDEXED" if filters.text else "SELECT * FROM events"
            statement = " ".join((select, where, f"ORDER BY {order} LIMIT ?"))
        return db.execute(statement, values).fetchall()


def read_events(
    path: Path, filters: EventFilters, cursor: str | None, limit: int,
) -> dict[str, Any]:
    if limit < 1:
        message = "event query limit must be positive"
        raise ValueError(message)
    limit = min(limit, MAX_LIMIT)
    if cursor:
        _cursor_values(cursor)
    try:
        rows = _read_rows(path, filters, cursor, limit)
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT:
            raise EventQueryTooBroad from exc
        raise
    items = [{**dict(row), "data": json.loads(row["data"])} for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([None if filters.text else last["ts"], last["id"]]).encode(),
        ).decode()
    return {"events": items, "cursor": next_cursor}


def validate_event_cursor(cursor: str) -> None:
    """Validate an external cursor without touching the database."""
    _cursor_values(cursor)
