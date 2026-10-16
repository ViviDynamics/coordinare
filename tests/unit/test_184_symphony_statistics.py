from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.services.event_store import EventStore
from coordinare.services.run_statistics import RunStatistics
from coordinare.services.stats_flow import card_flow
from coordinare.services.stats_observability import live_health, reliability
from coordinare.services.stats_params import StatsQuery
from coordinare.services.stats_poll import poll_board_observed
from tests.unit.dashboard_js_slices import shipped_function
from tests.unit.test_150_run_history import _header, _write
from tests.unit.test_182_run_statistics import NOW, TABLE, VISIBLE, lifecycle, write_run

RECENT_LIMIT = 10
REPO = Path(__file__).resolve().parents[2]


def test_daily_completions_and_recent_cards_use_merge_time_and_scope() -> None:
    records = {(owner, card): events for owner, cards in lifecycle().items() for card, events in cards.items()}
    body = card_flow(records, {}, VISIBLE, NOW - timedelta(days=7), NOW)
    alpha = body["alpha"]
    assert sum(row["cards_completed"] for row in alpha["daily_completed"]) == alpha["cards_completed"]
    assert alpha["recent_completed"][0]["card"] == "card"
    assert alpha["recent_completed"][0]["merged_at"] <= NOW.isoformat()
    assert body["beta"]["recent_completed"] == []


@pytest.mark.asyncio
async def test_recent_runs_are_bounded_windowed_scoped_and_have_transcript_identity(tmp_path: Path) -> None:
    for index in range(RECENT_LIMIT + 2):
        write_run(tmp_path, f"run-{index}", f"card-{index}", hours=index + 1)
    write_run(tmp_path, "outside", "outside", hours=240)
    write_run(tmp_path, "private", "private", symphony="beta")
    body = await RunStatistics(tmp_path, table=TABLE).query(
        StatsQuery(symphony="alpha"), visible=VISIBLE, lifecycle=lifecycle(), boards={}, now=NOW,
    )
    recent = body["recent_runs"]
    assert len(recent) == RECENT_LIMIT
    assert recent[0]["run"] == "run-0"
    assert all(row["symphony"] == "alpha" for row in recent)
    assert all(row["card"] and row["run"] for row in recent)
    flow = body["card_flow"]["alpha"]
    assert flow["cost_per_completed_card_usd"] == body["totals"]["cost"]["usd"] / flow["cards_completed"]


@pytest.mark.asyncio
async def test_recent_conductor_runs_keep_their_transcript_folder(tmp_path: Path) -> None:
    header = _header("decision", "card", stage="conductor")
    header["ts"] = NOW.isoformat()
    header["conductor"] = {"kind": "triage", "symphony": "alpha"}
    _write(tmp_path, "decision", "card", symphony="_conductor", header=header)
    body = await RunStatistics(tmp_path).query(
        StatsQuery(symphony="alpha"), visible=VISIBLE, lifecycle={}, boards={}, now=NOW,
    )
    assert body["recent_runs"][0]["symphony"] == "alpha"
    assert body["recent_runs"][0]["transcript_symphony"] == "_conductor"


@pytest.mark.asyncio
async def test_no_usage_never_produces_precise_cost_per_card(tmp_path: Path) -> None:
    body = await RunStatistics(None).query(
        StatsQuery(symphony="alpha"), visible=VISIBLE, lifecycle=lifecycle(), boards={}, now=NOW,
    )
    assert body["partial"] is True
    assert body["card_flow"]["alpha"]["cost_per_completed_card_usd"] is None


def test_symphony_statistics_node_contract(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    scripts = REPO / "src/coordinare/dashboard/dashboard_static"
    names = ("helpers", "usage", "waiting", "runs", "symphonies", "stats_charts", "symphony_stats")
    bundle = tmp_path / "stats.js"
    bundle.write_text("\n".join((scripts / f"{name}.js").read_text() for name in names))
    result = subprocess.run(
        [node, str(REPO / "tests/js/symphony_stats_184_checks.js"), str(bundle)],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_merged_symphony_detail_preserves_status_actions_and_settings(tmp_path: Path) -> None:
    scripts = REPO / "src/coordinare/dashboard/dashboard_static"
    bundle = tmp_path / "merged-detail.js"
    functions = ("renderSymphoniesPage", "symSummaryHtml", "symOverrideFields", "loadSymphonyPersonas")
    sources = [(scripts / f"{name}.js").read_text() for name in ("helpers", "symphony_stats")]
    sources.extend(shipped_function("app", name) for name in functions)
    sources.append("async " + shipped_function("app", "loadSymphonyDetail"))
    bundle.write_text("\n".join(sources))
    result = subprocess.run(
        ["node", str(REPO / "tests/js/symphony_detail_181_184_checks.js"), str(bundle)],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_board_poll_observations_preserve_results_and_errors() -> None:
    store = MagicMock()
    provider = SimpleNamespace(poll_board=AsyncMock(return_value={"snapshot": {"TODO": ["card"]}}))
    state = {"event_store": store, "current_symphony": "alpha"}
    assert await poll_board_observed(state, provider) == {"snapshot": {"TODO": ["card"]}}
    assert store.emit.call_args.args[0]["data"] == {"success": True}
    provider.poll_board.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        await poll_board_observed(state, provider)
    assert store.emit.call_args.args[0]["data"] == {"success": False}
    assert store.emit.call_args.args[0]["symphony"] == "alpha"


@pytest.mark.asyncio
async def test_cancelled_board_polls_do_not_invent_a_failure_observation() -> None:
    store = MagicMock()
    provider = SimpleNamespace(poll_board=AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await poll_board_observed({"event_store": store}, provider)
    store.emit.assert_not_called()


def test_reliability_uses_actual_windowed_polls_without_inventing_legacy_successes() -> None:
    events = [
        {"symphony": owner, "kind": "stats.board_check", "ts": (NOW - timedelta(hours=hours)).isoformat(),
         "data": {"success": success}, "message": "quota"}
        for owner, hours, success in (("alpha", 2, True), ("alpha", 1, False),
                                     ("alpha", 200, True), ("beta", 1, True))
    ]
    result = reliability(events, frozenset({"alpha", "legacy"}), NOW - timedelta(days=7), NOW)
    assert result["alpha"]["checks"] == 2
    assert result["alpha"]["success_rate"] == 0.5
    assert result["alpha"]["recent_errors"] == [{"at": (NOW - timedelta(hours=1)).isoformat(), "reason": "quota"}]
    assert result["legacy"] == {"checks": 0, "success_rate": None, "recent_errors": []}


def test_live_capacity_reuses_scheduler_rules_and_deduplicates_queued_cards() -> None:
    state = {
        "board_snapshot": {"TODO": ["queue", "new"], "IN_PROGRESS": ["run"], "IN_REVIEW": ["review"]},
        "todo_waits": {"queue": {"reason": "queued"}, "new": {"reason": "queued"}},
        "active_sessions": {
            "run": {"phase": "monitoring_performer", "agent_dispatch": {"session_id": "live"}},
            "queue": {"phase": "idle", "slot_queued_since": NOW.isoformat()},
            "review": {"phase": "monitoring_pr", "review_wait": "human", "current_card": {"status": "IN_REVIEW"}},
        },
    }
    health = live_health({"alpha": {"state": state}, "private": {}}, frozenset({"alpha"}))
    assert set(health) == {"alpha"}
    assert health["alpha"]["cards_active"] == 1
    assert health["alpha"]["running_card_ids"] == ["run"]
    assert health["alpha"]["cards_queued"] == 2
    assert health["alpha"]["cards_parked"] == 1
    assert health["alpha"]["slots_in_use"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["24h", "7d", "30d"])
async def test_list_and_detail_share_measures_and_board_columns(tmp_path: Path, window: str) -> None:
    write_run(tmp_path, "one", "card")
    write_run(tmp_path, "old", "old", hours=240)
    service = RunStatistics(tmp_path, table=TABLE)
    board = {"alpha": {"TODO": ["a", "b"], "IN_REVIEW": ["card"], "DONE": []}}
    arguments = {"visible": VISIBLE, "lifecycle": lifecycle(), "boards": board, "now": NOW}
    listing = await service.query(StatsQuery(group_by="symphony", window=window), **arguments)
    detail = await service.query(StatsQuery(group_by="stage", symphony="alpha", window=window), **arguments)
    alpha = next(row for row in listing["groups"] if row["key"] == "alpha")
    for field in ("tokens", "cost", "runs"):
        assert alpha[field] == detail["totals"][field]
    assert listing["card_flow"]["alpha"] == detail["card_flow"]["alpha"]
    assert detail["card_flow"]["alpha"]["columns_now"] == {key: len(cards) for key, cards in board["alpha"].items()}


@pytest.mark.asyncio
async def test_reliability_survives_restart_in_existing_event_store(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db", enable_fts=False)
    await store.start()
    state = {"event_store": store, "current_symphony": "alpha"}
    provider = SimpleNamespace(poll_board=AsyncMock(return_value={"snapshot": {}}))
    await poll_board_observed(state, provider)
    provider.poll_board.side_effect = RuntimeError("quota exhausted")
    with pytest.raises(RuntimeError, match="quota"):
        await poll_board_observed(state, provider)
    await store.close()
    body = await RunStatistics(None, event_path=store.path).query(
        StatsQuery(symphony="alpha"), visible=VISIBLE, lifecycle={}, boards={},
    )
    assert body["reliability"]["alpha"]["checks"] == 2
    assert body["reliability"]["alpha"]["success_rate"] == 0.5
    assert body["reliability"]["alpha"]["recent_errors"][0]["reason"] == "quota exhausted"
    assert body["reliability"]["alpha"]["recent_errors"][0]["at"]


def test_api_health_is_scoped_and_redacted() -> None:
    secret = "ghp_" + "a" * 36
    daemon = MagicMock()
    daemon.state = {
        "symphony_configs": dict.fromkeys(VISIBLE),
        "symphony_states": {"alpha": SimpleNamespace(
            board_snapshot={"TODO": ["card"]}, last_error=secret, active_sessions={},
        )},
    }
    api = TestClient(
        create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock()),
        base_url="http://127.0.0.1:8090",
    )
    response = api.get("/api/stats?symphony=alpha")
    assert response.status_code == 200
    assert set(response.json()["symphony_health"]) == {"alpha"}
    assert response.json()["card_flow"]["alpha"]["columns_now"] == {"TODO": 1}
    assert secret not in response.text
