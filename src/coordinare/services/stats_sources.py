from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from coordinare.services.event_capture import usage_drop_snapshot
from coordinare.services.event_store import ALL_USAGE_RUNS
from coordinare.services.event_store_query import (
    QUERY_PROGRESS_STEPS,
    QUERY_TIMEOUT_SECONDS,
    READ_BUSY_TIMEOUT_MS,
    EventQueryTooBroad,
    utc_timestamp,
)
from coordinare.services.run_history import retained_runs
from coordinare.services.run_transcript import transcript_path
from coordinare.services.run_viewer import RunLookupError, RunRef, RunRefusedError, open_run
from coordinare.services.stats_poll import BOARD_CHECK_KIND
from coordinare.services.stats_usage import UsageCoverage, read_stats_usage
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
EVENT_KINDS = (LIFECYCLE_KIND, "blocked", "system_error", "cancelled", BOARD_CHECK_KIND)


@dataclass
class StatsSources:
    runs: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, ModelTotals] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    usage_coverage: UsageCoverage = field(default_factory=UsageCoverage)


def _usage(
    root: Path,
    row: dict[str, Any],
    remaining: int,
    deadline: float,
) -> tuple[list[dict[str, Any]] | None, int, str | None]:
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
                    return records, consumed, None
                if len(line) > MAX_LINE_BYTES or consumed > remaining:
                    return None, consumed, "usage_read_budget_or_unreadable"
                if not line.endswith(b"\n"):
                    return records, consumed, "usage_partial_line"
                if b'"usage"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(record, dict) and record.get("type") == "usage":
                    records.append(record)
    except (OSError, RunLookupError, RunRefusedError):
        return None, consumed, "usage_read_budget_or_unreadable"
    return None, consumed, "usage_read_budget_or_unreadable"


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
    lifecycle: dict[str, Any] | None = None,
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
    oldest = None
    if event_path is not None:
        losses = usage_drop_snapshot(event_path)
        result.usage_coverage.dropped_runs.update(
            row["run"] for row in result.runs
            if ALL_USAGE_RUNS in losses or any(
                (row["run"], owner, row["card"]) in losses
                for owner in (row["symphony"], row["attributed_symphony"])
            )
        )
        result.usage, oldest, reasons = read_stats_usage(event_path, result.runs, end, result.usage_coverage)
        result.reasons.extend(reasons)
        result.events, reasons = read_stats_events(event_path, visible, end)
        result.reasons.extend(reasons)
        _mark_pruned_usage(result, lifecycle or {})
    if result.usage_coverage.dropped_runs:
        result.reasons.append("usage_events_dropped")
    if result.usage_coverage.pruned_runs:
        result.reasons.append("usage_partially_pruned")
    if root is not None:
        _transcript_usage(root, result, oldest)
    return result


def _transcript_usage(root: Path, result: StatsSources, oldest: str | None) -> None:
    ledger = TokenLedger()
    consumed = 0
    deadline = time.monotonic() + SOURCE_TIMEOUT_SECONDS
    legacy = []
    incomplete = result.usage_coverage.dropped_runs | result.usage_coverage.pruned_runs
    for row in result.runs:
        if oldest is not None and row["started_at"] >= oldest and row["run"] not in incomplete:
            if row["run"] not in result.usage:
                result.reasons.append("usage_events_missing")
            continue
        # A run predating retention can have only a suffix in the event store.
        result.usage.pop(row["run"], None)
        legacy.append(row)
    for row in legacy:
        if consumed >= MAX_USAGE_BYTES or time.monotonic() >= deadline:
            result.reasons.append("usage_read_budget_or_unreadable")
            break
        records, size, reason = _usage(root, row, max(0, MAX_USAGE_BYTES - consumed), deadline)
        consumed += size
        if reason is not None:
            result.reasons.append(reason)
        if records is not None:
            ledger.observe(
                row["run"], symphony=row["attributed_symphony"], card=row["card"],
                stage=str(row.get("stage") or ""), records=records,
            )
    result.usage.update(ledger.totals_by_run(row["run"] for row in result.runs))


def _mark_pruned_usage(result: StatsSources, lifecycle: dict[str, Any]) -> None:
    first: dict[tuple[str, str, str], str] = {}
    observations = [
        (symphony, card, event)
        for symphony, cards in lifecycle.items() for card, events in cards.items() for event in events
    ]
    observations.extend(
        (event["symphony"], event["card_id"], event["data"])
        for event in result.events if event["kind"] == LIFECYCLE_KIND
    )
    for symphony, card, event in observations:
        run_id = (event.get("detail") or {}).get("run_id")
        if not run_id or not isinstance(event.get("at"), str):
            continue
        try:
            timestamp = utc_timestamp(event["at"])
        except ValueError:
            continue
        key = (symphony, card, run_id)
        first[key] = min(first.get(key, timestamp), timestamp)
    for row in result.runs:
        began = first.get((row["attributed_symphony"], row["card"], row["run"]))
        usage = result.usage_coverage.first_usage.get(row["run"])
        if began is not None and usage is not None and began < usage:
            result.usage_coverage.pruned_runs.add(row["run"])
