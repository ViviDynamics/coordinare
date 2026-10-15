from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from coordinare.services.event_store_query import (
    QUERY_PROGRESS_STEPS,
    QUERY_TIMEOUT_SECONDS,
    READ_BUSY_TIMEOUT_MS,
    EventQueryTooBroad,
)
from coordinare.services.run_history import retained_runs
from coordinare.services.run_transcript import transcript_path
from coordinare.services.run_viewer import RunLookupError, RunRef, RunRefusedError, open_run
from coordinare.services.token_usage import TokenLedger

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.services.token_usage import ModelTotals

MAX_RUNS = 2000
MAX_USAGE_BYTES = 16 * 1024 * 1024
MAX_EVENT_BYTES = 16 * 1024 * 1024
MAX_EVENT_ROWS = 10000
MAX_LINE_BYTES = 1024 * 1024
SOURCE_TIMEOUT_SECONDS = 1.0
LIFECYCLE_KIND = "card_lifecycle"
EVENT_KINDS = (LIFECYCLE_KIND, "blocked", "system_error", "cancelled")


@dataclass
class StatsSources:
    runs: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, ModelTotals] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _usage(
    root: Path,
    row: dict[str, Any],
    remaining: int,
    deadline: float,
) -> tuple[list[dict[str, Any]] | None, int]:
    """Only usage is parsed; the existing ledger owns accounting and precedence."""
    ref = RunRef(symphony=row["symphony"], card=row["card"], run=row["run"])
    path = transcript_path(root, symphony=ref.symphony, card_id=ref.card, run_id=ref.run)
    records: list[dict[str, Any]] = []
    consumed = 0
    try:
        with open_run(path, ref) as handle:
            while consumed < remaining and time.monotonic() < deadline:
                line = handle.readline(min(MAX_LINE_BYTES, remaining - consumed) + 1)
                consumed += len(line)
                if not line:
                    return records, consumed
                if len(line) > MAX_LINE_BYTES or consumed > remaining or not line.endswith(b"\n"):
                    return None, consumed
                if b'"usage"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(record, dict) and record.get("type") == "usage":
                    records.append(record)
    except (OSError, RunLookupError, RunRefusedError):
        return None, consumed
    return None, consumed


def read_stats_events(
    path: Path, visible: frozenset[str], end: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Kind and time index reads with the same SQLite deadline as query_events."""
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    rows: list[dict[str, Any]] = []
    consumed = 0
    examined = 0
    reasons: list[str] = []
    try:
        with contextlib.closing(
            sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=QUERY_TIMEOUT_SECONDS,
            ),
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout={READ_BUSY_TIMEOUT_MS}")
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), QUERY_PROGRESS_STEPS)
            db.execute("BEGIN")
            for kind in EVENT_KINDS:
                cursor = db.execute(
                    "SELECT * FROM events WHERE kind=? AND ts<=? ORDER BY ts DESC LIMIT ?",
                    (kind, end, MAX_EVENT_ROWS + 1),
                )
                for raw in cursor:
                    examined += 1
                    consumed += sum(len(value.encode()) for value in raw if isinstance(value, str))
                    if examined > MAX_EVENT_ROWS or consumed > MAX_EVENT_BYTES:
                        return rows, ["event_read_budget"]
                    if time.monotonic() >= deadline:
                        raise EventQueryTooBroad
                    if raw["symphony"] in visible:
                        rows.append({**dict(raw), "data": json.loads(raw["data"])})
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            raise EventQueryTooBroad from exc
        raise
    return rows, reasons


def load_sources(
    root: Path | None, event_path: Path | None, visible: frozenset[str], end: str,
) -> StatsSources:
    """Load once per cache interval; no writes and no new durable storage."""
    result = StatsSources()
    deadline = time.monotonic() + SOURCE_TIMEOUT_SECONDS
    if root is None:
        result.reasons.append("transcripts_unavailable")
    else:
        history = retained_runs(root, visible, limit=MAX_RUNS, deadline=deadline)
        result.runs = history["rows"]
        result.reasons.extend(history["partial_reasons"])
        ledger = TokenLedger()
        consumed = 0
        for row in result.runs:
            if consumed >= MAX_USAGE_BYTES or time.monotonic() >= deadline:
                result.reasons.append("usage_read_budget_or_unreadable")
                break
            records, size = _usage(root, row, max(0, MAX_USAGE_BYTES - consumed), deadline)
            consumed += size
            if records is None:
                result.reasons.append("usage_read_budget_or_unreadable")
                continue
            ledger.observe(
                row["run"],
                symphony=row["attributed_symphony"],
                card=row["card"],
                stage=str(row.get("stage") or ""),
                records=records,
            )
        result.usage = ledger.totals_by_run(row["run"] for row in result.runs)
    if event_path is not None:
        result.events, reasons = read_stats_events(event_path, visible, end)
        result.reasons.extend(reasons)
    return result
