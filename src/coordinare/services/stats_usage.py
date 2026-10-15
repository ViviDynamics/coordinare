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
    EventQueryTooBroad,
)
from coordinare.services.token_usage import UsageCounts

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.services.token_usage import ModelTotals

MAX_USAGE_GROUPS = 10000
MAX_USAGE_RUNS = 2000
USAGE_BATCH_RUNS = 128
USAGE_READ_SECONDS = 1.0
USAGE_COVERAGE_SQL = """
    WITH requested AS (
        SELECT json_extract(value, '$.run') AS run,
               json_extract(value, '$.attributed_symphony') AS owner,
               json_extract(value, '$.symphony') AS folder,
               json_extract(value, '$.card') AS card FROM json_each(?)
    ) SELECT e.run_id,
        coalesce(
            min(CASE WHEN e.kind='run_usage' AND e.ts<=?
                AND json_type(e.data, '$.usage')='object' THEN e.ts END),
            min(CASE WHEN e.kind IN ('usage', 'cost') AND e.ts<=?
                AND json_type(e.data, '$.usage')='object' THEN e.ts END)
        ) AS first_usage,
        max(e.kind='usage_events_dropped') AS dropped
    FROM requested r JOIN events e INDEXED BY events_run_ts ON e.run_id=r.run
    WHERE e.symphony IN (r.owner, r.folder) AND e.card_id=r.card
        AND (e.kind IN ('run_usage', 'usage_events_dropped')
            OR (e.source='performer' AND e.kind IN ('usage', 'cost')))
    GROUP BY e.run_id
"""
GLOBAL_USAGE_LOSS_SQL = """
    SELECT 1 FROM events WHERE kind='usage_events_dropped' AND run_id='' LIMIT 1
"""
USAGE_SQL = """
        WITH requested AS MATERIALIZED (
            SELECT json_extract(value, '$.run') AS run,
                   json_extract(value, '$.attributed_symphony') AS owner,
                   json_extract(value, '$.symphony') AS folder,
                   json_extract(value, '$.card') AS card FROM json_each(?)
        ),
        raw AS (
            SELECT e.id, e.run_id, e.ts, e.kind, e.data FROM requested r JOIN events e INDEXED BY events_run_ts
              ON e.run_id=r.run AND e.ts<=?
            WHERE e.symphony IN (r.owner, r.folder)
              AND e.card_id=r.card
              AND (e.kind='run_usage' OR (e.source='performer' AND e.kind IN ('usage','cost')))
              AND json_type(e.data, '$.usage')='object'
        ), parsed AS (
            SELECT id, run_id, CASE WHEN json_type(data, '$.usage.model')='text'
                     THEN nullif(json_extract(data, '$.usage.model'), '') END AS model,
                coalesce(strftime('%Y-%m-%d', json_extract(data, '$.ts')), '') AS day,
                json_extract(data, '$.scope') AS scope,
                coalesce(json_extract(data, '$.seq'), json_extract(data, '$.transcript_seq')) AS seq,
                CASE WHEN kind='run_usage' THEN 1 ELSE 0 END AS canonical,
                CASE WHEN json_type(data, '$.usage.input_tokens')='integer' THEN max(0, json_extract(data, '$.usage.input_tokens')) ELSE 0 END AS input_tokens,
                CASE WHEN json_type(data, '$.usage.output_tokens')='integer' THEN max(0, json_extract(data, '$.usage.output_tokens')) ELSE 0 END AS output_tokens,
                CASE WHEN json_type(data, '$.usage.cache_read')='integer' THEN max(0, json_extract(data, '$.usage.cache_read')) ELSE 0 END AS cache_read,
                CASE WHEN json_type(data, '$.usage.cache_write')='integer' THEN max(0, json_extract(data, '$.usage.cache_write')) ELSE 0 END AS cache_write
            FROM raw
        ), valid AS (
            SELECT *, max(canonical) OVER (PARTITION BY run_id) AS preferred
            FROM parsed WHERE input_tokens+output_tokens+cache_read+cache_write>0
        ), deduplicated AS (
            SELECT *, row_number() OVER (PARTITION BY run_id,
                CASE WHEN seq IS NULL THEN 'id:'||id ELSE 'seq:'||seq END ORDER BY id) AS occurrence
            FROM valid WHERE canonical=preferred
        ), ranked AS (
            SELECT *, CASE scope WHEN 'message' THEN 2 WHEN 'run' THEN 1 ELSE 0 END AS tier,
                max(CASE scope WHEN 'message' THEN 2 WHEN 'run' THEN 1 ELSE 0 END)
                    OVER (PARTITION BY run_id) AS preferred_tier,
                row_number() OVER (PARTITION BY run_id, scope, day ORDER BY seq DESC, id DESC) AS daily_last
            FROM deduplicated WHERE occurrence=1
        ), selected AS (
            SELECT * FROM ranked WHERE tier=preferred_tier AND (tier!=1 OR daily_last=1)
        ), steps AS (
            SELECT run_id, model,
                CASE WHEN scope='run' THEN max(0, input_tokens - coalesce(lag(input_tokens) OVER (PARTITION BY run_id ORDER BY day), 0)) ELSE input_tokens END AS input_tokens,
                CASE WHEN scope='run' THEN max(0, output_tokens - coalesce(lag(output_tokens) OVER (PARTITION BY run_id ORDER BY day), 0)) ELSE output_tokens END AS output_tokens,
                CASE WHEN scope='run' THEN max(0, cache_read - coalesce(lag(cache_read) OVER (PARTITION BY run_id ORDER BY day), 0)) ELSE cache_read END AS cache_read,
                CASE WHEN scope='run' THEN max(0, cache_write - coalesce(lag(cache_write) OVER (PARTITION BY run_id ORDER BY day), 0)) ELSE cache_write END AS cache_write
            FROM selected
        ) SELECT run_id, model, sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens, sum(cache_read) AS cache_read, sum(cache_write) AS cache_write
          FROM steps GROUP BY run_id, model LIMIT ?
        """


@dataclass
class UsageCoverage:
    first_usage: dict[str, str] = field(default_factory=dict)
    dropped_runs: set[str] = field(default_factory=set)
    pruned_runs: set[str] = field(default_factory=set)


def read_stats_usage(
    path: Path, runs: list[dict[str, Any]], end: str,
    coverage: UsageCoverage | None = None,
) -> tuple[dict[str, ModelTotals], str | None, list[str]]:
    """Read bounded run/model totals using the run/time index in one read snapshot.

    The oldest retained event is the conservative coverage boundary. A run that
    started earlier needs its transcript even if some of its events remain.
    """
    deadline = time.monotonic() + USAGE_READ_SECONDS
    totals: dict[str, ModelTotals] = {}
    coverage = coverage if coverage is not None else UsageCoverage()
    if len(runs) > MAX_USAGE_RUNS:
        raise EventQueryTooBroad
    try:
        with contextlib.closing(sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=QUERY_TIMEOUT_SECONDS,
        )) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            first_rows = _query_rows(db, "SELECT ts FROM events ORDER BY ts LIMIT 1", (), deadline)
            first = first_rows[0] if first_rows else None
            oldest = first["ts"] if first is not None else None
            if _query_rows(db, GLOBAL_USAGE_LOSS_SQL, (), deadline):
                coverage.dropped_runs.update(run["run"] for run in runs)
            groups = 0
            for offset in range(0, len(runs), USAGE_BATCH_RUNS):
                batch = runs[offset:offset + USAGE_BATCH_RUNS]
                _coverage_batch(db, batch, end, deadline, coverage)
                rows = _usage_batch(db, batch, end, deadline)
                groups += len(rows)
                if groups > MAX_USAGE_GROUPS:
                    return {}, oldest, ["usage_event_group_budget"]
                for row in rows:
                    model = row["model"] if isinstance(row["model"], str) else None
                    totals.setdefault(row["run_id"], {})[model] = UsageCounts.from_mapping(dict(row))
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            raise EventQueryTooBroad from exc
        raise
    return totals, oldest, []


def _usage_batch(
    db: sqlite3.Connection, runs: list[dict[str, Any]], end: str, deadline: float,
) -> list[sqlite3.Row]:
    requested = [{key: run.get(key) for key in ("run", "card", "symphony", "attributed_symphony")} for run in runs]
    return _query_rows(db, USAGE_SQL, (json.dumps(requested), end, MAX_USAGE_GROUPS + 1), deadline)


def _coverage_batch(
    db: sqlite3.Connection, runs: list[dict[str, Any]], end: str, deadline: float,
    coverage: UsageCoverage,
) -> None:
    requested = [{key: run.get(key) for key in ("run", "card", "symphony", "attributed_symphony")} for run in runs]
    for row in _query_rows(db, USAGE_COVERAGE_SQL, (json.dumps(requested), end, end), deadline):
        if row["first_usage"] is not None:
            coverage.first_usage[row["run_id"]] = row["first_usage"]
        if row["dropped"]:
            coverage.dropped_runs.add(row["run_id"])


def _query_rows(
    db: sqlite3.Connection, sql: str, parameters: tuple[Any, ...], deadline: float,
) -> list[sqlite3.Row]:
    statement_deadline = min(deadline, time.monotonic() + QUERY_TIMEOUT_SECONDS)
    db.set_progress_handler(lambda: int(time.monotonic() >= statement_deadline), QUERY_PROGRESS_STEPS)
    rows = db.execute(sql, parameters).fetchall()
    if time.monotonic() >= statement_deadline:
        raise EventQueryTooBroad
    return rows
