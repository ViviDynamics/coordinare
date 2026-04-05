"""Tests for 035: Multi-card parallelism — check_board multi-card pickup,
daemon multi-session iteration, config validation, metrics, and dashboard.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.graph.state import initial_state
from coordinare.session import create_session_from_card

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(max_concurrent_cards: int = 1, **overrides: Any) -> MagicMock:
    """Return a mock ProjectConfiguration with the specified concurrency limit."""
    config = MagicMock()
    config.max_concurrent_cards = max_concurrent_cards
    config.priority = MagicMock()
    config.priority.field_name = None
    for k, v in overrides.items():
        setattr(config, k, v)
    return config


def _make_board(todo_ids: list[str], **extra_columns: list[str]) -> dict:
    """Return a fake board dict suitable for github_service.poll_board()."""
    snapshot: dict[str, list[str]] = {"TODO": todo_ids}
    for col, ids in extra_columns.items():
        snapshot[col] = ids
    titles = {cid: f"Card {cid}" for cid in todo_ids}
    descriptions = {cid: f"Desc for {cid}" for cid in todo_ids}
    issue_numbers = {cid: i + 1 for i, cid in enumerate(todo_ids)}
    issue_urls = {cid: f"https://github.com/issues/{i + 1}" for i, cid in enumerate(todo_ids)}
    content_node_ids = {cid: f"node_{cid}" for cid in todo_ids}
    return {
        "snapshot": snapshot,
        "titles": titles,
        "descriptions": descriptions,
        "issue_numbers": issue_numbers,
        "issue_urls": issue_urls,
        "content_node_ids": content_node_ids,
        "item_labels": {},
        "item_field_values": {},
    }


def _state_with_config(max_concurrent_cards: int = 1) -> dict:
    state = initial_state()
    state["config"] = _make_config(max_concurrent_cards)
    return state


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_max_concurrent_cards_default(tmp_path: Any) -> None:
    from coordinare.config import ProjectConfiguration

    path = tmp_path / "config.yaml"
    path.write_text(
        'project_name: "Demo"\n'
        'github_org: "acme"\n'
        "github_project_number: 12\n"
        'github_token: "ghp_test"\n'
        'human_reviewers: ["alice"]\n'
    )
    config = ProjectConfiguration.from_yaml(path)
    assert config.max_concurrent_cards == 1


def test_config_max_concurrent_cards_custom(tmp_path: Any) -> None:
    from coordinare.config import ProjectConfiguration

    path = tmp_path / "config.yaml"
    path.write_text(
        'project_name: "Demo"\n'
        'github_org: "acme"\n'
        "github_project_number: 12\n"
        'github_token: "ghp_test"\n'
        'human_reviewers: ["alice"]\n'
        "max_concurrent_cards: 5\n"
    )
    config = ProjectConfiguration.from_yaml(path)
    assert config.max_concurrent_cards == 5


def test_config_max_concurrent_cards_min_boundary(tmp_path: Any) -> None:
    from pydantic import ValidationError

    from coordinare.config import ProjectConfiguration

    path = tmp_path / "config.yaml"
    path.write_text(
        'project_name: "Demo"\n'
        'github_org: "acme"\n'
        "github_project_number: 12\n"
        'github_token: "ghp_test"\n'
        'human_reviewers: ["alice"]\n'
        "max_concurrent_cards: 0\n"
    )
    with pytest.raises(ValidationError):
        ProjectConfiguration.from_yaml(path)


def test_config_max_concurrent_cards_max_boundary(tmp_path: Any) -> None:
    from pydantic import ValidationError

    from coordinare.config import ProjectConfiguration

    path = tmp_path / "config.yaml"
    path.write_text(
        'project_name: "Demo"\n'
        'github_org: "acme"\n'
        "github_project_number: 12\n"
        'github_token: "ghp_test"\n'
        'human_reviewers: ["alice"]\n'
        "max_concurrent_cards: 21\n"
    )
    with pytest.raises(ValidationError):
        ProjectConfiguration.from_yaml(path)


def test_config_max_concurrent_cards_20_allowed(tmp_path: Any) -> None:
    from coordinare.config import ProjectConfiguration

    path = tmp_path / "config.yaml"
    path.write_text(
        'project_name: "Demo"\n'
        'github_org: "acme"\n'
        "github_project_number: 12\n"
        'github_token: "ghp_test"\n'
        'human_reviewers: ["alice"]\n'
        "max_concurrent_cards: 20\n"
    )
    config = ProjectConfiguration.from_yaml(path)
    assert config.max_concurrent_cards == 20


# ---------------------------------------------------------------------------
# check_board — single card mode (backward compatibility)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_board_single_card_mode_unchanged() -> None:
    """With max_concurrent_cards=1, check_board picks exactly 1 card (existing behavior)."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    github.poll_board.return_value = _make_board(["PVI_1", "PVI_2", "PVI_3"])

    state = _state_with_config(max_concurrent_cards=1)
    state["github_service"] = github

    result = await check_board(state)

    assert result["current_card"]["id"] == "PVI_1"
    assert result["phase"] == "dispatching"
    # active_sessions should still be empty in single-card mode
    assert result.get("active_sessions", {}) == {}


@pytest.mark.asyncio
async def test_check_board_no_config_defaults_to_single() -> None:
    """Without config, check_board picks exactly 1 card."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    github.poll_board.return_value = _make_board(["PVI_1", "PVI_2"])

    state = initial_state()
    state["github_service"] = github
    # No config at all

    result = await check_board(state)

    assert result["current_card"]["id"] == "PVI_1"
    assert result["phase"] == "dispatching"


# ---------------------------------------------------------------------------
# check_board — multi-card mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_board_multi_card_picks_up_to_limit() -> None:
    """With max_concurrent_cards=2, check_board picks 2 cards."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    github.poll_board.return_value = _make_board(["PVI_1", "PVI_2", "PVI_3"])

    state = _state_with_config(max_concurrent_cards=2)
    state["github_service"] = github

    result = await check_board(state)

    sessions = result.get("active_sessions", {})
    assert len(sessions) == 2
    assert "PVI_1" in sessions
    assert "PVI_2" in sessions
    assert "PVI_3" not in sessions
    # Flat state should reflect first session's card
    assert result["current_card"]["id"] == "PVI_1"
    assert result["phase"] == "dispatching"


@pytest.mark.asyncio
async def test_check_board_multi_card_under_capacity() -> None:
    """With max_concurrent_cards=3 and 1 card, picks 1 (no error)."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    github.poll_board.return_value = _make_board(["PVI_1"])

    state = _state_with_config(max_concurrent_cards=3)
    state["github_service"] = github

    result = await check_board(state)

    sessions = result.get("active_sessions", {})
    assert len(sessions) == 1
    assert "PVI_1" in sessions


@pytest.mark.asyncio
async def test_check_board_multi_card_skips_already_active() -> None:
    """Cards already in active_sessions are not picked up again."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    github.poll_board.return_value = _make_board(["PVI_1", "PVI_2", "PVI_3"])

    state = _state_with_config(max_concurrent_cards=3)
    state["github_service"] = github
    # PVI_1 is already active
    state["active_sessions"] = {
        "PVI_1": create_session_from_card({"id": "PVI_1", "title": "Existing"}),
    }

    result = await check_board(state)

    sessions = result.get("active_sessions", {})
    assert len(sessions) == 3
    assert "PVI_1" in sessions
    assert "PVI_2" in sessions
    assert "PVI_3" in sessions


@pytest.mark.asyncio
async def test_check_board_multi_card_at_capacity() -> None:
    """When already at capacity, no new cards are picked up."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    github.poll_board.return_value = _make_board(["PVI_3"])

    state = _state_with_config(max_concurrent_cards=2)
    state["github_service"] = github
    state["active_sessions"] = {
        "PVI_1": create_session_from_card({"id": "PVI_1", "title": "A"}),
        "PVI_2": create_session_from_card({"id": "PVI_2", "title": "B"}),
    }

    result = await check_board(state)

    sessions = result.get("active_sessions", {})
    assert len(sessions) == 2
    assert "PVI_3" not in sessions


@pytest.mark.asyncio
async def test_check_board_multi_card_deduplication() -> None:
    """Duplicate card IDs in TODO are not double-picked."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    board = _make_board(["PVI_1", "PVI_1", "PVI_2"])
    github.poll_board.return_value = board

    state = _state_with_config(max_concurrent_cards=3)
    state["github_service"] = github

    result = await check_board(state)

    sessions = result.get("active_sessions", {})
    # PVI_1 should appear only once
    assert len(sessions) == 2
    assert "PVI_1" in sessions
    assert "PVI_2" in sessions


@pytest.mark.asyncio
async def test_check_board_multi_card_empty_todo() -> None:
    """With no TODO cards, phase is idle."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    github.poll_board.return_value = _make_board([])

    state = _state_with_config(max_concurrent_cards=3)
    state["github_service"] = github

    result = await check_board(state)

    assert result["phase"] == "idle"


# ---------------------------------------------------------------------------
# check_board — session creation fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_board_multi_card_session_has_correct_fields() -> None:
    """Each session created by check_board has expected initial values."""
    from coordinare.graph.nodes.check_board import check_board

    github = AsyncMock()
    github.poll_board.return_value = _make_board(["PVI_1"])

    state = _state_with_config(max_concurrent_cards=2)
    state["github_service"] = github

    result = await check_board(state)

    session = result["active_sessions"]["PVI_1"]
    assert session["phase"] == "dispatching"
    assert session["performer_stage"] == "implementing"
    assert session["card_tokens_total"] == 0
    assert session["system_error_count"] == 0


# ---------------------------------------------------------------------------
# Daemon multi-session — _invoke_multi_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_single_card_mode_uses_direct_invoke() -> None:
    """When max_concurrent_cards=1, daemon uses direct graph.ainvoke."""
    from coordinare.daemon import CoordinareDaemon

    graph = AsyncMock()
    graph.ainvoke.return_value = initial_state()

    daemon = CoordinareDaemon(
        graph,
        max_cycles=1,
        sleep_func=AsyncMock(),
    )
    # No config — defaults to single card
    await daemon.start()

    graph.ainvoke.assert_called_once()


@pytest.mark.asyncio
async def test_daemon_multi_session_invokes_per_session() -> None:
    """When max_concurrent_cards>1 with 2 sessions, graph is invoked per session."""
    from coordinare.daemon import CoordinareDaemon

    invocation_count = 0

    async def fake_invoke(state: dict) -> dict:
        nonlocal invocation_count
        invocation_count += 1
        return state

    graph = AsyncMock()
    graph.ainvoke.side_effect = fake_invoke

    daemon = CoordinareDaemon(
        graph,
        max_cycles=1,
        sleep_func=AsyncMock(),
    )
    config = _make_config(max_concurrent_cards=2)
    daemon._state["config"] = config
    daemon._state["active_sessions"] = {
        "PVI_1": create_session_from_card({"id": "PVI_1", "title": "A"}),
        "PVI_2": create_session_from_card({"id": "PVI_2", "title": "B"}),
    }

    await daemon.start()

    # Should be invoked once per session
    assert invocation_count == 2


@pytest.mark.asyncio
async def test_daemon_multi_session_error_isolation() -> None:
    """An error in one session doesn't prevent processing of others."""
    from coordinare.daemon import CoordinareDaemon

    call_count = 0

    async def fake_invoke(state: dict) -> dict:
        nonlocal call_count
        call_count += 1
        card = state.get("current_card") or {}
        if card.get("id") == "PVI_1":
            raise RuntimeError("Session PVI_1 failed")
        return state

    graph = AsyncMock()
    graph.ainvoke.side_effect = fake_invoke

    daemon = CoordinareDaemon(
        graph,
        max_cycles=1,
        sleep_func=AsyncMock(),
    )
    config = _make_config(max_concurrent_cards=2)
    daemon._state["config"] = config
    daemon._state["active_sessions"] = {
        "PVI_1": create_session_from_card({"id": "PVI_1", "title": "Fail"}),
        "PVI_2": create_session_from_card({"id": "PVI_2", "title": "OK"}),
    }

    await daemon.start()

    # Both sessions should have been attempted
    assert call_count == 2
    # Both sessions should still exist (PVI_1 errored but wasn't removed)
    assert "PVI_1" in daemon._state["active_sessions"]
    assert "PVI_2" in daemon._state["active_sessions"]


@pytest.mark.asyncio
async def test_daemon_multi_session_completed_removed() -> None:
    """Completed sessions (phase=idle, current_card=None) are removed."""
    from coordinare.daemon import CoordinareDaemon

    async def fake_invoke(state: dict) -> dict:
        card = state.get("current_card") or {}
        if card.get("id") == "PVI_1":
            # Mark as completed
            state["phase"] = "idle"
            state["current_card"] = None
        return state

    graph = AsyncMock()
    graph.ainvoke.side_effect = fake_invoke

    daemon = CoordinareDaemon(
        graph,
        max_cycles=1,
        sleep_func=AsyncMock(),
    )
    config = _make_config(max_concurrent_cards=2)
    daemon._state["config"] = config
    daemon._state["active_sessions"] = {
        "PVI_1": create_session_from_card({"id": "PVI_1", "title": "Done"}),
        "PVI_2": create_session_from_card({"id": "PVI_2", "title": "Active"}),
    }

    await daemon.start()

    # PVI_1 should be removed; PVI_2 should remain
    sessions = daemon._state["active_sessions"]
    assert "PVI_1" not in sessions
    assert "PVI_2" in sessions


@pytest.mark.asyncio
async def test_daemon_multi_session_no_sessions_runs_graph_once() -> None:
    """When active_sessions is empty, graph is invoked once to let check_board fill them."""
    from coordinare.daemon import CoordinareDaemon

    graph = AsyncMock()
    graph.ainvoke.return_value = initial_state()

    daemon = CoordinareDaemon(
        graph,
        max_cycles=1,
        sleep_func=AsyncMock(),
    )
    config = _make_config(max_concurrent_cards=3)
    daemon._state["config"] = config
    # Empty active_sessions
    daemon._state["active_sessions"] = {}

    await daemon.start()

    graph.ainvoke.assert_called_once()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_active_sessions_gauge_exists() -> None:
    from coordinare.metrics import METRICS

    assert hasattr(METRICS, "active_sessions")
    # Should be a Gauge
    assert "coordinare_active_sessions" in METRICS.render()


# ---------------------------------------------------------------------------
# Dashboard — active session count
# ---------------------------------------------------------------------------


def test_dashboard_build_snapshot_includes_sessions() -> None:
    from coordinare.dashboard import DashboardStore
    from coordinare.metrics import CoordinareMetrics
    from coordinare.observability import HealthRegistry

    store = DashboardStore()
    metrics = CoordinareMetrics()
    health = HealthRegistry()

    daemon = MagicMock()
    daemon.state = initial_state()
    daemon.state["active_sessions"] = {
        "PVI_1": create_session_from_card({"id": "PVI_1", "title": "Card A"}),
        "PVI_2": create_session_from_card({"id": "PVI_2", "title": "Card B"}),
    }
    daemon._cycle_active = False
    daemon.running = True
    daemon.state_store = None

    snapshot = store.build_snapshot(daemon, metrics, health)

    assert snapshot["active_session_count"] == 2
    assert len(snapshot["active_sessions"]) == 2
    titles = {s["card_title"] for s in snapshot["active_sessions"]}
    assert "Card A" in titles
    assert "Card B" in titles


def test_dashboard_build_snapshot_no_sessions() -> None:
    from coordinare.dashboard import DashboardStore
    from coordinare.metrics import CoordinareMetrics
    from coordinare.observability import HealthRegistry

    store = DashboardStore()
    metrics = CoordinareMetrics()
    health = HealthRegistry()

    daemon = MagicMock()
    daemon.state = initial_state()
    daemon._cycle_active = False
    daemon.running = True
    daemon.state_store = None

    snapshot = store.build_snapshot(daemon, metrics, health)

    assert snapshot["active_session_count"] == 0
    assert snapshot["active_sessions"] == []


def test_dashboard_session_summary_fields() -> None:
    """Each session summary in the dashboard has the expected fields."""
    from coordinare.dashboard import DashboardStore
    from coordinare.metrics import CoordinareMetrics
    from coordinare.observability import HealthRegistry

    store = DashboardStore()
    metrics = CoordinareMetrics()
    health = HealthRegistry()

    session = create_session_from_card({"id": "PVI_1", "title": "Test"})
    session["performer_stage"] = "reviewing"
    session["card_tokens_total"] = 500
    session["card_cost_estimate"] = 1.5

    daemon = MagicMock()
    daemon.state = initial_state()
    daemon.state["active_sessions"] = {"PVI_1": session}
    daemon._cycle_active = False
    daemon.running = True
    daemon.state_store = None

    snapshot = store.build_snapshot(daemon, metrics, health)

    s = snapshot["active_sessions"][0]
    assert s["card_id"] == "PVI_1"
    assert s["card_title"] == "Test"
    assert s["performer_stage"] == "reviewing"
    assert s["card_tokens_total"] == 500
    assert s["card_cost_estimate"] == 1.5
    assert s["phase"] == "dispatching"
