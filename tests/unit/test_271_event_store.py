from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from coordinare.config import EventStoreConfig
from coordinare.services.event_store import EventStore
from coordinare.services.event_store_db import CREATE_TABLE, SCHEMA_VERSION, create_schema
from coordinare.services.event_store_query import EventFilters

TOKEN = "ghp_" + "a" * 36
EVENT_COUNT = 40
PRODUCERS = 8
WAIT_SECONDS = 30.0
POLL_SECONDS = 0.01
RETENTION_SIZE_BUDGET = 96 * 1024


def event(**overrides: object) -> dict[str, object]:
    return {"kind": "progress", "message": "needle found", "data": {}, **overrides}


async def drain(store: EventStore) -> None:
    await asyncio.wait_for(store.flush(), timeout=WAIT_SECONDS)


@pytest.mark.asyncio
async def test_schema_and_shutdown_flush(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    assert not store.path.exists()
    await store.start()
    assert store.emit(event())
    await store.close()
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        assert columns == {
            "id",
            "ts",
            "symphony",
            "card_id",
            "run_id",
            "stage",
            "source",
            "kind",
            "level",
            "message",
            "data",
        }
        indexes = {row[1] for row in db.execute("PRAGMA index_list(events)")}
        assert {"events_ts", "events_symphony_ts", "events_run_ts", "events_kind_ts"} <= indexes


@pytest.mark.parametrize("fts", [True, False])
def test_schema_migrates_existing_rows(tmp_path: Path, fts: bool) -> None:
    with sqlite3.connect(tmp_path / "events.db") as db:
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        db.execute(CREATE_TABLE)
        db.execute("PRAGMA user_version=1")
        assert not db.execute("PRAGMA index_list(events)").fetchall()
        db.execute(
            "INSERT INTO events (ts, message) VALUES ('2026-01-01T00:00:00+00:00', 'migration needle')",
        )
        db.commit()
        create_schema(db, enable_fts=fts)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT message FROM events").fetchone()[0] == "migration needle"
        if fts:
            assert (
                db.execute(
                    "SELECT count(*) FROM events_fts WHERE events_fts MATCH 'needle'",
                ).fetchone()[0]
                == 1
            )
        create_schema(db, enable_fts=fts)
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_queue_overflow_counts_drops(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db", queue_size=1)
    assert store.emit(event())
    assert not store.emit(event())
    assert store.health()["dropped"] == 1
    await store.start()
    await store.close()
    assert store.health()["rows"] == 1


@pytest.mark.asyncio
async def test_write_error_is_isolated_and_writer_recovers(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    with patch.object(store, "_write_batch", side_effect=sqlite3.OperationalError("unavailable")):
        assert store.emit(event())
        await drain(store)
    assert store.health()["dropped"] == 1
    assert store.emit(event(message="recovered"))
    await store.close()
    assert store.health()["rows"] == 1


@pytest.mark.asyncio
async def test_redaction_covers_every_column_and_snapshot(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    payload = event(
        message=f"token {TOKEN}",
        symphony=TOKEN,
        data={"nested": [TOKEN], "password": "plain-secret"},
    )
    assert store.emit(payload)
    payload["message"] = "changed"
    await store.start()
    await store.close()
    with sqlite3.connect(store.path) as db:
        dumped = json.dumps(db.execute("SELECT * FROM events").fetchall())
    assert TOKEN not in dumped
    assert "plain-secret" not in dumped
    assert "changed" not in dumped
    assert "REDACTED" in dumped


@pytest.mark.asyncio
async def test_many_producers_share_one_writer(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    thread_ids: set[int] = set()
    original = store._write_batch

    def write(rows: list[str]) -> None:
        thread_ids.add(threading.get_ident())
        original(rows)

    async def produce(producer: int) -> None:
        for index in range(EVENT_COUNT):
            assert store.emit(event(message=f"{producer}:{index}"))
            await asyncio.sleep(0)

    with patch.object(store, "_write_batch", side_effect=write):
        await asyncio.gather(*(produce(n) for n in range(PRODUCERS)))
        await drain(store)
    await store.close()
    assert store.health()["rows"] == EVENT_COUNT * PRODUCERS
    assert len(thread_ids) == 1
    assert threading.get_ident() not in thread_ids


@pytest.mark.asyncio
async def test_foreign_threads_use_bounded_ingress(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db", queue_size=1)
    await store.start()
    accepted: list[bool] = []
    producer = threading.Thread(
        target=lambda: accepted.extend(store.emit(event()) for _ in range(EVENT_COUNT)),
    )
    producer.start()
    # Hold the loop while the foreign producer fills ingress callbacks. The
    # queue budget must cover these callbacks as well as enqueued rows.
    producer.join(timeout=1)
    assert not producer.is_alive()
    assert sum(accepted) == 1
    await store.close()
    assert store.health()["rows"] + store.health()["dropped"] == EVENT_COUNT


@pytest.mark.parametrize(
    "field,value", [("retention_days", 0), ("max_bytes", 0), ("unexpected", True)],
)
def test_config_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        EventStoreConfig.model_validate({field: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("fts", [True, False])
async def test_queries_all_filters_and_cursor_ties(tmp_path: Path, fts: bool) -> None:
    store = EventStore(tmp_path / "events.db", enable_fts=fts)
    stamp = "2026-10-15T12:00:00+00:00"
    fields = {
        "symphony": "garden",
        "card_id": "card",
        "run_id": "run",
        "stage": "implementer",
        "source": "performer",
        "kind": "tool_use",
        "level": "error",
    }
    await store.start()
    for index in range(EVENT_COUNT):
        store.emit(event(ts=stamp, message=f"needle {index}", **fields))
    store.emit(event(ts=stamp, symphony="other", message="other"))
    await drain(store)
    filters = EventFilters(**fields, start=stamp, end=stamp, text="needle")
    first = await store.query_events(filters, limit=7)
    second = await store.query_events(filters, cursor=first["cursor"], limit=7)
    assert len(first["events"]) == 7
    assert len(second["events"]) == 7
    assert first["events"][-1]["id"] > second["events"][0]["id"]
    for key in fields:
        assert not (await store.query_events(EventFilters(**{key: "absent"})))["events"]
    assert not (await store.query_events(EventFilters(start="2027-01-01T00:00:00Z")))["events"]
    assert not (await store.query_events(EventFilters(end="2025-01-01T00:00:00Z")))["events"]
    assert not (await store.query_events(EventFilters(text="missing")))["events"]
    await store.close()


@pytest.mark.asyncio
async def test_fresh_store_finishes_empty_index_builds_before_readers(tmp_path: Path) -> None:
    from coordinare.services.event_store_db import FILTER_INDEXES

    store = EventStore(tmp_path / "events.db")
    await store.start()
    try:
        await store._call(store._initialize)
        await drain(store)
        with sqlite3.connect(store.path) as db:
            indexes = {row[1] for row in db.execute("PRAGMA index_list(events)")}
        assert {f"events_{name}" for name, _ in FILTER_INDEXES} <= indexes
        assert not store._filter_migration_active
        assert (await store.query_events())["events"] == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_query_limit_cursor_validation_and_unusual_text(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    store.emit(event(message='literal " OR * % _'))
    await drain(store)
    assert len((await store.query_events(limit=10000))["events"]) == 1
    assert (await store.query_events(EventFilters(text='" OR *')))["events"]
    with pytest.raises(ValueError):
        await store.query_events(cursor="invalid")
    with pytest.raises(ValueError):
        await store.query_events(limit=0)
    await store.close()


@pytest.mark.asyncio
async def test_retention_age_batches_and_health(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db", prune_batch_size=3)
    await store.start()
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    for _ in range(8):
        store.emit(event(ts=old))
    store.emit(event(message="keep"))
    await drain(store)
    before = store.health()
    assert before["rows"] == 9
    assert before["oldest_ts"] == old
    assert before["size_bytes"] == store.path.stat().st_size
    assert await store.prune() == 3
    assert await store.prune() == 3
    assert await store.prune() == 2
    assert await store.prune() == 0
    assert store.health()["rows"] == 1
    assert store.health()["pruned"] == 8
    await store.close()


@pytest.mark.asyncio
async def test_retention_size_deletes_oldest_and_reclaims_pages(tmp_path: Path) -> None:
    config = EventStoreConfig(max_bytes=RETENTION_SIZE_BUDGET)
    store = EventStore(tmp_path / "events.db", config=config, prune_batch_size=2)
    await store.start()
    for index in range(12):
        store.emit(event(message=f"old {index} " + "payload " * 1200))
    await drain(store)
    previous = store.health()["rows"]
    async with asyncio.timeout(WAIT_SECONDS):
        while store.health()["size_bytes"] > config.max_bytes:
            removed = await store.prune()
            assert removed <= 2
            assert store.health()["rows"] <= previous
            previous = store.health()["rows"]
            await asyncio.sleep(POLL_SECONDS)
    assert store.health()["size_bytes"] <= config.max_bytes
    assert store.health()["pruned"] > 0
    rows = (await store.query_events())["events"]
    if rows:
        assert rows[0]["message"].startswith("old 11 ")
    await store.close()


@pytest.mark.asyncio
async def test_shutdown_wait_is_bounded(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    release = threading.Event()
    entered = threading.Event()
    original = store._write_batch

    def blocked(rows: list[str]) -> None:
        entered.set()
        release.wait(timeout=5)
        original(rows)

    with patch.object(store, "_write_batch", side_effect=blocked):
        store.emit(event())
        await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(store.close(grace_seconds=0.01), timeout=1)
        assert not store.emit(event())
        release.set()
        await asyncio.wait_for(store.wait_closed(), timeout=5)


@pytest.mark.asyncio
async def test_query_page_is_stable_across_new_inserts_and_pruning(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    for index in range(5):
        store.emit(event(ts=f"2026-10-15T12:00:0{index}Z"))
    await drain(store)
    page = await store.query_events(limit=2)
    store.emit(event(ts="2026-10-15T12:00:09Z"))
    await drain(store)
    rest = await store.query_events(cursor=page["cursor"], limit=2)
    assert [row["id"] for row in rest["events"]] == [3, 2]
    last = await store.query_events(cursor=rest["cursor"], limit=2)
    assert [row["id"] for row in last["events"]] == [1]
    assert last["cursor"] is None
    await store.close()


@pytest.mark.asyncio
async def test_idle_background_pruning_continues_in_small_batches(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db", prune_batch_size=2)
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    for _ in range(7):
        store.emit(event(ts=old))
    with patch("coordinare.services.event_store.MAINTENANCE_SECONDS", 0.01):
        await store.start()
        await drain(store)
        async with asyncio.timeout(5):
            while store.health()["pruned"] < 7:
                await asyncio.sleep(0.01)
    assert store.health()["rows"] == 0
    assert store.health()["oldest_ts"] is None
    await store.close()


@pytest.mark.asyncio
async def test_bad_and_oversized_events_drop_without_reaching_disk(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    cyclic: dict[str, object] = {}
    cyclic["cycle"] = cyclic
    assert not store.emit(cyclic)
    assert not store.emit(event(message="large" * 100000))
    assert not store.emit(event(ts="invalid"))
    assert store.health()["dropped"] == 3
    await store.close()


def test_unknown_schema_is_refused_without_downgrading(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "events.db") as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        with pytest.raises(ValueError, match="newer"):
            create_schema(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1


class NoFTSConnection(sqlite3.Connection):
    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        if sql.startswith("CREATE VIRTUAL TABLE"):
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, parameters)


def test_runtime_without_fts_keeps_schema_usable(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "events.db", factory=NoFTSConnection) as db:
        assert not create_schema(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        db.execute(
            "INSERT INTO events (ts, message) VALUES ('2026-10-15T00:00:00+00:00', 'needle')",
        )
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_reopen_restores_health_and_preserves_fts_after_prune(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path)
    await store.start()
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    store.emit(event(ts=old, message="expiredneedle"))
    store.emit(event(message="retainedneedle"))
    await drain(store)
    await store.prune()
    assert not (await store.query_events(EventFilters(text="expiredneedle")))["events"]
    await store.close()
    reopened = EventStore(path)
    await reopened.start()
    reopened.emit(event(message="newneedle"))
    await drain(reopened)
    assert reopened.health()["rows"] == 2
    assert len((await reopened.query_events(EventFilters(text="retainedneedle")))["events"]) == 1
    await reopened.close()


@pytest.mark.asyncio
async def test_read_queries_run_off_loop_on_a_separate_connection(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    store.emit(event())
    await drain(store)
    loop_thread = threading.get_ident()
    original = sqlite3.connect
    query_threads: list[int] = []

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        query_threads.append(threading.get_ident())
        assert kwargs.get("uri") is True
        assert "mode=ro" in str(args[0])
        return original(*args, **kwargs)

    with patch("coordinare.services.event_store_query.sqlite3.connect", side_effect=connect):
        await asyncio.gather(*(store.query_events() for _ in range(PRODUCERS)))
    assert len(query_threads) == PRODUCERS
    assert loop_thread not in query_threads
    await store.close()


@pytest.mark.asyncio
async def test_cleanup_errors_never_raise_into_daemon(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    store.emit(event())
    await drain(store)
    original = store._close_connection

    def failing_close() -> None:
        original()
        raise sqlite3.OperationalError("cleanup failed")

    with patch.object(store, "_close_connection", side_effect=failing_close):
        await store.close()


def test_migration_failure_rolls_back_schema_and_version(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "events.db") as db:
        with (
            patch(
                "coordinare.services.event_store_db._create_fts",
                side_effect=RuntimeError("migration failed"),
            ),
            pytest.raises(RuntimeError),
        ):
            create_schema(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='events'").fetchone()
        create_schema(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_query_limit_is_capped_and_cursor_survives_deleted_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("coordinare.services.event_store.MAINTENANCE_SECONDS", WAIT_SECONDS * 2)
    store = EventStore(tmp_path / "events.db", prune_batch_size=1000)
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    await store.start()
    for _ in range(510):
        assert store.emit(event(ts=old))
    await drain(store)
    page = await store.query_events(limit=10000)
    assert len(page["events"]) == 500
    assert page["cursor"] is not None
    assert await store.prune() == 510
    after = await store.query_events(cursor=page["cursor"])
    assert after == {"events": [], "cursor": None}
    await store.close()
