from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from coordinare.graph.nodes import monitor_performer
from coordinare.graph.state import initial_state
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import stats_sources
from coordinare.services.card_lifecycle import CardLifecycleLog
from coordinare.services.event_capture import install_event_store
from coordinare.services.event_store import EventStore
from coordinare.services.event_store_query import EventQueryTooBroad
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.run_statistics import RunStatistics
from coordinare.services.run_transcript import (
    RunTranscriptWriter,
    TranscriptCollector,
    transcript_path,
)
from coordinare.services.stats_flow import card_flow
from coordinare.services.stats_params import StatsQuery
from coordinare.services.stats_usage import MAX_USAGE_GROUPS, USAGE_SQL
from coordinare.services.token_usage import TokenLedger
from tests.unit.test_182_run_statistics import NOW, TABLE, VISIBLE, query, write_run


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ['{"type":"usage",', '{"type":"usage","seq":9,"usage":{"input_tokens":999}}'])
async def test_partial_last_line_preserves_complete_usage(tmp_path: Path, tail: str) -> None:
    write_run(tmp_path, "run", "card")
    path = transcript_path(tmp_path, symphony="alpha", card_id="card", run_id="run")
    path.write_text(path.read_text() + tail)
    body = await query(RunStatistics(tmp_path, table=TABLE))
    assert body["totals"]["tokens"]["total"] == 165
    assert body["totals"]["cost"]["usd"] == pytest.approx(0.00025)
    assert body["partial_reasons"] == ["usage_partial_line"]


@pytest.mark.parametrize("closed_hours,seconds,samples", [(30, None, 0), (24, None, 0), (23, 3600, 1), (None, 86400, 1)])
def test_waits_count_only_positive_window_overlap(closed_hours: int | None, seconds: int | None, samples: int) -> None:
    records = [{"kind": "human_review_started", "at": (NOW - timedelta(hours=48)).isoformat()}]
    if closed_hours is not None:
        records.append({"kind": "human_review_ended", "at": (NOW - timedelta(hours=closed_hours)).isoformat()})
    flow = card_flow({("alpha", "card"): records}, {}, VISIBLE, NOW - timedelta(hours=24), NOW)["alpha"]
    assert flow["human_review_wait_seconds"] == seconds
    assert flow["human_review_wait_samples"] == samples


@pytest.mark.asyncio
async def test_completion_is_separate_from_verdict_rates(tmp_path: Path) -> None:
    for run, persona in (("implement", "implementer"), ("pass", "reviewer"), ("fail", "reviewer"), ("no-verdict", "reviewer")):
        write_run(tmp_path, run, run, persona=persona)
    events = {"alpha": {run: [{"kind": "verdict", "stage": "reviewing", "at": NOW.isoformat(),
                              "detail": {"run_id": run, "result": result}}]
                        for run, result in (("pass", "pass"), ("fail", "fail"))}}
    body = await RunStatistics(tmp_path).query(StatsQuery(group_by="stage"), visible=VISIBLE, lifecycle=events, boards={}, now=NOW)
    groups = {group["key"]: group for group in body["groups"]}
    assert groups["implementing"]["outcomes"]["completed_without_verdict"] == 1
    assert groups["implementing"]["success_rate"] is None
    assert groups["implementing"]["first_pass_rate"] is None
    review = groups["reviewing"]
    assert review["outcomes"]["success"] == review["outcomes"]["failure"] == 1
    assert review["outcomes"]["completed_without_verdict"] == 1
    assert review["verdict_runs"] == review["resolved_card_stages"] == 2
    assert review["success_rate"] == review["first_pass_rate"] == 0.5
    assert "verdict" in body["definitions"]["success_rate"]
    assert StatsQuery(outcome="completed_without_verdict").outcome == "completed_without_verdict"


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected,unattributed", [("overlap", 0, 2), ("gap", 0, 2), ("single", 1, 0), ("explicit", 1, 0)])
async def test_legacy_observations_require_one_open_run(tmp_path: Path, case: str, expected: int, unattributed: int) -> None:
    write_run(tmp_path, "first", "card", persona="reviewer", hours=3, duration=7200)
    write_run(tmp_path, "second", "card", persona="reviewer", hours=2, duration=1800)
    at = NOW - timedelta(minutes=90 if case == "overlap" else 30 if case == "gap" else 150)
    detail = {"run_id": "second"} if case == "explicit" else {}
    events = {"alpha": {"card": [
        {"kind": "verdict", "stage": "reviewer", "at": at.isoformat(), "detail": {**detail, "result": "pass"}},
        {"kind": "ci_result", "stage": "reviewing", "at": at.isoformat(), "detail": {**detail, "result": "BOUNCE"}},
    ]}}
    body = await RunStatistics(tmp_path).query(StatsQuery(), visible=VISIBLE, lifecycle=events, boards={}, now=NOW)
    assert body["totals"]["outcomes"]["success"] == expected
    assert body["totals"]["bounces"] == expected
    assert body["coverage"]["unattributed_observations"] == unattributed


@pytest.mark.asyncio
async def test_usage_prefers_indexed_store_and_falls_back_only_before_coverage(tmp_path: Path) -> None:
    for run, hours in (("old", 72), ("stored", 1), ("missing", 1)):
        write_run(tmp_path, run, run, hours=hours)
    store = EventStore(tmp_path / "events.db", enable_fts=False)
    await store.start()
    store.emit({"ts": (NOW - timedelta(hours=48)).isoformat(), "kind": "started"})
    records = [
        {"type": "cost", "scope": "message", "usage": {"model": "m", "input_tokens": 7}},
        {"type": "usage", "scope": "run", "usage": {"model": "m", "input_tokens": 999}},
    ]
    for seq, record in enumerate(records, start=1):
        store.emit({"ts": NOW.isoformat(), "run_id": "stored", "symphony": "alpha", "card_id": "stored",
                    "source": "performer", "kind": record["type"], "data": {**record, "transcript_seq": seq}})
    store.emit({"ts": NOW.isoformat(), "run_id": "stored", "symphony": "hidden", "card_id": "stored",
                "source": "performer", "kind": "usage", "data": {"usage": {"input_tokens": 9999}, "transcript_seq": 3}})
    await store.close()
    with patch.object(stats_sources, "_usage", wraps=stats_sources._usage) as fallback:
        body = await query(RunStatistics(tmp_path, event_path=store.path, table=TABLE))
    assert [call.args[1]["run"] for call in fallback.call_args_list] == ["old"]
    assert body["totals"]["tokens"]["input"] == 107
    assert body["totals"]["missing_usage_runs"] == 1
    assert "usage_events_missing" in body["partial_reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize("offset_timestamps", [True, False])
async def test_store_usage_matches_ledger_scope_and_cumulative_rules(tmp_path: Path, offset_timestamps: bool) -> None:
    records = [
        {"seq": 1, "ts": (NOW - timedelta(days=2)).isoformat(), "scope": "run", "usage": {"model": "m", "input_tokens": 100}},
        {"seq": 2, "ts": (NOW - timedelta(days=2)).isoformat(), "scope": "run", "usage": {"model": "m", "input_tokens": 120}},
        {"seq": 3, "ts": NOW.isoformat(), "scope": "run", "usage": {"model": "other", "input_tokens": 180}},
    ]
    if offset_timestamps:
        records[0]["ts"] = "2026-10-15T00:30:00+02:00"
        records[1]["ts"] = "2026-10-14T23:30:00+00:00"
        records[2]["ts"] = "2026-10-15T01:30:00+02:00"
    store = EventStore(tmp_path / "events.db", enable_fts=False)
    await store.start()
    for seq, record in enumerate(records, start=1):
        store.emit({"ts": record["ts"], "run_id": "run", "symphony": "alpha", "card_id": "card", "source": "performer",
                    "kind": "usage", "data": {**record, "type": "usage", "transcript_seq": seq}})
    await store.close()
    rows = [{"run": "run", "card": "card", "attributed_symphony": "alpha"}]
    totals, _, _ = stats_sources.read_stats_usage(store.path, rows, NOW.isoformat())
    ledger = TokenLedger()
    ledger.observe("run", symphony="alpha", card="card", stage="reviewing", records=[{**record, "type": "usage"} for record in records])
    assert totals == ledger.totals_by_run(["run"])
    with sqlite3.connect(store.path) as db:
        plan = db.execute("EXPLAIN QUERY PLAN " + USAGE_SQL, (json.dumps(rows), NOW.isoformat(), MAX_USAGE_GROUPS + 1)).fetchall()
    assert "events_run_ts" in str(plan)


@pytest.mark.asyncio
async def test_accepted_transcript_usage_reaches_store(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db", enable_fts=False)
    await store.start()
    collector = TranscriptCollector(tmp_path / "transcripts", enabled=True, max_bytes_per_run=10000)
    service = HTTPPerformerService(PerformerEndpointConfig(id="test", mode="persistent", endpoint="http://unused", image="test", roles=["implementer"]), transcripts=collector)
    writer = RunTranscriptWriter(collector.root, parts=("alpha", "card"), run_id="run", max_bytes=10000)
    writer.set_stage("reviewing")
    record = {"seq": 1, "type": "usage", "ts": NOW.isoformat(), "scope": "message", "usage": {"model": "m", "input_tokens": 100}}
    writer.append_batch([record], last_seq=1)
    install_event_store(store)
    try:
        service._count_usage(writer)
        service._count_usage(writer)
    finally:
        install_event_store(None)
        await store.close()
    with sqlite3.connect(store.path) as db:
        rows = db.execute("SELECT run_id, data FROM events WHERE kind='run_usage'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "run"
    assert json.loads(rows[0][1])["usage"]["input_tokens"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["message", "run", None])
async def test_canonical_usage_overrides_display_events_and_deduplicates(tmp_path: Path, scope: str | None) -> None:
    store = EventStore(tmp_path / "events.db", enable_fts=False)
    await store.start()
    records = [
        {"seq": 1, "ts": (NOW - timedelta(hours=1)).isoformat(), "scope": scope,
         "usage": {"model": "m", "input_tokens": 100, "output_tokens": True, "cache_read": -1}},
        {"seq": 2, "ts": NOW.isoformat(), "scope": scope, "usage": {"model": "m", "input_tokens": 200, "output_tokens": 50}},
    ]
    for record in records:
        for _ in range(2):
            store.emit({"ts": record["ts"], "run_id": "run", "symphony": "alpha", "card_id": "card", "source": "daemon",
                        "kind": "run_usage", "data": {**record, "type": "usage"}})
    store.emit({"ts": NOW.isoformat(), "run_id": "run", "symphony": "alpha", "card_id": "card", "source": "performer",
                "kind": "cost", "data": {"usage": {"model": "m", "input_tokens": 9999}}})
    await store.close()
    runs = [{"run": "run", "card": "card", "attributed_symphony": "alpha"}]
    totals, _, reasons = stats_sources.read_stats_usage(store.path, runs, NOW.isoformat())
    ledger = TokenLedger()
    ledger.observe("run", symphony="alpha", card="card", stage="reviewing", records=[{**record, "type": "usage"} for record in records])
    assert totals == ledger.totals_by_run(["run"])
    assert not reasons


@pytest.mark.asyncio
async def test_usage_query_deadline_and_group_bound_are_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EventStore(tmp_path / "events.db", enable_fts=False)
    await store.start()
    store.emit({"ts": NOW.isoformat(), "run_id": "run", "symphony": "alpha", "card_id": "card", "source": "performer",
                "kind": "usage", "data": {"usage": {"model": "m", "input_tokens": 10}}})
    await store.close()
    runs = [{"run": "run", "card": "card", "attributed_symphony": "alpha"}]
    with patch("coordinare.services.stats_usage.MAX_USAGE_GROUPS", 0):
        totals, _, reasons = stats_sources.read_stats_usage(store.path, runs, NOW.isoformat())
    assert not totals
    assert reasons == ["usage_event_group_budget"]
    monkeypatch.setattr("coordinare.services.stats_usage.QUERY_TIMEOUT_SECONDS", 0)
    with pytest.raises(EventQueryTooBroad):
        stats_sources.read_stats_usage(store.path, runs, NOW.isoformat())


@pytest.mark.asyncio
async def test_legacy_observations_stop_at_recorded_terminal_boundary(tmp_path: Path) -> None:
    write_run(tmp_path, "run", "card", persona="reviewer", result=False)
    events = {"alpha": {"card": [
        {"kind": "run_outcome", "stage": "reviewing", "at": (NOW - timedelta(minutes=30)).isoformat(),
         "detail": {"run_id": "run", "outcome": "cancelled"}},
        {"kind": "verdict", "stage": "reviewing", "at": NOW.isoformat(), "detail": {"result": "pass"}},
    ], "unretained": [{"kind": "ci_result", "stage": "reviewer", "at": NOW.isoformat(), "detail": {"result": "BOUNCE"}}]}}
    body = await RunStatistics(tmp_path).query(StatsQuery(), visible=VISIBLE, lifecycle=events, boards={}, now=NOW)
    assert body["totals"]["outcomes"]["cancelled"] == 1
    assert body["coverage"]["unattributed_observations"] == 2
    assert body["totals"]["success_rate"] is None


@pytest.mark.asyncio
async def test_real_monitor_body_keeps_lifecycle_and_store_on_error(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db", enable_fts=False)
    await store.start()
    log = CardLifecycleLog()
    state = initial_state()
    state.update({"phase": "monitoring_performer", "performer_stage": "implementing", "current_symphony": "alpha",
                  "current_card": {"id": "card"}, "agent_dispatch": {"session_id": "run"},
                  "card_lifecycle": log, "event_store": store,
                  "performer_services": {"implementing": SimpleNamespace(check_status=AsyncMock(side_effect=ConnectionError("broken")))}})
    result = await monitor_performer.monitor_performer(state)
    await store.close()
    assert result["card_lifecycle"] is log
    assert result["event_store"] is store
    assert result["phase"] == "system_error"
    with sqlite3.connect(store.path) as db:
        rows = db.execute("SELECT data FROM events WHERE kind='card_lifecycle'").fetchall()
    assert any(json.loads(row[0])["detail"].get("run_id") == "run" for row in rows)
