from __future__ import annotations

import base64
import binascii
import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from coordinare.services.event_store_db import BUSY_TIMEOUT_MS

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_CURSOR_LENGTH = 512
EXACT_FILTERS = ("symphony", "card_id", "run_id", "stage", "source", "kind", "level")


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


def _decode_cursor(cursor: str) -> tuple[str, int]:
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise ValueError
    ts, row_id = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
    if not isinstance(ts, str) or type(row_id) is not int or row_id < 1:
        raise ValueError
    return utc_timestamp(ts), row_id


def _cursor_values(cursor: str) -> tuple[str, int]:
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
        return "id IN (SELECT rowid FROM events_fts WHERE events_fts MATCH ?)", '"' + text.replace(
            '"', '""',
        ) + '"'
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "message LIKE ? ESCAPE '\\'", f"%{escaped}%"


def read_events(
    path: Path, filters: EventFilters, cursor: str | None, limit: int,
) -> dict[str, Any]:
    if limit < 1:
        message = "event query limit must be positive"
        raise ValueError(message)
    limit = min(limit, MAX_LIMIT)
    if cursor:
        _cursor_values(cursor)
    # mode=ro never creates a database as a side effect of a query.
    with contextlib.closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        fts = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='events_fts'").fetchone())
        clauses, values = _conditions(filters, cursor, fts=fts)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit + 1)
        statement = " ".join(("SELECT * FROM events", where, "ORDER BY ts DESC, id DESC LIMIT ?"))
        rows = db.execute(statement, values).fetchall()
    items = [{**dict(row), "data": json.loads(row["data"])} for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([last["ts"], last["id"]]).encode(),
        ).decode()
    return {"events": items, "cursor": next_cursor}
