"""Unit tests for cancel_active_card (026-card-cancellation)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.cancel import cancel_active_card
from coordinare.graph.state import initial_state


@pytest.mark.asyncio
async def test_cancel_active_card_returns_cancelled() -> None:
    """Cancelling an active card resets state and returns cancelled."""
    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["current_card"] = {"id": "ITEM_1", "title": "Test Card", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["agent_service"] = MagicMock(relay_feedback=AsyncMock(return_value={}))
    state["github_service"] = MagicMock(move_card=AsyncMock())
    state["notification_service"] = MagicMock(dispatch=AsyncMock())

    result = await cancel_active_card(state)

    assert result["status"] == "cancelled"
    assert result["card_id"] == "ITEM_1"
    assert state["phase"] == "idle"
    assert state["current_card"] is None
    assert state["agent_dispatch"] == {}


@pytest.mark.asyncio
async def test_cancel_idle_returns_no_active_card() -> None:
    """Cancelling when idle returns no_active_card with no side effects."""
    state = initial_state()
    state["phase"] = "idle"

    result = await cancel_active_card(state)

    assert result["status"] == "no_active_card"
    assert state["phase"] == "idle"


@pytest.mark.asyncio
async def test_cancel_moves_card_to_todo() -> None:
    """By default, cancellation moves the card to TODO on the board."""
    github = MagicMock(move_card=AsyncMock())
    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["current_card"] = {"id": "ITEM_1", "title": "Card"}
    state["github_service"] = github
    state["notification_service"] = MagicMock(dispatch=AsyncMock())

    await cancel_active_card(state, move_to_todo=True)

    github.move_card.assert_called_once_with("ITEM_1", "TODO")


@pytest.mark.asyncio
async def test_cancel_skips_todo_when_board_triggered() -> None:
    """Board-triggered cancellation does not move card to TODO."""
    github = MagicMock(move_card=AsyncMock())
    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["current_card"] = {"id": "ITEM_1", "title": "Card"}
    state["github_service"] = github
    state["notification_service"] = MagicMock(dispatch=AsyncMock())

    await cancel_active_card(state, move_to_todo=False)

    github.move_card.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_emits_notification() -> None:
    """Cancellation emits a card_cancelled notification."""
    notification = MagicMock(dispatch=AsyncMock())
    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["current_card"] = {"id": "ITEM_1", "title": "Card"}
    state["notification_service"] = notification

    await cancel_active_card(state)

    notification.dispatch.assert_called_once()
    event = notification.dispatch.call_args[0][0]
    assert event.event_type.value == "card_cancelled"
    assert "summary" in event.payload
    assert "source" in event.payload
    assert "event_type" in event.payload


@pytest.mark.asyncio
async def test_board_triggered_cancellation_via_check_board() -> None:
    """Active card missing from all columns triggers cancellation in check_board."""
    from coordinare.graph.nodes.check_board import check_board

    class _GitHubNoCard:
        async def poll_board(self):
            return {
                "snapshot": {"TODO": [], "IN_PROGRESS": [], "IN_REVIEW": [], "BLOCKED": [], "DONE": []},
                "titles": {},
                "descriptions": {},
                "issue_numbers": {},
            }
        async def move_card(self, item_id, status): pass

    state = initial_state()
    state["github_service"] = _GitHubNoCard()
    state["phase"] = "monitoring_performer"
    state["current_card"] = {"id": "ITEM_GONE", "title": "Vanished Card", "status": "IN_PROGRESS"}
    state["notification_service"] = MagicMock(dispatch=AsyncMock())

    result = await check_board(state)

    assert result["phase"] == "idle"
    assert result["current_card"] is None


def test_cancel_endpoint_returns_200(tmp_path) -> None:
    """POST /api/cancel returns 200 with cancellation result."""
    import asyncio
    from unittest.mock import MagicMock

    from starlette.testclient import TestClient

    from coordinare.dashboard import DashboardStore, create_dashboard_app
    from coordinare.observability import HealthRegistry

    store = DashboardStore()
    daemon = MagicMock()
    daemon.state = initial_state()
    daemon.state["phase"] = "monitoring_performer"
    daemon.state["current_card"] = {"id": "ITEM_1", "title": "Card"}
    daemon.state["notification_service"] = MagicMock(dispatch=AsyncMock())
    daemon._cycle_active = False
    daemon._webhook_trigger = asyncio.Event()
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    daemon.running = True

    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    metrics.build_info.labels.return_value._value.get.return_value = {}

    health = MagicMock(spec=HealthRegistry)
    health.snapshot.return_value.probes = []

    app = create_dashboard_app(store, daemon, metrics, health)
    client = TestClient(app)

    res = client.post("/api/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Exception-swallowing paths in cancel_active_card (lines 48-68, 120-121)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_performer_stop_timeout_is_swallowed() -> None:
    """TimeoutError from performer stop is caught and logged; cancellation still completes."""
    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["current_card"] = {"id": "ITEM_1", "title": "Card"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["agent_service"] = MagicMock(
        relay_feedback=AsyncMock(side_effect=TimeoutError("performer stop timed out"))
    )
    state["notification_service"] = MagicMock(dispatch=AsyncMock())

    result = await cancel_active_card(state)

    assert result["status"] == "cancelled"
    assert state["phase"] == "idle"


@pytest.mark.asyncio
async def test_cancel_workspace_cleanup_exception_is_swallowed() -> None:
    """Exception from workspace teardown is caught and logged; cancellation still completes."""
    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["current_card"] = {"id": "ITEM_1", "title": "Card"}

    workspace_manager = MagicMock()
    workspace_manager.teardown = AsyncMock(side_effect=RuntimeError("workspace teardown failed"))
    state["workspace_manager"] = workspace_manager
    state["workspace_path"] = "/tmp/workspace"
    state["notification_service"] = MagicMock(dispatch=AsyncMock())

    result = await cancel_active_card(state)

    assert result["status"] == "cancelled"
    assert state["phase"] == "idle"


@pytest.mark.asyncio
async def test_cancel_move_to_todo_exception_is_swallowed() -> None:
    """Exception from move_card(TODO) is caught and logged; cancellation still completes."""
    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["current_card"] = {"id": "ITEM_1", "title": "Card"}

    github = MagicMock()
    github.move_card = AsyncMock(side_effect=RuntimeError("board API unavailable"))
    state["github_service"] = github
    state["notification_service"] = MagicMock(dispatch=AsyncMock())

    result = await cancel_active_card(state, move_to_todo=True)

    assert result["status"] == "cancelled"
    assert state["phase"] == "idle"


@pytest.mark.asyncio
async def test_cancel_notification_exception_is_swallowed() -> None:
    """Exception from notification dispatch is caught and logged; result is still 'cancelled'."""
    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["current_card"] = {"id": "ITEM_1", "title": "Card"}

    notification = MagicMock()
    notification.dispatch = AsyncMock(side_effect=RuntimeError("notification service down"))
    state["notification_service"] = notification

    result = await cancel_active_card(state)

    assert result["status"] == "cancelled"
    assert result["card_id"] == "ITEM_1"


def test_cancel_endpoint_idle_returns_no_active_card(tmp_path) -> None:
    """POST /api/cancel when idle returns no_active_card."""
    import asyncio
    from unittest.mock import MagicMock

    from starlette.testclient import TestClient

    from coordinare.dashboard import DashboardStore, create_dashboard_app
    from coordinare.observability import HealthRegistry

    store = DashboardStore()
    daemon = MagicMock()
    daemon.state = initial_state()
    daemon._cycle_active = False
    daemon._webhook_trigger = asyncio.Event()
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    daemon.running = True

    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    metrics.build_info.labels.return_value._value.get.return_value = {}

    health = MagicMock(spec=HealthRegistry)
    health.snapshot.return_value.probes = []

    app = create_dashboard_app(store, daemon, metrics, health)
    client = TestClient(app)

    res = client.post("/api/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "no_active_card"
