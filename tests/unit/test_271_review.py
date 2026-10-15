from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from collections import deque
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any
from unittest.mock import patch

import pytest

from coordinare.services import event_capture, event_store
from coordinare.services.event_store import EventStore
from coordinare.services.event_store_db import create_schema

DEADLINE_SECONDS = 10
LARGE_TEXT_CHARS = 100 * 1024
TEXT_CAP_CHARS = 16 * 1024
MAX_REDACTION_SECONDS = 0.008
TIMING_SAMPLES = 7
MILLISECONDS_PER_SECOND = 1000
OLD_DATABASE_ROWS = 50_000
MIGRATION_BATCH_SIZE = 32
BUFFER_OVERFLOW = 3
TOKEN = "ghp_" + "a" * 36
BOUNDARY_OFFSET = 8
REENTRY_SCRIPT = '''
from __future__ import annotations
import asyncio
import sys
from pathlib import Path
import structlog
from coordinare.services import event_capture
from coordinare.services.event_capture import event_store_processor, install_event_store
from coordinare.services.event_store import EventStore
logger = structlog.wrap_logger(
    structlog.ReturnLogger(),
    processors=[event_store_processor, structlog.processors.JSONRenderer()],
).bind()
class Noisy:
    def __str__(self):
        logger.info("inside_str")
        return "noisy"
async def main():
    store = EventStore(Path(sys.argv[1])) if sys.argv[2] == "store" else None
    if store is not None:
        install_event_store(store)
        await store.start()
    logger.info("outer", obj=Noisy())
    if store is not None:
        await store.close()
        rows = (await store.query_events())["events"]
        assert [row["kind"] for row in rows] == ["outer"]
    else:
        assert len(event_capture._EARLY_RECORDS) == 1
    print("completed")
asyncio.run(main())
'''
REPLAY_SCRIPT = '''
from __future__ import annotations
from coordinare.services.event_capture import event_store_processor, install_event_store
class LoggingStore:
    def emit(self, row):
        event_store_processor(None, "info", {"event": "replay.nested"})
        return True
event_store_processor(None, "info", {"event": "early"})
install_event_store(LoggingStore())
print("completed")
'''


@pytest.mark.parametrize("mode", ["store", "no_store"])
def test_logging_str_completes_with_deadline(tmp_path: Path, mode: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", REENTRY_SCRIPT, str(tmp_path / "events.db"), mode],
        capture_output=True, text=True, timeout=DEADLINE_SECONDS, check=True,
    )
    assert "completed" in result.stdout


def test_replay_emit_can_log_with_deadline() -> None:
    result = subprocess.run(
        [sys.executable, "-c", REPLAY_SCRIPT],
        capture_output=True, text=True, timeout=DEADLINE_SECONDS, check=True,
    )
    assert "completed" in result.stdout


def test_early_buffer_is_a_bounded_ring(monkeypatch: pytest.MonkeyPatch) -> None:
    event_capture.install_event_store(None)
    assert event_capture._EARLY_RECORDS.maxlen == event_capture.EARLY_RECORD_LIMIT
    ring: deque[str] = deque(maxlen=event_capture.EARLY_RECORD_LIMIT)
    monkeypatch.setattr(event_capture, "_EARLY_RECORDS", ring)
    count = event_capture.EARLY_RECORD_LIMIT + BUFFER_OVERFLOW
    for index in range(count):
        event_capture.event_store_processor(None, "info", {"event": "early", "index": index})
        assert len(ring) <= event_capture.EARLY_RECORD_LIMIT
        assert sum(map(len, ring)) <= event_capture.EARLY_BYTE_LIMIT
    assert ring.maxlen == event_capture.EARLY_RECORD_LIMIT
    assert json.loads(ring[0])["data"]["index"] == BUFFER_OVERFLOW


@pytest.mark.parametrize("field", ["message", "data"])
def test_large_text_redaction_has_bounded_cost_and_output(field: str) -> None:
    text = "x" * LARGE_TEXT_CHARS
    payload = {field: text if field == "message" else {"nested": [text]}}
    raw = json.dumps(payload)
    timings: list[float] = []
    for _ in range(TIMING_SAMPLES):
        started = perf_counter()
        row = event_store._safe_row(raw)
        timings.append(perf_counter() - started)
    output = row["message"] if field == "message" else json.loads(row["data"])["nested"][0]
    assert len(output) <= TEXT_CAP_CHARS
    elapsed = median(timings)
    print(f"{field}_redaction_ms={elapsed * MILLISECONDS_PER_SECOND:.3f}")
    assert elapsed < MAX_REDACTION_SECONDS


@pytest.mark.parametrize("field", ["message", "data"])
def test_clipped_secret_is_redacted_across_boundary(field: str) -> None:
    prefix = "x" * (TEXT_CAP_CHARS - BOUNDARY_OFFSET) + " "
    text = prefix + TOKEN + " " + "tail" * TEXT_CAP_CHARS
    payload = {field: text if field == "message" else {"nested": [text]}}
    row = event_store._safe_row(json.dumps(payload))
    output = row["message"] if field == "message" else json.loads(row["data"])["nested"][0]
    assert "ghp_" not in output
    assert len(output) <= TEXT_CAP_CHARS
    assert output.startswith(prefix)


@pytest.mark.asyncio
async def test_redaction_yields_between_rows(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    store.emit({"message": "seed"})
    await asyncio.wait_for(store.flush(), timeout=DEADLINE_SECONDS)
    rows = [json.dumps({"message": str(index)}) for index in range(MIGRATION_BATCH_SIZE)]
    original = event_store._safe_row
    calls: list[str] = []

    def safe_row(raw: str) -> dict[str, Any]:
        calls.append("redact")
        return original(raw)

    def yield_thread(seconds: float) -> None:
        assert seconds == 0
        calls.append("yield")

    with (
        patch.object(event_store, "_safe_row", side_effect=safe_row),
        patch.object(event_store.time, "sleep", side_effect=yield_thread),
    ):
        await store._call(lambda: store._write_batch(rows))
    await store.close()
    assert calls.count("yield") >= len(rows)
    assert calls[:2] == ["redact", "yield"]


@pytest.mark.asyncio
async def test_stats_failure_after_commit_never_retries_insert(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    store.emit({"message": "seed"})
    await asyncio.wait_for(store.flush(), timeout=DEADLINE_SECONDS)
    with (
        patch.object(store, "_refresh_stats", side_effect=OSError("stats unavailable")),
        patch.object(store, "_write_batch", wraps=store._write_batch) as write,
    ):
        store.emit({"message": "committed once"})
        await asyncio.wait_for(store.flush(), timeout=DEADLINE_SECONDS)
        assert write.call_count == 1
        assert store.health()["last_error"] == "OSError"
        assert store.health()["dropped"] == 0
        rows = (await store.query_events())["events"]
        assert [row["message"] for row in rows].count("committed once") == 1
    store.emit({"message": "stats recover"})
    await asyncio.wait_for(store.flush(), timeout=DEADLINE_SECONDS)
    assert store.health()["last_error"] is None
    assert store.health()["rows"] == len((await store.query_events())["events"])
    await store.close()


def legacy_database(path: Path, count: int) -> None:
    with sqlite3.connect(path) as db:
        create_schema(db)
        db.execute("DROP INDEX events_performer_seq")
        db.execute("PRAGMA user_version=2")
        db.executemany(
            "INSERT INTO events (ts,source,run_id,data) VALUES (?,?,?,?)",
            [("2026-10-15T12:00:00Z", "performer", "run", '{"transcript_seq":1}')] * count,
        )


@pytest.mark.asyncio
async def test_large_v2_database_accepts_first_write_before_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    legacy_database(path, OLD_DATABASE_ROWS)
    original = sqlite3.connect
    statements: list[str] = []

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = original(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    store = EventStore(path)
    with patch.object(event_store.sqlite3, "connect", side_effect=connect):
        await store.start()
        assert store.emit({"message": "first batch"})
        await asyncio.wait_for(store.flush(), timeout=DEADLINE_SECONDS)
        await store.close()
    assert not any(sql.lstrip().upper().startswith("DELETE FROM EVENTS ") for sql in statements)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == OLD_DATABASE_ROWS + 1
        assert db.execute("SELECT message FROM events ORDER BY id DESC LIMIT 1").fetchone()[0] == "first batch"


@pytest.mark.asyncio
async def test_migration_cleanup_is_bounded_resumable_and_guards_new_replays(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    legacy_database(path, MIGRATION_BATCH_SIZE * BUFFER_OVERFLOW)
    store = EventStore(path, prune_batch_size=MIGRATION_BATCH_SIZE)
    await store.start()
    replay = {"source": "performer", "run_id": "run", "data": {"transcript_seq": 1}}
    store.emit(replay)
    await asyncio.wait_for(store.flush(), timeout=DEADLINE_SECONDS)
    assert store.health()["rows"] == MIGRATION_BATCH_SIZE * BUFFER_OVERFLOW
    before = store.health()["rows"]
    await store.prune()
    assert 0 < before - store.health()["rows"] <= MIGRATION_BATCH_SIZE
    await store.close()
    reopened = EventStore(path, prune_batch_size=MIGRATION_BATCH_SIZE)
    await reopened.start()
    reopened.emit(replay)
    await asyncio.wait_for(reopened.flush(), timeout=DEADLINE_SECONDS)
    for _ in range(BUFFER_OVERFLOW):
        await reopened.prune()
    assert reopened.health()["rows"] == 1
    reopened.emit(replay)
    await asyncio.wait_for(reopened.flush(), timeout=DEADLINE_SECONDS)
    assert reopened.health()["rows"] == 1
    await reopened.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='events_performer_seq'").fetchone()
