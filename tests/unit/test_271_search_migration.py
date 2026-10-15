"""Search budgets and online filter index migration regressions."""
from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from coordinare.services import event_store as writer_module
from coordinare.services import event_store_db, event_store_query
from coordinare.services.event_store import EventStore
from coordinare.services.event_store_query import EventFilters, read_events

LARGE_TABLE_ROWS = 600_000
READ_BUDGET_SECONDS = 0.250
PAGE_SIZE = 7
PAGING_ROWS = 40
WAIT_SECONDS = 10.0
MILLISECONDS_PER_SECOND = 1000
FILTER_INDEX_COUNT = 3
LEGACY_SCHEMA_VERSION = 3
MATCH_INTERVAL = 2
STAMP = "2026-10-15T12:00:00+00:00"
LATE_STAMP = "2026-10-14T12:00:00+00:00"
FILTER_INDEX_NAMES = {"events_card_ts", "events_source_ts", "events_level_ts"}


def _filter_indexes(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA index_list(events)")} & FILTER_INDEX_NAMES


@pytest.mark.parametrize("fts", [True, False])
def test_271_large_fts_search_streams_within_budget(tmp_path: Path, fts: bool) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        db.execute(event_store_db.CREATE_TABLE)
        db.execute(
            """WITH RECURSIVE n(id) AS (SELECT 1 UNION ALL SELECT id+1 FROM n WHERE id < ?)
            INSERT INTO events (ts,message) SELECT ?, CASE WHEN id % ? = 0
            THEN 'needle match' ELSE 'ordinary message' END FROM n""",
            (LARGE_TABLE_ROWS, STAMP, MATCH_INTERVAL),
        )
        db.commit()
        assert event_store_db.create_schema(db, enable_fts=fts) == fts
        while event_store_db.migrate_filter_index(db):
            pass
    started = perf_counter()
    result = read_events(path, EventFilters(text="needle", source="daemon"), None, PAGE_SIZE)
    elapsed = perf_counter() - started
    assert len(result["events"]) == PAGE_SIZE
    assert elapsed < READ_BUDGET_SECONDS
    assert event_store_query.QUERY_TIMEOUT_SECONDS == READ_BUDGET_SECONDS
    print(f"fts={fts} table_rows={LARGE_TABLE_ROWS} matching_rows={LARGE_TABLE_ROWS // MATCH_INTERVAL} elapsed_ms={elapsed * MILLISECONDS_PER_SECOND:.3f}")


@pytest.mark.parametrize("fts", [True, False])
def test_271_text_pages_follow_ids_across_late_events(tmp_path: Path, fts: bool) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        event_store_db.create_schema(db, enable_fts=fts)
        db.executemany(
            "INSERT INTO events (ts,message) VALUES (?, 'needle')",
            [(STAMP if row_id % MATCH_INTERVAL else LATE_STAMP,) for row_id in range(PAGING_ROWS)],
        )
    first = read_events(path, EventFilters(text="needle"), None, PAGE_SIZE)
    found = [row["id"] for row in first["events"]]
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO events (ts,message) VALUES (?, 'needle')", (LATE_STAMP,))
    cursor = first["cursor"]
    assert json.loads(base64.urlsafe_b64decode(cursor))[0] is None
    while cursor:
        page = read_events(path, EventFilters(text="needle"), cursor, PAGE_SIZE)
        found.extend(row["id"] for row in page["events"])
        cursor = page["cursor"]
    assert found == list(range(PAGING_ROWS, 0, -1))
    assert read_events(path, EventFilters(text="needle"), None, PAGE_SIZE)["events"][0]["id"] == PAGING_ROWS + 1


@pytest.mark.parametrize("legacy", [True, False])
def test_271_filter_migration_resumes_one_committed_index_per_slice(tmp_path: Path, legacy: bool) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        if legacy:
            db.execute(event_store_db.CREATE_TABLE)
            for name, columns in event_store_db.BASELINE_INDEXES:
                db.execute(f"CREATE INDEX events_{name} ON events ({columns})")
            db.execute(f"PRAGMA user_version={LEGACY_SCHEMA_VERSION}")
            db.execute("INSERT INTO events (ts,message) VALUES (?, 'legacy')", (STAMP,))
            db.commit()
        event_store_db.create_schema(db, enable_fts=False)
        assert not _filter_indexes(db)
        assert len(read_events(path, EventFilters(stage=""), None, PAGE_SIZE)["events"]) == int(legacy)
        with capture_logs() as logs:
            assert event_store_db.migrate_filter_index(db)
        assert len(_filter_indexes(db)) == 1
        assert logs[0]["event"] == "event_store.index_built"
        assert logs[0]["duration_seconds"] >= 0
        completed = _filter_indexes(db)
    db.close()
    with sqlite3.connect(path) as db:
        event_store_db.create_schema(db, enable_fts=False)
        assert _filter_indexes(db) == completed
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        while event_store_db.migrate_filter_index(db):
            pass
        assert len(_filter_indexes(db)) == FILTER_INDEX_COUNT
        assert sum(sql == "BEGIN IMMEDIATE" for sql in statements) == FILTER_INDEX_COUNT - len(completed)
        assert sum(sql == "COMMIT" for sql in statements) == FILTER_INDEX_COUNT - len(completed)
        assert db.execute("PRAGMA user_version").fetchone()[0] == event_store_db.SCHEMA_VERSION


@pytest.mark.asyncio
async def test_271_writer_commits_between_index_slices(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db", enable_fts=False)
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()
    counts: list[int] = []
    migrate = event_store_db.migrate_filter_index

    def slice_with_ingress(db: sqlite3.Connection) -> bool:
        counts.append(db.execute("SELECT count(*) FROM events").fetchone()[0])
        assert store.health()["writer_state"] != "starting"
        active = migrate(db)
        if active:
            loop.call_soon_threadsafe(store.emit, {"message": "arrived during migration"})
        else:
            loop.call_soon_threadsafe(finished.set)
        return active

    with patch.object(writer_module, "migrate_filter_index", side_effect=slice_with_ingress):
        assert store.emit({"message": "before maintenance"})
        await store.start()
        try:
            await asyncio.wait_for(finished.wait(), WAIT_SECONDS)
            await asyncio.wait_for(store.flush(), WAIT_SECONDS)
            assert counts == list(range(1, FILTER_INDEX_COUNT + 2))
            assert store.health()["dropped"] == 0
        finally:
            await store.close()


def test_271_stage_index_is_retired_in_its_own_slice(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "events.db") as db:
        event_store_db.create_schema(db, enable_fts=False)
        db.execute("CREATE INDEX events_stage_ts ON events (stage, ts)")
        with capture_logs() as logs:
            assert event_store_db.migrate_filter_index(db)
        assert "events_stage_ts" not in {row[1] for row in db.execute("PRAGMA index_list(events)")}
        assert not _filter_indexes(db)
        assert logs[0]["event"] == "event_store.index_dropped"
        assert logs[0]["duration_seconds"] >= 0


@pytest.mark.parametrize("fts", [True, False])
def test_271_text_query_plan_has_no_sort_with_filter_index(tmp_path: Path, fts: bool) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        event_store_db.create_schema(db, enable_fts=fts)
        while event_store_db.migrate_filter_index(db):
            pass
    statements: list[str] = []
    connect = sqlite3.connect

    def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    with patch.object(event_store_query.sqlite3, "connect", side_effect=traced_connect):
        read_events(path, EventFilters(source="daemon", text="needle"), None, PAGE_SIZE)
    select = next(sql for sql in statements if "ORDER BY" in sql)
    with sqlite3.connect(path) as db:
        plan = db.execute("EXPLAIN QUERY PLAN " + select).fetchall()
    assert not any("TEMP B-TREE" in row[-1] for row in plan)


def test_271_interrupted_index_build_retries_without_losing_progress(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "events.db") as db:
        event_store_db.create_schema(db, enable_fts=False)
        assert event_store_db.migrate_filter_index(db)
        completed = _filter_indexes(db)

        def interrupt() -> int:
            return 1

        def interrupt_build(action: int, name: str, _table: str, _database: str, _trigger: str) -> int:
            if action == sqlite3.SQLITE_CREATE_INDEX and name == "events_source_ts":
                db.set_progress_handler(interrupt, 1)
            return sqlite3.SQLITE_OK

        db.set_authorizer(interrupt_build)
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            event_store_db.migrate_filter_index(db)
        db.set_progress_handler(None, 0)
        db.set_authorizer(None)
        assert not db.in_transaction
        assert _filter_indexes(db) == completed
        assert event_store_db.migrate_filter_index(db)
        assert len(_filter_indexes(db)) == len(completed) + 1
