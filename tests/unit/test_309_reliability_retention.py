from __future__ import annotations

import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from coordinare.services.event_store import EventStore
from coordinare.services.event_store_db import create_schema, prune_batch
from coordinare.services.event_store_settings import MIN_MAX_BYTES, EventStoreConfig
from coordinare.services.run_statistics import DEFINITIONS, RunStatistics
from coordinare.services.stats_params import StatsQuery
from coordinare.services.stats_poll_db import record_board_checks
from coordinare.services.stats_reliability import read_board_checks

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
WINDOW_DAYS = 30
SHORT_RETENTION_DAYS = 7
OLD_HOUR_DAYS = 10
EXPIRED_HOUR_DAYS = 40
PRUNE_BATCH_SIZE = 1
SCOPE = frozenset({"alpha", "beta"})
REPO = Path(__file__).resolve().parents[2]
NODE_TIMEOUT_SECONDS = 60
SIZE_PRESSURE_BYTES = 4 * 1024 * 1024
HOURLY_SCHEMA_VERSION = 5


def record_hour(path: Path, *, days: int, symphony: str = "alpha") -> None:
    with sqlite3.connect(path) as db:
        record_board_checks(db, [{
            "ts": (NOW - timedelta(days=days)).isoformat(), "symphony": symphony,
            "message": "", "data": '{"success":true}',
        }])


async def figure(
    path: Path, root: Path, *, symphony: str = "alpha", window: str = "30d", now: datetime = NOW,
) -> dict[str, Any]:
    body = await RunStatistics(root, event_path=path).query(
        StatsQuery(symphony=symphony, window=window), visible=SCOPE,
        lifecycle={}, boards={}, now=now,
    )
    assert body["partial"] is False, "board history loss must not mark run usage partial"
    return body["reliability"][symphony]


@pytest.mark.asyncio
@pytest.mark.parametrize("retention_days", [SHORT_RETENTION_DAYS, WINDOW_DAYS])
async def test_month_reliability_reports_short_retention_only(tmp_path: Path, retention_days: int) -> None:
    config = EventStoreConfig(retention_days=retention_days) if retention_days < WINDOW_DAYS else EventStoreConfig()
    store = EventStore(tmp_path / "events.db", config=config)
    await store.start()
    await store.close()
    record_hour(store.path, days=1)
    result = await figure(store.path, tmp_path)
    assert result["checks"] == 1
    assert result["success_rate"] == 1
    if retention_days < WINDOW_DAYS:
        assert result["partial"] is True
        assert any("retention_days" in reason and "7" in reason for reason in result["partial_reasons"])
    else:
        assert not result.get("partial")
        assert not result.get("partial_reasons")
    week = await figure(store.path, tmp_path, window="7d")
    assert not week.get("partial")
    assert not week.get("partial_reasons")


@pytest.mark.asyncio
@pytest.mark.parametrize("retention_days,checks,partial", [(7, 168, True), (30, 720, False)])
async def test_retention_prune_history_survives_restart_with_month_policy(
    tmp_path: Path, retention_days: int, checks: int, partial: bool,
) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
        record_board_checks(db, [
            {"ts": (NOW - timedelta(hours=hour)).isoformat(), "symphony": "alpha",
             "message": "", "data": '{"success":true}'}
            for hour in range(1, 961)
        ])
    with sqlite3.connect(path) as db, patch("coordinare.services.event_store_db.datetime") as clock:
        clock.now.return_value = NOW
        assert prune_batch(db, path, EventStoreConfig(retention_days=retention_days), 1000) == 960 - checks
    store = EventStore(path, config=EventStoreConfig(retention_days=WINDOW_DAYS), enable_fts=False)
    await store.start()
    await store.close()
    result = await figure(path, tmp_path)
    assert result["checks"] == checks
    assert result["success_rate"] == 1
    assert bool(result.get("partial")) is partial
    if partial:
        assert any("retention_days" in reason for reason in result["partial_reasons"])
        assert not any("max_bytes" in reason for reason in result["partial_reasons"])
    # Steady 30-day pruning removes only hours outside the month. An all-history
    # query still exposes that loss, proving retention markers were recorded.
    history = await figure(path, tmp_path, window="all")
    assert history["partial"] is True
    assert any("retention_days" in reason for reason in history["partial_reasons"])
    assert not (await figure(path, tmp_path, window="7d")).get("partial")
    assert not (await figure(path, tmp_path, symphony="beta")).get("partial")


@pytest.mark.asyncio
async def test_retention_and_size_prunes_keep_both_causes(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
    record_hour(path, days=OLD_HOUR_DAYS)
    record_hour(path, days=1)
    with sqlite3.connect(path) as db, patch("coordinare.services.event_store_db.datetime") as clock:
        clock.now.return_value = NOW
        assert prune_batch(db, path, EventStoreConfig(retention_days=SHORT_RETENTION_DAYS), 1) == 1
        with patch.object(Path, "stat") as stat:
            stat.return_value.st_size = MIN_MAX_BYTES + 1
            assert prune_batch(db, path, EventStoreConfig(max_bytes=MIN_MAX_BYTES), 1) == 1
    result = await figure(path, tmp_path)
    assert result["checks"] == 0
    assert result["partial"] is True
    assert len(result["partial_reasons"]) == 2
    assert any("retention_days" in reason for reason in result["partial_reasons"])
    assert any("max_bytes" in reason for reason in result["partial_reasons"])


@pytest.mark.asyncio
async def test_missing_retention_policy_row_reports_unknown_without_losing_totals(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
        db.execute("DELETE FROM stats_board_check_policy")
    record_hour(path, days=1)
    result = await figure(path, tmp_path)
    assert result["checks"] == 1
    assert result["success_rate"] == 1
    assert result["partial"] is True
    assert any("retention_days" in reason and "unknown" in reason for reason in result["partial_reasons"])


@pytest.mark.asyncio
async def test_reader_before_metadata_schema_reports_not_recorded_yet_then_recovers(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
        db.execute("DROP TABLE stats_board_check_policy")
        db.execute("DROP TABLE stats_board_check_pruning")
    record_hour(path, days=1)
    result = await figure(path, tmp_path)
    assert result["checks"] is None
    assert result["success_rate"] is None
    assert result["reason"] == "board_check_history_not_recorded_yet"
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
    result = await figure(path, tmp_path)
    assert result["checks"] == 1
    assert not result.get("partial")


def test_idle_prune_skips_unchanged_policy_without_write_transaction(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    statements: list[str] = []
    with sqlite3.connect(path) as db, patch("coordinare.services.event_store_db.datetime") as clock:
        clock.now.return_value = NOW
        create_schema(db, enable_fts=False)
        db.set_trace_callback(statements.append)
        assert prune_batch(db, path, EventStoreConfig(), 1) == 0
        assert not any(sql.startswith(("UPDATE stats_board_check_policy", "BEGIN")) for sql in statements)
        # A changed policy is still persisted, even when there is nothing to delete.
        assert prune_batch(db, path, EventStoreConfig(retention_days=SHORT_RETENTION_DAYS), 1) == 0
        assert db.execute("SELECT retention_days FROM stats_board_check_policy WHERE id=1").fetchone() == (7,)


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [OLD_HOUR_DAYS, EXPIRED_HOUR_DAYS])
async def test_size_prune_survives_restart_and_respects_window_and_scope(tmp_path: Path, days: int) -> None:
    path = tmp_path / "events.db"
    config = EventStoreConfig(retention_days=EXPIRED_HOUR_DAYS + WINDOW_DAYS, max_bytes=MIN_MAX_BYTES)
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
        db.execute(
            "INSERT INTO events(ts,message) VALUES (?,?)",
            (NOW.isoformat(), "x" * SIZE_PRESSURE_BYTES),
        )
    record_hour(path, days=days)
    record_hour(path, days=1, symphony="beta")
    with sqlite3.connect(path) as db, patch(
        "coordinare.services.event_store_db.datetime",
    ) as clock:
        clock.now.return_value = NOW
        assert path.stat().st_size > config.max_bytes
        assert prune_batch(db, path, config, PRUNE_BATCH_SIZE) == PRUNE_BATCH_SIZE
        assert db.execute("SELECT count(*) FROM stats_board_check_pruning").fetchone()[0] == 0
        assert prune_batch(db, path, config, PRUNE_BATCH_SIZE) == PRUNE_BATCH_SIZE
    # Reopen the writer, then read through a new statistics service.
    store = EventStore(path, config=config, enable_fts=False)
    await store.start()
    await store.close()
    result = await figure(path, tmp_path)
    assert result["checks"] == 0
    if days < WINDOW_DAYS:
        assert result["partial"] is True
        assert any("max_bytes" in reason for reason in result["partial_reasons"])
    else:
        assert not result.get("partial")
    unaffected = await figure(path, tmp_path, symphony="beta")
    assert unaffected["checks"] == 1
    assert not unaffected.get("partial")
    assert not (await figure(path, tmp_path, window="7d")).get("partial")


@pytest.mark.parametrize("retention_days", [SHORT_RETENTION_DAYS, WINDOW_DAYS])
def test_prune_marker_rolls_back_with_failed_deletion(tmp_path: Path, retention_days: int) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
    record_hour(path, days=OLD_HOUR_DAYS)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TRIGGER refuse_delete BEFORE DELETE ON stats_board_checks
            BEGIN SELECT RAISE(ABORT, 'refused'); END""")
        db.commit()
        with patch("coordinare.services.event_store_db.datetime") as clock, patch.object(Path, "stat") as stat:
            clock.now.return_value = NOW
            stat.return_value.st_size = MIN_MAX_BYTES + 1
            with pytest.raises(sqlite3.IntegrityError, match="refused"):
                prune_batch(
                    db, path, EventStoreConfig(retention_days=retention_days, max_bytes=MIN_MAX_BYTES), PRUNE_BATCH_SIZE,
                )
        assert db.execute("SELECT count(*) FROM stats_board_checks").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM stats_board_check_pruning").fetchone()[0] == 0


def test_retention_prune_requires_marker_to_commit(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
    record_hour(path, days=OLD_HOUR_DAYS)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TRIGGER refuse_marker BEFORE INSERT ON stats_board_check_pruning
            BEGIN SELECT RAISE(ABORT, 'refused'); END""")
        db.commit()
        with patch("coordinare.services.event_store_db.datetime") as clock:
            clock.now.return_value = NOW
            with pytest.raises(sqlite3.IntegrityError, match="refused"):
                prune_batch(db, path, EventStoreConfig(retention_days=SHORT_RETENTION_DAYS), PRUNE_BATCH_SIZE)
        assert db.execute("SELECT count(*) FROM stats_board_checks").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM stats_board_check_pruning").fetchone()[0] == 0
        assert db.execute("SELECT retention_days FROM stats_board_check_policy WHERE id=1").fetchone() == (30,)


def test_existing_size_marker_survives_retention_metadata_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
        db.execute("DROP TABLE stats_board_check_pruning")
        db.execute("""CREATE TABLE stats_board_check_pruning (
            symphony TEXT PRIMARY KEY, last_hour TEXT NOT NULL,
            previous_hour TEXT NOT NULL DEFAULT '') WITHOUT ROWID""")
        db.execute("INSERT INTO stats_board_check_pruning VALUES ('alpha',?, '')", ((NOW - timedelta(days=10)).isoformat(),))
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
    result, limited = read_board_checks(path, SCOPE, (NOW - timedelta(days=30)).isoformat(), NOW.isoformat())
    assert not limited
    assert result["alpha"]["partial"] is True
    assert len(result["alpha"]["partial_reasons"]) == 1
    assert "max_bytes" in result["alpha"]["partial_reasons"][0]
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
    again, _ = read_board_checks(path, SCOPE, (NOW - timedelta(days=30)).isoformat(), NOW.isoformat())
    assert again == result


def test_existing_hourly_schema_stays_compatible_without_losing_totals(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
        db.execute("DROP TABLE IF EXISTS stats_board_check_policy")
        db.execute("DROP TABLE IF EXISTS stats_board_check_pruning")
        db.execute(f"PRAGMA user_version={HOURLY_SCHEMA_VERSION}")
    record_hour(path, days=1)
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
        assert db.execute("PRAGMA user_version").fetchone()[0] == HOURLY_SCHEMA_VERSION
        assert db.execute("SELECT polls FROM stats_board_checks").fetchone()[0] == 1
        assert db.execute("SELECT retention_days FROM stats_board_check_policy").fetchone()[0] == WINDOW_DAYS


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted_days", [OLD_HOUR_DAYS, EXPIRED_HOUR_DAYS])
async def test_pruned_current_hour_only_affects_windows_after_it_completes(tmp_path: Path, deleted_days: int) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
    record_hour(path, days=deleted_days)
    record_hour(path, days=0)
    config = EventStoreConfig(retention_days=EXPIRED_HOUR_DAYS + WINDOW_DAYS, max_bytes=MIN_MAX_BYTES)
    with sqlite3.connect(path) as db, patch("coordinare.services.event_store_db.datetime") as clock, patch.object(Path, "stat") as stat:
        clock.now.return_value = NOW
        stat.return_value.st_size = config.max_bytes + 1
        assert prune_batch(db, path, config, PRUNE_BATCH_SIZE) == PRUNE_BATCH_SIZE
        assert prune_batch(db, path, config, PRUNE_BATCH_SIZE) == PRUNE_BATCH_SIZE
    assert bool((await figure(path, tmp_path)).get("partial")) is (deleted_days < WINDOW_DAYS)
    completed = await figure(path, tmp_path, now=NOW + timedelta(hours=1))
    assert completed["partial"] is True
    assert any("max_bytes" in reason for reason in completed["partial_reasons"])


def test_reliability_definition_names_both_history_limits() -> None:
    definition = DEFINITIONS["reliability"]
    assert "retention_days" in definition
    assert "max_bytes" in definition
    assert "partial" in definition.lower()
    assert "session" not in definition.lower()
    assert "attempt" not in definition.lower()


def test_reliability_partial_rendering_under_node(tmp_path: Path) -> None:
    scripts = REPO / "src/coordinare/dashboard/dashboard_static"
    names = ("helpers", "usage", "waiting", "runs", "symphonies", "bootstrap", "stats_charts", "symphony_stats")
    bundle = tmp_path / "stats.js"
    bundle.write_text("\n".join((scripts / f"{name}.js").read_text() for name in names))
    result = subprocess.run(
        ["node", str(REPO / "tests/js/reliability_309_checks.js"), str(bundle)],
        capture_output=True, text=True, check=False, timeout=NODE_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stdout + result.stderr
