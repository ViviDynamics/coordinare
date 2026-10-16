from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from coordinare.cancel import cancel_active_card
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.graph.nodes import monitor_performer
from coordinare.graph.nodes.monitor.body import _BodyCtx, _phase_verdict_record
from coordinare.graph.nodes.monitor_pr import _record_review_wait
from coordinare.mcp.tools import find_tool
from coordinare.mcp.tools.stats_tools import query_stats_handler
from coordinare.services import stats_sources
from coordinare.services.card_lifecycle import CardLifecycleLog, record_lifecycle
from coordinare.services.event_store import EventStore
from coordinare.services.event_store_query import EventQueryTooBroad
from coordinare.services.run_statistics import RunStatistics
from coordinare.services.run_transcript import TranscriptCollector, transcript_path
from coordinare.services.stats_flow import card_flow, percentiles
from coordinare.services.stats_params import StatsQuery
from coordinare.services.token_usage import ModelPrice, PriceTable
from coordinare.state_store import WorkflowSnapshot

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
VISIBLE = frozenset({"alpha", "beta"})
TABLE = PriceTable(rules={"m": ModelPrice(1, 2, 3, 4)})


def write_run(
    root: Path,
    run: str,
    card: str,
    *,
    hours: int = 1,
    duration: int = 60,
    persona: str = "implementer",
    symphony: str = "alpha",
    error: bool = False,
    model: str = "m",
    result: bool = True,
) -> None:
    started = NOW - timedelta(hours=hours)
    records: list[dict[str, Any]] = [
        {
            "type": "run_header",
            "ts": started.isoformat(),
            "run_id": run,
            "persona": {"name": persona},
            "performer": {"backend": "codex"},
            "model": {"model": model},
            "card": {"id": card},
        },
        {
            "type": "usage",
            "seq": 1,
            "ts": started.isoformat(),
            "scope": "message",
            "usage": {
                "model": model,
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read": 10,
                "cache_write": 5,
            },
        },
        {
            "type": "usage",
            "seq": 2,
            "ts": started.isoformat(),
            "scope": "run",
            "usage": {"model": model, "input_tokens": 1000},
        },
    ]
    if result:
        records.append(
            {
                "type": "result",
                "ts": (started + timedelta(seconds=duration)).isoformat(),
                "is_error": error,
                "text": "worker failed" if error else "done",
            },
        )
    path = transcript_path(root, symphony=symphony, card_id=card, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def lifecycle() -> dict[str, Any]:
    began = NOW - timedelta(hours=5)
    return {
        "alpha": {
            "card": [
                {"kind": "dispatched", "stage": "implementing", "at": began.isoformat()},
                {
                    "kind": "verdict",
                    "stage": "implementing",
                    "at": (began + timedelta(minutes=2)).isoformat(),
                    "detail": {"result": "fail", "marker": "changes_requested"},
                },
                {
                    "kind": "pr_opened",
                    "at": (began + timedelta(minutes=3)).isoformat(),
                    "detail": {"pr_number": 12},
                },
                {"kind": "human_review_started", "at": (NOW - timedelta(hours=2)).isoformat()},
                {
                    "kind": "review_received",
                    "at": (NOW - timedelta(hours=1)).isoformat(),
                    "detail": {"reviewer": "human", "review_state": "CHANGES_REQUESTED"},
                },
                {"kind": "merged", "at": (NOW - timedelta(minutes=30)).isoformat()},
            ],
        },
    }


async def query(service: RunStatistics, **kwargs: Any) -> dict[str, Any]:
    return await service.query(
        StatsQuery(**kwargs),
        visible=VISIBLE,
        lifecycle=lifecycle(),
        boards={"alpha": {"DONE": ["card"], "TODO": ["other"]}},
        now=NOW,
    )


@pytest.mark.asyncio
async def test_fixed_records_define_all_run_measures(tmp_path: Path) -> None:
    write_run(tmp_path, "first", "card", hours=5, duration=60, persona="reviewer")
    write_run(tmp_path, "retry", "card", hours=3, duration=120, persona="reviewer")
    write_run(tmp_path, "single", "other", duration=180, persona="reviewer")
    write_run(tmp_path, "failure", "broken", error=True, duration=240, persona="reviewer")
    events = {"alpha": {card: [
        {"kind": "verdict", "stage": "reviewing", "at": (NOW - timedelta(hours=hours)).isoformat(),
         "detail": {"run_id": run, "result": result, "marker": "changes_requested" if result == "fail" else "approved"}}
        for run, result, hours in verdicts
    ] for card, verdicts in {"card": [("first", "fail", 5), ("retry", "pass", 3)], "other": [("single", "pass", 1)]}.items()}}
    body = await RunStatistics(tmp_path).query(StatsQuery(group_by="agent"), visible=VISIBLE, lifecycle=events, boards={}, now=NOW)
    group = body["groups"][0]
    assert group["key"] == "reviewer"
    assert group["runs"] == 4
    assert group["outcomes"] == {
        "success": 2,
        "failure": 1,
        "completed_without_verdict": 0,
        "system_error": 1,
        "blocked": 0,
        "cancelled": 0,
        "unknown": 0,
    }
    assert group["success_rate"] == pytest.approx(2 / 3)
    assert group["verdict_runs"] == 3
    assert group["first_pass_rate"] == 0.5
    assert group["resolved_card_stages"] == 2
    assert group["runs_per_card"] == pytest.approx(4 / 3)
    assert group["retries"] == 1
    assert group["bounces"] == 1
    assert group["duration_seconds"] == {"median": 150, "p90": 222, "samples": 4}
    assert group["last_run_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert {row["reason"] for row in group["failure_reasons"]} == {
        "changes_requested",
        "worker failed",
    }
    assert sum(row["runs"] for row in body["daily"]) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("window,expected", [("24h", 1), ("7d", 2), ("30d", 3), ("all", 4)])
async def test_windows_use_run_start_and_daily_series(
    tmp_path: Path, window: str, expected: int,
) -> None:
    for hours in (1, 48, 240, 960):
        write_run(tmp_path, str(hours), str(hours), hours=hours)
    body = await query(RunStatistics(tmp_path), window=window)
    assert body["totals"]["runs"] == expected
    assert sum(row["runs"] for row in body["daily"]) == expected


@pytest.mark.asyncio
async def test_usage_reconciles_across_dimensions_and_survives_restart(tmp_path: Path) -> None:
    write_run(tmp_path, "one", "a", persona="implementer", hours=240)
    write_run(tmp_path, "two", "b", persona="reviewer", hours=1)
    service = RunStatistics(tmp_path, table=TABLE)
    agents = await query(service, group_by="agent", window="30d")
    symphony = await query(service, group_by="symphony", window="30d")
    total = symphony["groups"][0]
    assert total["tokens"] == {
        "input": 200,
        "output": 100,
        "cache_read": 20,
        "cache_write": 10,
        "total": 330,
    }
    assert total["cost"]["usd"] == pytest.approx(0.0005)
    assert sum(row["cost"]["usd"] for row in agents["groups"]) == total["cost"]["usd"]
    for name, count in total["tokens"].items():
        assert sum(row["tokens"][name] for row in agents["groups"]) == count
    restarted = await query(RunStatistics(tmp_path, table=TABLE), group_by="agent", window="30d")
    assert restarted == agents


@pytest.mark.asyncio
async def test_snapshot_roundtrip_preserves_card_flow(tmp_path: Path) -> None:
    original = await query(RunStatistics(tmp_path))
    snapshot = WorkflowSnapshot(snapshot_at=NOW, phase="idle", card_lifecycle=lifecycle())
    restored = WorkflowSnapshot.model_validate_json(snapshot.model_dump_json())
    log = CardLifecycleLog()
    log.load(restored.card_lifecycle)
    restarted = await RunStatistics(tmp_path).query(
        StatsQuery(), visible=VISIBLE, lifecycle=log.to_snapshot(),
        boards={"alpha": {"DONE": ["card"], "TODO": ["other"]}}, now=NOW,
    )
    assert restarted["card_flow"] == original["card_flow"]


@pytest.mark.asyncio
async def test_card_flow_uses_first_dispatch_to_merge_and_human_wait(tmp_path: Path) -> None:
    body = await query(RunStatistics(tmp_path), group_by="symphony")
    flow = body["card_flow"]["alpha"]
    assert flow["cards_completed"] == flow["prs_merged"] == 1
    assert flow["columns_now"] == {"DONE": 1, "TODO": 1}
    assert flow["cycle_time_seconds"] == {"median": 16200, "p90": 16200, "samples": 1}
    assert flow["human_review_wait_seconds"] == 3600
    assert flow["revision_rounds_per_pr"] == 1


@pytest.mark.asyncio
async def test_unknown_usage_and_unfinished_runs_are_not_success(tmp_path: Path) -> None:
    write_run(tmp_path, "unfinished", "a", result=False, model="unpriced")
    group = (await query(RunStatistics(tmp_path), group_by="backend"))["groups"][0]
    assert group["outcomes"]["unknown"] == 1
    assert group["success_rate"] is None
    assert group["first_pass_rate"] is None
    assert group["cost"]["usd"] is None
    assert group["tokens"]["total"] == 165


@pytest.mark.asyncio
async def test_source_cache_is_shared_across_queries_and_cold_requests(tmp_path: Path) -> None:
    write_run(tmp_path, "one", "a")
    service = RunStatistics(tmp_path)
    with patch.object(stats_sources, "load_sources", wraps=stats_sources.load_sources) as load:
        await asyncio.gather(
            query(service, group_by="agent"),
            query(service, group_by="stage", window="30d"),
            query(service, group_by="symphony", symphony="alpha"),
        )
        assert load.call_count == 1
        service._expires = 0
        await query(service)
        assert load.call_count == 2


@pytest.mark.asyncio
async def test_read_bounds_are_explicit_and_do_not_claim_complete_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_run(tmp_path, "one", "a")
    write_run(tmp_path, "two", "b")
    monkeypatch.setattr(stats_sources, "MAX_RUNS", 1)
    body = await query(RunStatistics(tmp_path))
    assert body["partial"]
    assert "run_limit" in body["partial_reasons"]
    assert body["totals"]["runs"] == 1


@pytest.mark.asyncio
async def test_lifecycle_events_are_durable_indexed_and_deduplicated(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db", enable_fts=False)
    await store.start()
    log = CardLifecycleLog()
    state = {"event_store": store, "card_lifecycle": log, "current_symphony": "alpha"}
    for event in lifecycle()["alpha"]["card"]:
        fields = {key: value for key, value in event.items() if key != "kind"}
        assert record_lifecycle(state, event["kind"], "card", **fields)
    await store.close()
    with sqlite3.connect(store.path) as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM events WHERE kind=? AND ts<=? ORDER BY ts LIMIT ?",
            ("card_lifecycle", NOW.isoformat(), 100),
        ).fetchall()
        assert "events_kind_ts" in str(plan)
    cold = await query(RunStatistics(None, event_path=store.path))
    assert cold["card_flow"]["alpha"]["cards_completed"] == 1
    assert cold["card_flow"]["alpha"]["revision_rounds_per_pr"] == 1


def client(root: Path, *, auth: bool = False) -> TestClient:
    daemon = MagicMock()
    daemon.state = {
        "symphony_configs": dict.fromkeys(VISIBLE),
        "config": None,
        "transcript_collector": TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6),
        "card_lifecycle": CardLifecycleLog(),
        "symphony_states": {},
    }
    app = create_dashboard_app(
        DashboardStore(),
        daemon,
        MagicMock(),
        MagicMock(),
        auth_token=SecretStr("secret") if auth else None,
    )
    return TestClient(app, base_url="http://127.0.0.1:8090")


def test_api_scope_validation_redaction_and_auth(tmp_path: Path) -> None:
    write_run(tmp_path, "one", "a")
    write_run(tmp_path, "hidden", "b", symphony="hidden")
    api = client(tmp_path)
    response = api.get("/api/stats?window=all&group_by=stage")
    assert response.status_code == 200
    assert response.json()["totals"]["runs"] == 1
    assert response.json()["groups"][0]["key"] == "implementing"
    assert api.get("/api/stats?symphony=hidden").status_code == 404
    assert api.get("/api/stats?window=forever").status_code == 422
    assert api.get("/api/stats?top_n=0").status_code == 422
    assert api.get("/api/stats?group_by=oops").status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert client(tmp_path, auth=True).get("/api/stats").status_code == 401


@pytest.mark.asyncio
async def test_mcp_uses_same_validated_query_and_rest_read_budget() -> None:
    tool = find_tool("query_stats")
    assert tool is not None and tool.annotations.read_only_hint
    dashboard = SimpleNamespace(get_json=AsyncMock(), read_budget=4)
    await query_stats_handler(StatsQuery(group_by="agent", symphony="a/b", window="30d"), dashboard)
    args, kwargs = dashboard.get_json.call_args
    assert args[0].startswith("/api/stats?")
    assert "symphony=a%2Fb" in args[0]
    assert kwargs == {"timeout_seconds": 4}
    with pytest.raises(ValidationError):
        StatsQuery(group_by="invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["agent", "stage", "symphony", "model", "backend", "outcome"])
async def test_each_dimension_partitions_totals_and_daily_series(
    tmp_path: Path, dimension: str,
) -> None:
    write_run(tmp_path, "a", "a")
    write_run(tmp_path, "b", "b", persona="reviewer", symphony="beta")
    body = await query(RunStatistics(tmp_path, table=TABLE), group_by=dimension)
    assert sum(group["runs"] for group in body["groups"]) == body["totals"]["runs"] == 2
    assert sum(group["tokens"]["total"] for group in body["groups"]) == 330
    assert sum(group["cost"]["usd"] for group in body["groups"]) == body["totals"]["cost"]["usd"]
    assert sum(group["runs"] for day in body["daily"] for group in day["groups"]) == 2


@pytest.mark.asyncio
async def test_terminal_lifecycle_outcomes_and_top_reasons(tmp_path: Path) -> None:
    records = {}
    for name in ("blocked", "cancelled", "system_error"):
        write_run(tmp_path, name, name, result=False)
        records[name] = [
            {
                "kind": "run_outcome",
                "stage": "implementing",
                "at": NOW.isoformat(),
                "detail": {"outcome": name, "run_id": name, "reason": "reason " + name},
            },
        ]
    body = await RunStatistics(tmp_path).query(
        StatsQuery(group_by="outcome", top_n=1),
        visible=VISIBLE,
        lifecycle={"alpha": records},
        boards={},
        now=NOW,
    )
    assert {group["key"] for group in body["groups"]} == {"blocked", "cancelled", "system_error"}
    assert len(body["totals"]["failure_reasons"]) == 1
    assert body["totals"]["first_pass_rate"] is None


@pytest.mark.asyncio
async def test_prior_window_attempt_prevents_first_pass(tmp_path: Path) -> None:
    write_run(tmp_path, "old", "a", hours=48, persona="reviewer")
    write_run(tmp_path, "new", "a", hours=1, persona="reviewer")
    events = {"alpha": {"a": [{"kind": "verdict", "stage": "reviewing", "at": NOW.isoformat(),
                               "detail": {"run_id": "new", "result": "pass"}}]}}
    body = await RunStatistics(tmp_path).query(StatsQuery(window="24h"), visible=VISIBLE, lifecycle=events, boards={}, now=NOW)
    assert body["totals"]["runs"] == body["totals"]["retries"] == 1
    assert body["totals"]["first_pass_rate"] == 0


@pytest.mark.asyncio
async def test_usage_budget_never_returns_partial_run_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_run(tmp_path, "a", "a")
    monkeypatch.setattr(stats_sources, "MAX_USAGE_BYTES", 1)
    body = await query(RunStatistics(tmp_path))
    assert body["partial"]
    assert body["totals"]["missing_usage_runs"] == 1
    assert body["totals"]["cost"]["usd"] is None


@pytest.mark.asyncio
async def test_event_read_budget_and_row_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EventStore(tmp_path / "events.db", enable_fts=False)
    await store.start()
    for index in range(3):
        store.emit(
            {"ts": NOW.isoformat(), "kind": "blocked", "symphony": "alpha", "run_id": str(index)},
        )
    await store.close()
    monkeypatch.setattr(stats_sources, "MAX_EVENT_ROWS", 1)
    events, reasons = stats_sources.read_stats_events(store.path, VISIBLE, NOW.isoformat())
    assert len(events) == 1
    assert reasons == ["event_read_budget"]
    monkeypatch.setattr(stats_sources, "QUERY_TIMEOUT_SECONDS", 0)
    with pytest.raises(EventQueryTooBroad):
        stats_sources.read_stats_events(store.path, VISIBLE, NOW.isoformat())


def test_api_redacts_free_text_and_reports_source_failures(tmp_path: Path) -> None:
    write_run(tmp_path, "one", "a", error=True)
    path = transcript_path(tmp_path, symphony="alpha", card_id="a", run_id="one")
    token = "ghp_" + "a" * 36
    path.write_text(path.read_text().replace("worker failed", token))
    api = client(tmp_path)
    assert token not in api.get("/api/stats?window=all").text
    with patch.object(stats_sources, "load_sources", side_effect=EventQueryTooBroad):
        assert client(tmp_path).get("/api/stats").status_code == 422
    with patch.object(stats_sources, "load_sources", side_effect=sqlite3.OperationalError):
        assert client(tmp_path).get("/api/stats").status_code == 503


@pytest.mark.asyncio
async def test_monitor_records_terminal_phase_with_original_run_identity() -> None:
    log = CardLifecycleLog()
    state = {"phase": "monitoring_performer", "performer_stage": "implementing",
             "agent_dispatch": {"session_id": "run"}, "current_card": {"id": "card"},
             "current_symphony": "alpha", "card_lifecycle": log}
    result = {**state, "phase": "blocked", "agent_dispatch": {}, "system_error_reason": "limit reached"}
    with patch.object(monitor_performer, "_monitor_performer_body", AsyncMock(return_value=result)):
        await monitor_performer.monitor_performer(state)
    assert log.events("alpha", "card")[0]["detail"] == {
        "outcome": "blocked", "reason": "limit reached", "run_id": "run"}


@pytest.mark.asyncio
async def test_same_head_verdicts_are_deduplicated_per_run() -> None:
    log = CardLifecycleLog()
    state = {"current_symphony": "alpha", "card_lifecycle": log}
    ctx = _BodyCtx(stage="reviewing", marker="changes_requested", card_id="card",
                   status={"head_after": "a" * 40})
    for run in ("a" * 35 + "b", "a" * 35 + "c"):
        state["agent_dispatch"] = {"session_id": run}
        await _phase_verdict_record(state, ctx)
        await _phase_verdict_record(state, ctx)
    assert len(log.events("alpha", "card")) == 2


def test_human_wait_boundaries_reuse_existing_wait_classification() -> None:
    log = CardLifecycleLog()
    state = {"current_symphony": "alpha", "card_lifecycle": log}
    for wait in ("automation", "human", "human", "automation", "human", None):
        _record_review_wait(state, "card", wait, reason="test", head="head")
    assert [event["kind"] for event in log.events("alpha", "card")] == [
        "human_review_started", "human_review_ended", "human_review_started", "human_review_ended"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [True, False])
async def test_cancellation_is_recorded_only_after_confirmed_stop(stopped: bool) -> None:
    log = CardLifecycleLog()
    agent = SimpleNamespace(relay_feedback=AsyncMock(side_effect=None if stopped else TimeoutError))
    state = {"phase": "monitoring_performer", "performer_stage": "implementing", "active_sessions": {},
             "agent_dispatch": {"session_id": "run"}, "current_card": {"id": "card"},
             "current_symphony": "alpha", "card_lifecycle": log, "agent_service": agent}
    await cancel_active_card(state, move_to_todo=False)
    outcomes = [event for event in log.events("alpha", "card") if event["kind"] == "run_outcome"]
    assert bool(outcomes) is stopped
    if stopped:
        assert outcomes[0]["detail"]["outcome"] == "cancelled"


def test_card_flow_keeps_dispatch_context_before_window_and_clips_waits() -> None:
    start = NOW - timedelta(hours=24)
    records = {("alpha", "one"): [
        {"kind": "dispatched", "at": (NOW - timedelta(hours=48)).isoformat()},
        {"kind": "human_review_started", "at": (NOW - timedelta(hours=30)).isoformat()},
        {"kind": "merged", "at": (NOW - timedelta(hours=1)).isoformat()},
    ], ("alpha", "unknown"): [{"kind": "merged", "at": NOW.isoformat()}]}
    flow = card_flow(records, {}, VISIBLE, start, NOW)["alpha"]
    assert flow["cards_completed"] == 2
    assert flow["cycle_time_seconds"] == {"median": 169200, "p90": 169200, "samples": 1}
    assert flow["human_review_wait_seconds"] == 82800
    assert flow["columns_now"] is None
    assert percentiles([-1]) == {"median": None, "p90": None, "samples": 0}


@pytest.mark.asyncio
async def test_ci_bounces_and_dimension_filters_share_the_same_cohort(tmp_path: Path) -> None:
    write_run(tmp_path, "a", "a")
    write_run(tmp_path, "b", "b", persona="reviewer")
    events = {"alpha": {"a": [
        {"kind": "ci_result", "at": NOW.isoformat(), "stage": "implementing", "detail": {"result": "BOUNCE", "run_id": "a"}},
        {"kind": "ci_result", "at": NOW.isoformat(), "stage": "implementing", "detail": {"result": "BOUNCE", "run_id": "a"}},
    ]}}
    body = await RunStatistics(tmp_path).query(
        StatsQuery(agent="implementer", stage="implementing", backend="codex", model="m", outcome="completed_without_verdict"),
        visible=VISIBLE, lifecycle=events, boards={}, now=NOW,
    )
    assert body["totals"]["runs"] == body["totals"]["bounces"] == 1
    assert body["totals"]["outcomes"]["completed_without_verdict"] == 1


@pytest.mark.asyncio
async def test_discovery_deadline_and_symlink_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_run(tmp_path, "outside", "outside", symphony="hidden")
    (tmp_path / "alpha").symlink_to(tmp_path / "hidden", target_is_directory=True)
    assert (await query(RunStatistics(tmp_path)))["totals"]["runs"] == 0
    monkeypatch.setattr(stats_sources, "SOURCE_TIMEOUT_SECONDS", 0)
    body = await query(RunStatistics(tmp_path))
    assert body["partial"] and "history_time_budget" in body["partial_reasons"]


@pytest.mark.asyncio
async def test_symphony_filter_scopes_coverage_as_well_as_groups(tmp_path: Path) -> None:
    write_run(tmp_path, "alpha-run", "a", hours=1)
    write_run(tmp_path, "beta-run", "b", hours=240, symphony="beta")
    body = await query(RunStatistics(tmp_path), window="all", symphony="alpha")
    assert body["coverage"]["retained_runs_read"] == 1
    assert body["coverage"]["usage_runs_read"] == 1
    assert body["coverage"]["oldest_run_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert set(body["card_flow"]) == {"alpha"}


def test_revision_denominator_includes_pr_opened_before_window() -> None:
    records = {("alpha", "one"): [
        {"kind": "pr_opened", "at": (NOW - timedelta(hours=48)).isoformat(), "detail": {"pr_number": 1}},
        {"kind": "review_received", "at": (NOW - timedelta(hours=1)).isoformat(),
         "detail": {"review_state": "CHANGES_REQUESTED"}},
    ], ("alpha", "inactive"): [
        {"kind": "pr_opened", "at": (NOW - timedelta(hours=48)).isoformat(), "detail": {"pr_number": 2}},
    ]}
    flow = card_flow(records, {}, VISIBLE, NOW - timedelta(hours=24), NOW)["alpha"]
    assert flow["revision_rounds_per_pr"] == 1


@pytest.mark.asyncio
async def test_exact_costs_reconcile_below_display_precision(tmp_path: Path) -> None:
    write_run(tmp_path, "a", "a", persona="implementer")
    write_run(tmp_path, "b", "b", persona="reviewer")
    table = PriceTable(rules={"m": ModelPrice(0.001, 0.001, 0.001, 0.001)})
    body = await query(RunStatistics(tmp_path, table=table))
    exact = sum(Decimal(group["cost"]["usd_exact"]) for group in body["groups"])
    assert exact == Decimal(body["totals"]["cost"]["usd_exact"]) == Decimal("0.00000033")
