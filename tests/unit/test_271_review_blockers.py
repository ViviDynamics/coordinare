"""Regression coverage for the phase two independent review."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import httpx
import pytest

from coordinare.dashboard.context import DashboardContext
from coordinare.dashboard.routers import events
from coordinare.dashboard.sse import SSEBroadcaster
from coordinare.services import event_store_query
from coordinare.services.event_store import EventStore
from coordinare.services.event_store_db import CREATE_TABLE, SCHEMA_VERSION, create_schema
from coordinare.services.event_store_query import EventFilters, read_events
from tests.unit.test_271_logs import _app

LEGACY_SCHEMA_VERSION = 3
REPLAY_SCHEMA_VERSION = 2
SCAN_ROWS = 5000
EXPECTED_DISTINCT_ROWS = 2
NOTIFICATION_INTERVAL_SECONDS = 1.0
WAIT_SECONDS = 10
MAX_READ_WAIT_MS = 100
FILTER_INDEXES = {
    "card_id": "events_card_ts",
    "source": "events_source_ts",
    "level": "events_level_ts",
    "stage": "events_stage_ts",
}
TOO_BROAD_MESSAGE = "query too broad, narrow the filters"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_ignored_performer_replays_are_counted_in_health(tmp_path: Path, legacy: bool) -> None:
    path = tmp_path / "events.db"
    replay = {"source": "performer", "run_id": "run", "data": {"transcript_seq": 1}}
    if legacy:
        with sqlite3.connect(path) as db:
            db.execute(CREATE_TABLE)
            db.execute(f"PRAGMA user_version={REPLAY_SCHEMA_VERSION}")
            db.execute(
                "INSERT INTO events (ts,source,run_id,data) VALUES (?,?,?,?)",
                ("2026-10-15T12:00:00+00:00", "performer", "run", '{"transcript_seq":1}'),
            )
    store = EventStore(path)
    await store.start()
    try:
        assert store.emit(replay)
        await asyncio.wait_for(store.flush(), WAIT_SECONDS)
        assert store.health()["dropped"] == int(legacy)
        assert store.emit(replay)
        assert store.emit({**replay, "data": {"transcript_seq": 2}})
        await asyncio.wait_for(store.flush(), WAIT_SECONDS)
        assert store.health()["rows"] == EXPECTED_DISTINCT_ROWS
        assert store.health()["dropped"] == int(legacy) + 1
        assert store.health()["writer_state"] == "idle"
    finally:
        await store.close()


@pytest.mark.parametrize("field,index", FILTER_INDEXES.items())
@pytest.mark.parametrize("legacy", [False, True])
def test_common_filter_migration_uses_search_index(tmp_path: Path, field: str, index: str, legacy: bool) -> None:
    with sqlite3.connect(tmp_path / "events.db") as db:
        if legacy:
            db.execute(CREATE_TABLE)
            db.execute(f"PRAGMA user_version={LEGACY_SCHEMA_VERSION}")
            db.execute("INSERT INTO events (ts,message) VALUES ('2026-10-15T12:00:00+00:00','retained')")
            db.commit()
        create_schema(db)
        create_schema(db)
        assert SCHEMA_VERSION > LEGACY_SCHEMA_VERSION
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        clauses, values = event_store_query._conditions(EventFilters(**{field: "absent"}), None, fts=False)
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM events WHERE " + " AND ".join(clauses)
            + " ORDER BY ts DESC, id DESC LIMIT ?", [*values, 1],
        ).fetchall()
        details = " ".join(row[-1] for row in plan)
        assert f"SEARCH events USING INDEX {index}" in details
        assert "TEMP B-TREE" not in details
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == int(legacy)


def test_broad_read_is_interrupted_and_releases_transaction(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as writer:
        create_schema(writer, enable_fts=False)
        writer.executemany(
            "INSERT INTO events (ts,message) VALUES (?,?)",
            [("2026-10-15T12:00:00+00:00", "ordinary message")] * SCAN_ROWS,
        )
        writer.commit()
        clock = SimpleNamespace(monotonic=Mock(side_effect=[0.0, 1.0]))
        with patch.object(event_store_query, "time", clock, create=True), pytest.raises(
            sqlite3.OperationalError, match=TOO_BROAD_MESSAGE,
        ):
            read_events(path, EventFilters(text="absent"), None, 1)
        writer.execute("PRAGMA busy_timeout=0")
        writer.execute("INSERT INTO events (ts,message) VALUES ('2026-10-15T12:00:00+00:00','after timeout')")
        writer.commit()
    assert read_events(path, EventFilters(), None, 1)["events"][0]["message"] == "after timeout"


def test_read_lock_wait_is_short(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        create_schema(db)
    statements: list[str] = []
    connect = sqlite3.connect

    def traced_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    with patch.object(event_store_query.sqlite3, "connect", side_effect=traced_connect):
        read_events(path, EventFilters(), None, 1)
    waits = [int(sql.split("=")[1]) for sql in statements if sql.startswith("PRAGMA busy_timeout=")]
    assert waits and max(waits) <= MAX_READ_WAIT_MS


@pytest.mark.asyncio
async def test_api_explains_interrupted_read(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db", enable_fts=False)
    await store.start()
    try:
        store.emit({"message": "seed"})
        await asyncio.wait_for(store.flush(), WAIT_SECONDS)
        with patch.object(event_store_query, "QUERY_PROGRESS_STEPS", 1, create=True), patch.object(
            event_store_query, "time", SimpleNamespace(monotonic=Mock(side_effect=[0.0, 1.0])), create=True,
        ):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(store)), base_url="http://test") as client:
                response = await client.get("/api/events", params={"text": "absent"})
        assert response.status_code == 422
        assert response.json()["error"] == TOO_BROAD_MESSAGE
        assert response.headers["cache-control"] == "no-store"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_commit_bursts_schedule_one_notification_per_second() -> None:
    store = SimpleNamespace(on_commit=None)
    broadcaster = SSEBroadcaster()
    queue = broadcaster.subscribe()
    ctx = DashboardContext(
        daemon=SimpleNamespace(state={"event_store": store}),
        store=SimpleNamespace(broadcaster=broadcaster), metrics=None,
        health=None, config_path=None, assistant_on=False,
    )
    app = _app(None)
    events.register(app, ctx)
    loop = Mock()
    with patch("asyncio.get_running_loop", return_value=loop):
        for _ in range(SCAN_ROWS):
            store.on_commit()
        assert queue.empty()
        loop.call_later.assert_called_once()
        delay, callback = loop.call_later.call_args.args
        assert delay == events.EVENTS_CHANGED_INTERVAL_SECONDS == NOTIFICATION_INTERVAL_SECONDS
        callback()
        assert queue.get_nowait() == {"_event": "events_changed"}
        store.on_commit()
        assert loop.call_later.call_count == 2
        assert queue.empty()
        loop.call_later.call_args.args[1]()
        assert queue.get_nowait() == {"_event": "events_changed"}


def test_events_changed_replaces_queued_notification_and_preserves_state() -> None:
    broadcaster = SSEBroadcaster()
    queue = broadcaster.subscribe()
    broadcaster.broadcast({"_event": "events_changed", "revision": "old"})
    broadcaster.broadcast({"snapshot": "first"})
    broadcaster.broadcast({"_event": "events_changed", "revision": "new"})
    assert queue.qsize() == 2
    queued = [queue.get_nowait(), queue.get_nowait()]
    assert {"snapshot": "first"} in queued
    assert {"_event": "events_changed", "revision": "new"} in queued


def test_full_sse_queue_gives_state_priority_over_event_notification() -> None:
    broadcaster = SSEBroadcaster()
    queue = broadcaster.subscribe()
    broadcaster.broadcast({"_event": "events_changed"})
    while not queue.full():
        broadcaster.broadcast({"snapshot": queue.qsize()})
    broadcaster.broadcast({"snapshot": "latest"})
    queued = [queue.get_nowait() for _ in range(queue.qsize())]
    assert {"snapshot": "latest"} in queued
    assert all(payload.get("_event") != "events_changed" for payload in queued)
    for _ in queued:
        queue.task_done()
    assert queue.empty()


def test_event_notification_never_displaces_full_queue_of_states() -> None:
    broadcaster = SSEBroadcaster()
    queue = broadcaster.subscribe()
    while not queue.full():
        broadcaster.broadcast({"snapshot": queue.qsize()})
    expected = queue.qsize()
    broadcaster.broadcast({"_event": "events_changed"})
    assert queue.qsize() == expected
    assert all(queue.get_nowait() == {"snapshot": index} for index in range(expected))
