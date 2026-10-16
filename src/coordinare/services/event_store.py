from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

from coordinare.lib.redaction import MAX_STATE_DEPTH, redact_mapping, redact_state_value
from coordinare.services.event_store_db import (
    COLUMNS,
    create_schema,
    disk_stats,
    migrate_filter_index,
    migrate_performer_batch,
    prune_batch,
)
from coordinare.services.event_store_query import (
    DEFAULT_LIMIT,
    EventFilters,
    read_events,
    utc_timestamp,
)
from coordinare.services.event_store_settings import EventStoreConfig
from coordinare.services.export_metrics import EXPORT_METRICS
from coordinare.services.stats_poll_db import (
    BOARD_CHECK_KIND,
    migrate_board_checks_batch,
    record_board_checks,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from coordinare.services.otlp_export import OtlpExporter

DEFAULT_QUEUE_SIZE = 4096
WRITE_ATTEMPTS = 6
RETRY_INITIAL_SECONDS = 0.05
RETRY_MAX_SECONDS = 0.5
RETRY_BACKOFF = 2
INSERT_BATCH_SIZE = 128
PRUNE_BATCH_SIZE = 256
MAINTENANCE_SECONDS = 30.0
MAINTENANCE_YIELD_SECONDS = 0.01
SHUTDOWN_SECONDS = 5.0
MAX_EVENT_BYTES = 256 * 1024
MAX_USAGE_DROP_RUNS = 128
USAGE_DROPPED_KIND = "usage_events_dropped"
ALL_USAGE_RUNS = ("", "", "")
MAX_TEXT_CHARS = 16 * 1024
REDACTION_OVERLAP_CHARS = 1024
REDACTION_YIELD_ROWS = 1
# sleep(0) does not release the GIL to a waiting event loop thread on CPython; a real
# sleep does. Measured: 1 ms caps loop overrun near 3 ms for a burst of 100 KB rows.
REDACTION_YIELD_SECONDS = 0.001
DEPTH_MARKER = "[REDACTED: value nested too deeply]"
T = TypeVar("T")


def _clip_strings(value: Any, limit: int, depth: int = 0) -> Any:
    """Bound each regex input, including nested data and mapping keys."""
    if isinstance(value, str):
        return value[:limit]
    if depth >= MAX_STATE_DEPTH:
        return DEPTH_MARKER
    if isinstance(value, dict):
        return {
            key[:limit]: _clip_strings(item, limit, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_clip_strings(item, limit, depth + 1) for item in value]
    return value


def _safe_row(raw: str) -> dict[str, Any]:
    # Only the writer transforms the immutable producer snapshot. No raw value
    # is passed to SQLite, including its journal and full text index.
    plain = _clip_strings(json.loads(raw), MAX_TEXT_CHARS + REDACTION_OVERLAP_CHARS)
    safe = _clip_strings(redact_mapping(redact_state_value(plain)), MAX_TEXT_CHARS)
    row = {column: str(safe.get(column) or "") for column in COLUMNS}
    row["ts"] = utc_timestamp(str(safe["ts"])) if safe.get("ts") else datetime.now(UTC).isoformat()
    row["level"] = str(safe.get("level") or "info").lower()
    row["source"] = str(safe.get("source") or "daemon")
    row["data"] = json.dumps(safe.get("data") or {}, ensure_ascii=True)
    return row


class EventStore:
    """Best-effort ingress, one SQLite writer and independent threaded readers.

    No error path logs through structlog. Broken disks and malformed events must
    not recurse through the capture processor or raise into daemon callers.
    """

    def __init__(
        self,
        path: Path,
        *,
        config: EventStoreConfig | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        prune_batch_size: int = PRUNE_BATCH_SIZE,
        enable_fts: bool = True,
    ) -> None:
        self._exporter: OtlpExporter | None = None
        self._export_error = ""
        self.path = path
        self.config = config or EventStoreConfig()
        self._capacity = max(queue_size, 1)
        self._byte_capacity = self.config.queue_bytes
        self._outstanding_bytes = 0
        self._writer_state = "starting"
        self._last_error: str | None = None
        self._stats_error: str | None = None
        self._prune_batch_size = max(prune_batch_size, 1)
        self._enable_fts = enable_fts
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._capacity)
        self._ready = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._lock = threading.Lock()
        self._outstanding = 0
        self._dropped = 0
        self._usage_drops: set[tuple[str, str, str]] = set()
        self._pruned = 0
        self._migration_active = False
        self._board_check_migration_active: bool | None = None
        self._filter_migration_active = True
        self._accepting = True
        self._loop: asyncio.AbstractEventLoop | None = None
        self._owner = threading.get_ident()
        self._task: asyncio.Task[None] | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="event-store")
        self._db: sqlite3.Connection | None = None
        self.on_commit: Callable[[], None] | None = None
        self._stats: dict[str, Any] = {"size_bytes": 0, "rows": 0, "oldest_ts": None}

    def set_export_error(self, reason: str) -> None:
        self._export_error = reason

    def set_exporter(self, exporter: OtlpExporter) -> None:
        """Attach optional outbound delivery before starting the store."""
        self._exporter = exporter

    def emit(self, event: Mapping[str, Any]) -> bool:
        """Snapshot and enqueue without redaction or disk work on the caller."""
        try:
            snapshot = dict(event)
            snapshot["ts"] = utc_timestamp(str(snapshot["ts"])) if snapshot.get("ts") else datetime.now(UTC).isoformat()
            raw = json.dumps(snapshot, default=str, ensure_ascii=True)
        except Exception:
            with self._lock:
                self._dropped += 1
                EXPORT_METRICS.dropped.inc()
            return False
        # ASCII JSON length is its byte size. Check before any redaction or
        # queue reservation, and include callbacks and batches still in flight.
        size = len(raw)
        with self._lock:
            if (
                not self._accepting or size > MAX_EVENT_BYTES
                or self._outstanding >= self._capacity
                or self._outstanding_bytes + size > self._byte_capacity
            ):
                self._dropped += 1
                EXPORT_METRICS.dropped.inc()
                return False
            self._outstanding += 1
            self._outstanding_bytes += size
        try:
            if threading.get_ident() == self._owner:
                self._enqueue(raw)
            elif self._loop is not None:
                self._loop.call_soon_threadsafe(self._enqueue, raw)
            else:
                self._finish(1, size, failed=True)
                return False
        except Exception:
            self._finish(1, size, failed=True)
            return False
        return True

    def _enqueue(self, raw: str) -> None:
        self._idle.clear()
        self._queue.put_nowait(raw)
        self._ready.set()

    def record_usage_drop(self, run_id: str, symphony: str, card_id: str) -> None:
        """Keep bounded run identities until the writer can persist loss markers.

        Reserve one slot for a conservative global marker if identities overflow.
        This prevents saturation from silently restoring complete coverage.
        """
        identity = (run_id, symphony, card_id)
        with self._lock:
            if identity in self._usage_drops:
                return
            if len(self._usage_drops) >= MAX_USAGE_DROP_RUNS - 1:
                identity = ALL_USAGE_RUNS
            self._usage_drops.add(identity)

    def usage_drop_snapshot(self) -> set[tuple[str, str, str]]:
        with self._lock:
            return self._usage_drops.copy()

    async def _persist_usage_drops(self) -> None:
        identities = self.usage_drop_snapshot()
        if not identities:
            return
        timestamp = datetime.now(UTC).isoformat()
        rows = [json.dumps({
            "ts": timestamp, "run_id": run, "symphony": symphony, "card_id": card,
            "kind": USAGE_DROPPED_KIND,
            "data": {"all_runs": (run, symphony, card) == ALL_USAGE_RUNS},
        }) for run, symphony, card in identities]
        try:
            await self._call(lambda: self._write_batch(rows))
        except Exception as exc:
            self._record_error(exc)
        else:
            with self._lock:
                self._usage_drops.difference_update(identities)

    def _finish(self, count: int, size: int, *, failed: bool = False) -> None:
        with self._lock:
            self._outstanding -= count
            self._outstanding_bytes -= size
            if failed:
                self._dropped += count
                EXPORT_METRICS.dropped.inc(count)
        if self._loop is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._signal_idle)

    def _signal_idle(self) -> None:
        if not self._outstanding and not self.usage_drop_snapshot():
            self._idle.set()

    async def _call(self, function: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._executor, function)

    def _initialize(self) -> None:
        if self._db is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        try:
            create_schema(db, enable_fts=self._enable_fts)
            with db:
                db.execute(
                    "UPDATE stats_board_check_policy SET retention_days=? WHERE id=1",
                    (self.config.retention_days,),
                )
            # Build empty indexes before readers can arrive. Deferring these
            # builds races the first reads with exclusive schema locks.
            if db.execute("SELECT 1 FROM events LIMIT 1").fetchone() is None:
                while migrate_filter_index(db):
                    pass
                self._filter_migration_active = False
        except Exception:
            db.close()
            raise
        self._db = db
        self._update_stats()

    def _refresh_stats(self, delta: int | None = None) -> None:
        if self._db is not None:
            rows = self._stats["rows"] + delta if delta is not None else None
            self._stats = disk_stats(self._db, self.path, rows)

    def _update_stats(self, delta: int | None = None) -> None:
        # A committed write must never be retried because health collection
        # failed. Recount on recovery because cached deltas may now be stale.
        try:
            self._refresh_stats(None if self._stats_error else delta)
        except Exception as exc:
            self._stats_error = type(exc).__name__
            self._record_error(exc)
        else:
            self._stats_error = None

    def _write_batch(self, rows: list[str]) -> None:
        safe_rows = []
        for index, raw in enumerate(rows, start=1):
            safe_rows.append(_safe_row(raw))
            if index % REDACTION_YIELD_ROWS == 0:
                time.sleep(REDACTION_YIELD_SECONDS)
        self._initialize()
        if self._db is None:
            return
        polls = [row for row in safe_rows if row["kind"] == BOARD_CHECK_KIND]
        safe_rows = [row for row in safe_rows if row["kind"] != BOARD_CHECK_KIND]
        columns = ",".join(COLUMNS)
        placeholders = ",".join("?" for _ in COLUMNS)
        # Identifiers come only from the schema; event values remain bound parameters.
        insert_sql = "".join(("INSERT OR IGNORE INTO events (", columns, ") VALUES (", placeholders, ")"))
        accepted_sql = "".join(("SELECT ", columns, " FROM events WHERE id > ? ORDER BY id"))
        with self._db:
            if polls:
                record_board_checks(self._db, polls)
            before = self._db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
            inserted = self._db.executemany(
                insert_sql,
                [tuple(row[column] for column in COLUMNS) for row in safe_rows],
            )
            # One writer owns this connection. A replay can leave gaps in ids,
            # so read the committed range only when SQLite ignored some rows.
            accepted = safe_rows if inserted.rowcount == len(safe_rows) else [
                dict(zip(COLUMNS, values, strict=True))
                for values in self._db.execute(
                    accepted_sql, (before,),
                ).fetchall()
            ]
        # rowcount excludes unique-index and replay-trigger ignores. Count only
        # after commit so a retried transaction cannot inflate the counter.
        with self._lock:
            self._dropped += len(safe_rows) - inserted.rowcount
        EXPORT_METRICS.written.inc(len(accepted))
        # Telemetry is outside the transaction and cannot turn a committed
        # insert into a retry. Only accepted redacted rows cross this boundary.
        for row in accepted:
            with contextlib.suppress(Exception):
                EXPORT_METRICS.observe(row)
            if self._exporter is not None:
                with contextlib.suppress(Exception):
                    self._exporter.offer(row)
        self._update_stats(len(accepted))

    async def start(self) -> None:
        if self._task is not None:
            return
        if self._exporter is not None:
            self._exporter.start()
        self._loop = asyncio.get_running_loop()
        self._owner = threading.get_ident()
        self._task = asyncio.create_task(self._run(), name="event-store-writer")

    async def _run(self) -> None:
        next_prune = time.monotonic() + MAINTENANCE_SECONDS
        next_index = time.monotonic() + MAINTENANCE_YIELD_SECONDS
        try:
            try:
                await self._call(self._initialize)
                self._writer_state = "idle"
            except Exception as exc:
                self._record_error(exc)
            while self._accepting or self._outstanding:
                self._ready.clear()
                rows = self._take_batch()
                if rows:
                    await self._persist(rows)
                await self._persist_usage_drops()
                self._signal_idle()
                if not rows and self._accepting:
                    next_work = min(next_prune, next_index) if self._filter_migration_active else next_prune
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._ready.wait(),
                            timeout=max(next_work - time.monotonic(), MAINTENANCE_YIELD_SECONDS),
                        )
                if self._accepting and time.monotonic() >= next_prune:
                    removed = await self.prune()
                    interval = MAINTENANCE_YIELD_SECONDS if removed or self._migration_active else MAINTENANCE_SECONDS
                    next_prune = time.monotonic() + interval
                elif self._accepting and self._filter_migration_active and time.monotonic() >= next_index:
                    interval = await self._maintain_filter_index()
                    next_index = time.monotonic() + interval
                await asyncio.sleep(0)
        finally:
            await self._persist_usage_drops()
            with contextlib.suppress(Exception):
                await self._call(self._close_connection)
            self._executor.shutdown(wait=False)
            if self._writer_state != "failed":
                self._writer_state = "closed"

    def _take_batch(self) -> list[str]:
        rows: list[str] = []
        while len(rows) < INSERT_BATCH_SIZE and not self._queue.empty():
            rows.append(self._queue.get_nowait())
        return rows

    def _record_error(self, exc: Exception) -> None:
        self._writer_state = "failed"
        # Exception text may contain secrets or attacker input. The class gives
        # operators a safe failure category without logging back into the store.
        self._last_error = type(exc).__name__

    async def _persist(self, rows: list[str]) -> None:
        failed = True
        delay = RETRY_INITIAL_SECONDS
        try:
            for attempt in range(WRITE_ATTEMPTS):
                self._writer_state = "writing"
                try:
                    await self._call(lambda: self._write_batch(rows))
                except Exception as exc:
                    self._record_error(exc)
                    if attempt + 1 < WRITE_ATTEMPTS:
                        self._writer_state = "retrying"
                        await asyncio.sleep(delay)
                        delay = min(delay * RETRY_BACKOFF, RETRY_MAX_SECONDS)
                else:
                    failed = False
                    if self.on_commit is not None:
                        with contextlib.suppress(Exception):
                            self.on_commit()
                    self._writer_state = "failed" if self._stats_error else "idle"
                    self._last_error = self._stats_error
                    break
        finally:
            self._finish(len(rows), sum(map(len, rows)), failed=failed)
            for _ in rows:
                self._queue.task_done()

    def _migrate_filter_index(self) -> bool:
        self._initialize()
        return self._db is not None and migrate_filter_index(self._db)

    async def _maintain_filter_index(self) -> float:
        try:
            self._filter_migration_active = await self._call(self._migrate_filter_index)
        except Exception as exc:
            self._record_error(exc)
            return MAINTENANCE_SECONDS
        return MAINTENANCE_YIELD_SECONDS

    def _prune(self) -> int:
        self._initialize()
        if self._db is None:
            return 0
        migrated, self._migration_active = migrate_performer_batch(self._db, self._prune_batch_size)
        if self._board_check_migration_active is None:
            # New polls go straight to hourly counters, so absence is final
            # for this writer's lifetime. Use the kind index and stop at one.
            self._board_check_migration_active = self._db.execute(
                "SELECT 1 FROM events WHERE kind=? LIMIT 1", (BOARD_CHECK_KIND,),
            ).fetchone() is not None
        rolled_up = 0
        if self._board_check_migration_active:
            rolled_up = migrate_board_checks_batch(self._db, self._prune_batch_size)
            self._board_check_migration_active = bool(rolled_up)
        self._migration_active = self._migration_active or self._board_check_migration_active
        removed = prune_batch(self._db, self.path, self.config, self._prune_batch_size)
        self._pruned += removed
        EXPORT_METRICS.pruned.inc(removed)
        # Retention also removes hourly counters, which are not event rows.
        self._update_stats()
        return removed + migrated + rolled_up

    async def prune(self) -> int:
        try:
            return await self._call(self._prune)
        except Exception as exc:
            self._record_error(exc)
            return 0

    def health(self) -> dict[str, Any]:
        return {
            **(self._exporter.health() if self._exporter is not None else {"export_dropped": 0, "export_failed": 0}),
            "export_error": self._export_error,
            **self._stats, "dropped": self._dropped, "pruned": self._pruned,
            "writer_state": self._writer_state,
            "last_error": redact_state_value(self._last_error),
        }

    async def query_events(
        self,
        filters: EventFilters | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            read_events, self.path, filters or EventFilters(), cursor, limit,
        )

    async def flush(self) -> None:
        self._idle.clear()
        self._signal_idle()
        await self._idle.wait()

    async def close(self, *, grace_seconds: float = SHUTDOWN_SECONDS) -> None:
        with self._lock:
            self._accepting = False
        if self._task is None:
            await self.start()
        self._ready.set()
        if self._task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._task), timeout=grace_seconds)

        if self._exporter is not None:
            await self._exporter.close()

    async def wait_closed(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    def _close_connection(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
