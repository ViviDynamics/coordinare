from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections import deque
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any
from unittest.mock import patch

import pytest
import structlog

from coordinare.dashboard import store as dashboard_store_module
from coordinare.graph.nodes.monitor import activity as monitor_activity_module
from coordinare.graph.nodes.monitor.activity import _record_activity_batch
from coordinare.services import activity_log as activity_log_module
from coordinare.services import event_capture
from coordinare.services import event_store as event_store_module
from coordinare.services.activity_log import ActivityLog
from coordinare.services.event_capture import event_store_processor, install_event_store
from coordinare.services.event_store import MAX_EVENT_BYTES, EventStore
from coordinare.services.event_store_db import (
    CREATE_TABLE,
    VACUUM_PAGES,
    create_schema,
    prune_batch,
)
from coordinare.services.event_store_settings import EventStoreConfig

WAIT_SECONDS = 30.0
READER_LOCK_SECONDS = 3.0
MAX_STRUCTLOG_OVERHEAD_US = 50.0
TIMING_SAMPLES = 9
CALLS_PER_SAMPLE = 200
MICROSECONDS_PER_SECOND = 1_000_000
BYTE_BUDGET = 512
BUFFER_RECORDS = 128
PAYLOAD_REPEATS = 16
RESTART_COUNT = 2
TOKEN = "ghp_" + "a" * 36


async def flushed(store: EventStore) -> None:
    await asyncio.wait_for(store.flush(), timeout=WAIT_SECONDS)


@pytest.mark.asyncio
async def test_structlog_capture_median_overhead(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    baseline = structlog.wrap_logger(
        structlog.ReturnLogger(), processors=[structlog.processors.JSONRenderer()],
    ).bind()
    captured = structlog.wrap_logger(
        structlog.ReturnLogger(), processors=[event_store_processor, structlog.processors.JSONRenderer()],
    ).bind()
    payload = {"detail": "ordinary log metadata " * PAYLOAD_REPEATS}
    install_event_store(store)
    timings: list[float] = []
    try:
        for _ in range(TIMING_SAMPLES):
            started = perf_counter()
            for _ in range(CALLS_PER_SAMPLE):
                baseline.info("daemon.progress", **payload)
            baseline_seconds = perf_counter() - started
            started = perf_counter()
            for _ in range(CALLS_PER_SAMPLE):
                captured.info("daemon.progress", **payload)
            timings.append((perf_counter() - started - baseline_seconds) / CALLS_PER_SAMPLE)
        overhead = median(timings) * MICROSECONDS_PER_SECOND
        print(f"structlog_median_added_us={overhead:.2f}")
        assert overhead < MAX_STRUCTLOG_OVERHEAD_US
    finally:
        install_event_store(None)
        await store.close()
        await asyncio.wait_for(store.wait_closed(), timeout=WAIT_SECONDS)


@pytest.mark.asyncio
async def test_oversized_raw_event_never_calls_redaction(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    with patch("coordinare.services.event_store.redact_state_value") as redact:
        assert not store.emit({"message": TOKEN + "x" * MAX_EVENT_BYTES})
        redact.assert_not_called()
    assert store.health()["dropped"] == 1
    await store.close()


@pytest.mark.asyncio
async def test_redaction_runs_on_writer_before_sql_or_journal(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    owner = threading.get_ident()
    threads: list[int] = []
    original = event_store_module.redact_state_value

    def redact(value: Any) -> Any:
        threads.append(threading.get_ident())
        return original(value)

    statements: list[str] = []
    original_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = original_connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    payload = {"message": TOKEN, "data": {"password": "secret-value"}}
    with (
        patch("coordinare.services.event_store.sqlite3.connect", side_effect=connect),
        patch("coordinare.services.event_store.redact_state_value", side_effect=redact),
    ):
        assert store.emit(payload)
        assert not threads
        payload["message"] = "mutated"
        await store.start()
        await flushed(store)
        for path in tmp_path.iterdir():
            assert TOKEN.encode() not in path.read_bytes()
            assert b"secret-value" not in path.read_bytes()
        await store.close()
    assert any("INSERT OR IGNORE INTO events (" in sql for sql in statements)
    assert TOKEN not in str(statements)
    assert "secret-value" not in str(statements)
    assert threads and owner not in threads
    assert "mutated" not in str((await store.query_events())["events"])


@pytest.mark.asyncio
async def test_queue_byte_budget_covers_foreign_ingress_and_inflight_batch(tmp_path: Path) -> None:
    raw = {"message": "x" * (BYTE_BUDGET // 2)}
    store = EventStore(tmp_path / "events.db", config=EventStoreConfig(queue_bytes=BYTE_BUDGET))
    await store.start()
    entered = threading.Event()
    release = threading.Event()
    original = store._write_batch

    def write(rows: Any) -> None:
        entered.set()
        assert release.wait(WAIT_SECONDS)
        original(rows)

    with patch.object(store, "_write_batch", side_effect=write):
        assert await asyncio.to_thread(store.emit, raw)
        assert await asyncio.to_thread(entered.wait, WAIT_SECONDS)
        assert not await asyncio.to_thread(store.emit, raw)
        assert store.health()["dropped"] == 1
        release.set()
        await flushed(store)
        assert store.emit(raw)
        await flushed(store)
    await store.close()
    assert store.health()["rows"] == 2


@pytest.mark.asyncio
async def test_reader_lock_for_three_seconds_does_not_drop_batch(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    store.emit({"message": "seed"})
    await flushed(store)
    with sqlite3.connect(store.path) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM events").fetchall()
        assert store.emit({"message": "survives lock"})
        await asyncio.sleep(READER_LOCK_SECONDS)
        assert store.health()["dropped"] == 0
        assert store.health()["writer_state"] in {"writing", "retrying"}
        reader.rollback()
    await flushed(store)
    await store.close()
    assert store.health()["rows"] == 2


@pytest.mark.asyncio
async def test_persistent_writer_failure_is_visible_and_recovery_clears_error(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    with patch.object(store, "_write_batch", side_effect=sqlite3.OperationalError("disk failure")):
        assert store.emit({"message": "fails"})
        await flushed(store)
        assert store.health()["writer_state"] == "failed"
        assert "OperationalError" in store.health()["last_error"]
        assert store.health()["dropped"] == 1
    store.emit({"message": "recovers"})
    await flushed(store)
    assert store.health()["writer_state"] == "idle"
    assert store.health()["last_error"] is None
    await store.close()


@pytest.mark.asyncio
async def test_performer_feed_has_one_persistent_source_and_restart_identity(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    events = [
        {"type": "progress", "text": "same", "timestamp": "2026-10-15T12:00:00Z", "transcript_seq": seq}
        for seq in (1, 2)
    ]
    for attempt in range(RESTART_COUNT):
        store = EventStore(path)
        install_event_store(store)
        state: Any = {
            "event_store": store, "activity_log": ActivityLog(),
            "agent_dispatch": {"session_id": "run"},
        }
        try:
            await store.start()
            _record_activity_batch(state, events[:1], card_id="card", stage="implementer")
            await flushed(store)
            assert len((await store.query_events())["events"]) == attempt + 1
            _record_activity_batch(state, events, card_id="card", stage="implementer")
            await flushed(store)
            rows = (await store.query_events())["events"]
            assert len(rows) == 2
            assert {row["source"] for row in rows} == {"performer"}
            assert {row["data"]["transcript_seq"] for row in rows} == {1, 2}
        finally:
            install_event_store(None)
            await store.close()


@pytest.mark.asyncio
async def test_startup_logs_are_bounded_snapshots_and_flushed(tmp_path: Path, monkeypatch: Any) -> None:
    install_event_store(None)
    monkeypatch.setattr(event_capture, "_EARLY_RECORDS", deque())
    total = BUFFER_RECORDS * 2
    for index in range(total):
        event_store_processor(None, "info", {"event": "startup.progress", "index": index})
    record: dict[str, Any] = {"event": "startup.ready", "nested": {"value": "original"}}
    event_store_processor(None, "info", record)
    record["nested"]["value"] = "changed"
    store = EventStore(tmp_path / "events.db")
    install_event_store(store)
    try:
        await store.start()
        await flushed(store)
        rows = (await store.query_events(limit=BUFFER_RECORDS))["events"]
        assert len(rows) == BUFFER_RECORDS
        ready = next(row for row in rows if row["kind"] == "startup.ready")
        assert ready["data"]["nested"]["value"] == "original"
        assert len(event_capture._EARLY_RECORDS) == 0
    finally:
        install_event_store(None)
        await store.close()


@pytest.mark.asyncio
async def test_prune_issues_one_bounded_vacuum(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    store.emit({"message": "seed"})
    await flushed(store)
    await store.close()
    with sqlite3.connect(store.path) as db:
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        prune_batch(db, store.path, EventStoreConfig(), 1)
    vacuums = [sql for sql in statements if "incremental_vacuum" in sql]
    assert vacuums == [f"PRAGMA incremental_vacuum({VACUUM_PAGES})"]


@pytest.mark.parametrize("module", [dashboard_store_module, activity_log_module, monitor_activity_module])
def test_module_docstrings_are_preserved(module: Any) -> None:
    assert module.__doc__


def test_upgrade_removes_old_performer_replays_before_unique_index(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        db.execute(CREATE_TABLE)
        db.execute("PRAGMA user_version=2")
        for seq in (1, 1, 2):
            db.execute(
                "INSERT INTO events (ts,source,run_id,data) VALUES (?,?,?,?)",
                ("2026-10-15T12:00:00Z", "performer", "run", json.dumps({"transcript_seq": seq})),
            )
        db.commit()
        create_schema(db)
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 2
        db.execute(
            "INSERT OR IGNORE INTO events (ts,source,run_id,data) VALUES (?,?,?,?)",
            ("2026-10-15T12:00:00Z", "performer", "run", json.dumps({"transcript_seq": 1})),
        )
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 2
