from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinare.services import event_store, stats_sources
from coordinare.services.event_capture import capture_usage_records, install_event_store
from coordinare.services.event_store import EventStore
from coordinare.services.event_store_query import QUERY_TIMEOUT_SECONDS
from coordinare.services.run_statistics import DEFINITIONS, RunStatistics
from coordinare.services.stats_params import StatsQuery
from tests.unit.test_182_run_statistics import NOW, TABLE, VISIBLE, query, write_run

QUEUE_CAPACITY = 1
MILLISECONDS_PER_SECOND = 1000
PARTIAL_INPUT_TOKENS = 7
TEST_DROP_RUN_LIMIT = 3


def usage_record() -> dict[str, object]:
    return {
        "type": "usage", "seq": 1, "ts": NOW.isoformat(), "scope": "message",
        "usage": {"model": "m", "input_tokens": PARTIAL_INPUT_TOKENS},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("flush_before_close", [True, False])
@pytest.mark.parametrize("transcript_usage", [True, False])
async def test_full_queue_persists_usage_loss_and_falls_back_after_restart(
    tmp_path: Path, flush_before_close: bool, transcript_usage: bool,
) -> None:
    write_run(tmp_path, "run", "card")
    if not transcript_usage:
        path = tmp_path / "alpha" / "card" / "run.jsonl"
        path.write_text("".join(
            line for line in path.read_text().splitlines(keepends=True)
            if json.loads(line)["type"] != "usage"
        ))
    store = EventStore(tmp_path / "events.db", queue_size=QUEUE_CAPACITY, enable_fts=False)
    await store.start()
    store.emit({"ts": (NOW - timedelta(days=2)).isoformat(), "kind": "started"})
    await store.flush()
    install_event_store(store)
    try:
        capture_usage_records("run", "alpha", "card", "implementing", [usage_record()])
        capture_usage_records("run", "alpha", "card", "implementing", [usage_record()])
        if flush_before_close:
            await store.flush()
    finally:
        install_event_store(None)
        await store.close()
    with sqlite3.connect(store.path) as db:
        markers = db.execute("SELECT run_id FROM events WHERE kind='usage_events_dropped'").fetchall()
    assert markers == [("run",)]
    with patch.object(stats_sources, "_usage", wraps=stats_sources._usage) as fallback:
        body = await query(RunStatistics(tmp_path, event_path=store.path, table=TABLE))
    assert [call.args[1]["run"] for call in fallback.call_args_list] == ["run"]
    assert "usage_events_dropped" in body["partial_reasons"]
    assert body["coverage"]["usage_events_dropped_runs"] == 1
    if transcript_usage:
        assert body["totals"]["tokens"]["total"] == 165
    else:
        assert body["totals"]["missing_usage_runs"] == 1
        assert body["totals"]["cost"]["usd"] is None
        assert body["totals"]["tokens"]["total"] == 0


@pytest.mark.asyncio
async def test_full_queue_flags_usage_before_marker_can_be_persisted(tmp_path: Path) -> None:
    write_run(tmp_path, "run", "card")
    store = EventStore(tmp_path / "events.db", queue_size=QUEUE_CAPACITY, enable_fts=False)
    await store.start()
    store.emit({"ts": (NOW - timedelta(days=2)).isoformat(), "kind": "started"})
    await store.flush()
    store.emit({"ts": NOW.isoformat(), "run_id": "run", "symphony": "alpha", "card_id": "card",
                "kind": "run_usage", "data": usage_record()})
    await store.flush()
    release = asyncio.Event()
    persist = store._persist

    async def paused_persist(rows: list[str]) -> None:
        await release.wait()
        await persist(rows)

    install_event_store(store)
    try:
        with patch.object(store, "_persist", side_effect=paused_persist):
            store.emit({"ts": NOW.isoformat(), "kind": "filler"})
            capture_usage_records("run", "alpha", "card", "implementing", [usage_record()])
            try:
                with patch.object(stats_sources, "_usage", wraps=stats_sources._usage) as fallback:
                    body = await query(RunStatistics(tmp_path, event_path=store.path, table=TABLE))
                assert [call.args[1]["run"] for call in fallback.call_args_list] == ["run"]
                assert "usage_events_dropped" in body["partial_reasons"]
                assert body["totals"]["tokens"]["total"] == 165
            finally:
                release.set()
                await store.flush()
    finally:
        install_event_store(None)
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_lifecycle", [True, False])
@pytest.mark.parametrize("prune_usage", [True, False])
async def test_pruned_usage_suffix_uses_run_lifecycle_boundary(
    tmp_path: Path, persist_lifecycle: bool, prune_usage: bool,
) -> None:
    write_run(tmp_path, "run", "card")
    began = (NOW - timedelta(hours=1)).isoformat()
    lifecycle = {"kind": "dispatched", "stage": "implementing", "at": began,
                 "detail": {"run_id": "run"}}
    store = EventStore(tmp_path / "events.db", enable_fts=False)
    await store.start()
    store.emit({"ts": (NOW - timedelta(days=2)).isoformat(), "kind": "started"})
    for timestamp in (began, NOW.isoformat()):
        store.emit({"ts": timestamp, "run_id": "run", "symphony": "alpha", "card_id": "card",
                    "kind": "run_usage", "data": usage_record()})
    if persist_lifecycle:
        store.emit({"ts": began, "symphony": "alpha", "card_id": "card",
                    "kind": "card_lifecycle", "data": lifecycle})
    await store.close()
    if prune_usage:
        with sqlite3.connect(store.path) as db:
            db.execute("DELETE FROM events WHERE kind='run_usage' AND ts=?", (began,))
    snapshot = {} if persist_lifecycle else {"alpha": {"card": [lifecycle]}}
    with patch.object(stats_sources, "_usage", wraps=stats_sources._usage) as fallback:
        body = await RunStatistics(tmp_path, event_path=store.path, table=TABLE).query(
            StatsQuery(), visible=VISIBLE, lifecycle=snapshot, boards={}, now=NOW,
        )
    assert [call.args[1]["run"] for call in fallback.call_args_list] == (["run"] if prune_usage else [])
    assert ("usage_partially_pruned" in body["partial_reasons"]) is prune_usage
    assert body["coverage"]["usage_partially_pruned_runs"] == int(prune_usage)
    assert body["totals"]["tokens"]["total"] == (165 if prune_usage else PARTIAL_INPUT_TOKENS)


@pytest.mark.asyncio
async def test_usage_loss_tracker_is_bounded_and_overflow_stays_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(event_store, "MAX_USAGE_DROP_RUNS", TEST_DROP_RUN_LIMIT)
    write_run(tmp_path, "overflow", "card")
    store = EventStore(tmp_path / "events.db", queue_size=QUEUE_CAPACITY, enable_fts=False)
    await store.start()
    store.emit({"ts": (NOW - timedelta(days=2)).isoformat(), "kind": "started"})
    await store.flush()
    install_event_store(store)
    try:
        store.emit({"ts": NOW.isoformat(), "kind": "filler"})
        for index in range(TEST_DROP_RUN_LIMIT):
            capture_usage_records(str(index), "alpha", "card", "implementing", [usage_record()])
        capture_usage_records("overflow", "alpha", "card", "implementing", [usage_record()])
        assert len(store.usage_drop_snapshot()) == TEST_DROP_RUN_LIMIT
        await store.flush()
        assert not store.usage_drop_snapshot()
    finally:
        install_event_store(None)
        await store.close()
    body = await query(RunStatistics(tmp_path, event_path=store.path, table=TABLE))
    assert "usage_events_dropped" in body["partial_reasons"]
    assert body["totals"]["tokens"]["total"] == 165


def test_usage_read_bounds_matches_statement_timeout() -> None:
    milliseconds = QUERY_TIMEOUT_SECONDS * MILLISECONDS_PER_SECOND
    assert f"{milliseconds:g} ms deadline" in DEFINITIONS["usage_read_bounds"]
