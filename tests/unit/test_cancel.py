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
    client = TestClient(app, base_url="http://127.0.0.1:8090")

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
        relay_feedback=AsyncMock(side_effect=TimeoutError("performer stop timed out")),
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


@pytest.mark.asyncio
async def test_cancel_performer_stop_exception_is_swallowed() -> None:
    """Non-TimeoutError exception from performer stop is caught and logged; cancellation completes."""
    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["current_card"] = {"id": "ITEM_1", "title": "Card"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["agent_service"] = MagicMock(
        relay_feedback=AsyncMock(side_effect=RuntimeError("performer API error")),
    )
    state["notification_service"] = MagicMock(dispatch=AsyncMock())

    result = await cancel_active_card(state)

    assert result["status"] == "cancelled"
    assert state["phase"] == "idle"


@pytest.mark.asyncio
async def test_cancel_without_notification_service() -> None:
    """Cancellation skips notification when notification_service is None."""
    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["current_card"] = {"id": "ITEM_1", "title": "Card"}
    state["notification_service"] = None

    result = await cancel_active_card(state)

    assert result["status"] == "cancelled"
    assert state["phase"] == "idle"


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
    client = TestClient(app, base_url="http://127.0.0.1:8090")

    res = client.post("/api/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "no_active_card"


@pytest.mark.asyncio
async def test_cancel_clears_all_system_error_fields_including_last_at() -> None:
    """Candidate #4 — cancel.py:82-85.

    Operator-initiated cancellation must clear EVERY system_error field —
    including ``system_error_last_at`` — so the next dispatch starts with a
    clean slate.  This is the correct behavior; the IN_PROGRESS-recovery
    and new-card-pickup paths in check_board.py omit ``last_at`` and leak
    stale mid-retry signal into the next card's lifecycle.  This test
    locks in the cancel path as the reference implementation.
    """
    from datetime import UTC, datetime

    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["current_card"] = {"id": "ITEM_X", "title": "Stuck", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["agent_service"] = MagicMock(relay_feedback=AsyncMock(return_value={}))
    state["github_service"] = MagicMock(move_card=AsyncMock())
    state["system_error_count"] = 2
    state["system_error_reason"] = "Transport failure during status check"
    state["system_error_notified"] = False
    state["system_error_last_at"] = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)

    await cancel_active_card(state)

    assert state["system_error_count"] == 0
    assert state["system_error_reason"] is None
    assert state["system_error_notified"] is False
    assert state["system_error_last_at"] is None, (
        "cancel must clear system_error_last_at so the next card's first "
        "transport bump isn't misread as mid-retry."
    )


@pytest.mark.asyncio
async def test_cancel_retires_active_session() -> None:
    """066 FR-010: cancel must remove the active_sessions entry and drop
    active_card_id, not merely clear the top-level current_card mirror."""
    state = initial_state()
    state["phase"] = "monitoring_performer"
    card = {"id": "ITEM_1", "title": "Test", "status": "IN_PROGRESS"}
    state["active_card_id"] = "ITEM_1"
    state["active_sessions"] = {
        "ITEM_1": {
            "current_card": card,
            "agent_dispatch": {"session_id": "s1"},
            "performer_stage": "implementing",
            "phase": "monitoring_performer",
        },
    }
    state["current_card"] = card
    state["agent_dispatch"] = {"session_id": "s1"}
    state["agent_service"] = MagicMock(relay_feedback=AsyncMock(return_value={}))
    state["github_service"] = MagicMock(move_card=AsyncMock())

    await cancel_active_card(state)

    assert state["current_card"] is None
    assert state["active_card_id"] is None
    assert "ITEM_1" not in state["active_sessions"]


# --- 377: the override levers must work on a restored multi-session state ----


def _restored_multi_session_state() -> dict:
    """State as it is after a daemon restart, while a performer is running.

    The shape matters and is the whole of #377. Each session's ``current_card``
    is NOT persisted -- it is "rebuilt from the live board by check_board's
    re-adopt path" (daemon.py) -- so after a restart the derived top-level
    mirror is None while `active_card_id` and `active_sessions` are populated
    and a performer is genuinely running.

    Every existing test in this file sets ``current_card`` directly, which is
    the single-card model and why this defect passed CI while being inert in
    production.
    """
    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["active_card_id"] = "ITEM_1"
    state["active_card_title"] = "Weekly timesheet"
    state["active_sessions"] = {
        "ITEM_1": {"card_id": "ITEM_1", "performer_stage": "implementing", "phase": "monitoring_performer"},
    }
    state["current_card"] = None  # the mirror, not yet re-derived
    state["agent_dispatch"] = {"session_id": "s1"}
    state["agent_service"] = MagicMock(relay_feedback=AsyncMock(return_value={}))
    state["github_service"] = MagicMock(move_card=AsyncMock())
    state["notification_service"] = MagicMock(dispatch=AsyncMock())
    return state


@pytest.mark.asyncio
async def test_cancel_works_on_a_restored_session_without_the_mirror() -> None:
    """The defect: observed live, with a performer running and being polled.

        POST /api/cancel -> {"status": "no_active_card"}

    while monitor_performer logged has_live_session=True every 37s. Cancel is
    the operator's emergency stop, and it was inert in the one state where they
    would reach for it. The workaround -- stopping the container by hand -- is
    worse than nothing: coordinare reads the dead container as a system error and
    spends one of the card's three retries.
    """
    state = _restored_multi_session_state()

    result = await cancel_active_card(state)

    assert result["status"] == "cancelled", "a live session must be cancellable"
    assert result["card_id"] == "ITEM_1"
    assert state["phase"] == "idle"
    assert state["agent_dispatch"] == {}


@pytest.mark.asyncio
async def test_the_performer_is_actually_stopped_not_just_the_state_reset() -> None:
    """Resolving the card is pointless if the performer keeps running."""
    state = _restored_multi_session_state()
    relay = state["agent_service"].relay_feedback

    await cancel_active_card(state)

    assert relay.await_count == 1, "the performer session was never told to stop"
    sent = relay.await_args.args[0]
    assert sent.get("action") == "cancel" and sent.get("session_id") == "s1"


@pytest.mark.asyncio
async def test_the_card_goes_back_to_the_board() -> None:
    """Otherwise the card is stranded: not running, not queued."""
    state = _restored_multi_session_state()

    await cancel_active_card(state)

    state["github_service"].move_card.assert_awaited()
    assert "ITEM_1" in str(state["github_service"].move_card.await_args)


@pytest.mark.asyncio
async def test_genuinely_idle_still_reports_no_active_card() -> None:
    """The guard must still refuse when there is really nothing to cancel.

    A fix that resolves a card out of an empty state would make the endpoint
    always claim success, which is a worse lie than the original refusal.
    """
    state = initial_state()
    state["phase"] = "idle"
    state["active_sessions"] = {}
    state["active_card_id"] = None

    assert (await cancel_active_card(state))["status"] == "no_active_card"


@pytest.mark.asyncio
async def test_a_stale_active_card_id_with_no_session_is_not_cancellable() -> None:
    """active_card_id pointing at a session that no longer exists is not a card."""
    state = initial_state()
    state["phase"] = "monitoring_performer"
    state["active_card_id"] = "GONE"
    state["active_sessions"] = {}

    assert (await cancel_active_card(state))["status"] == "no_active_card"


@pytest.mark.asyncio
async def test_idle_refuses_even_when_a_session_is_still_resolvable() -> None:
    """The phase guard is load-bearing on its own, not just belt-and-braces.

    A session entry can outlive the work it described -- retirement clears it,
    but a snapshot restored while idle, or a session awaiting reconciliation,
    leaves a resolvable card id with nothing actually running. Cancelling then
    would stop a performer that does not exist, move a card nobody was working
    on back to Todo, and reset counters for a run that already finished.

    This survived a mutation run with the phase check removed: every other test
    either has no resolvable card or an active phase, so nothing held it.
    """
    state = initial_state()
    state["phase"] = "idle"
    state["active_card_id"] = "ITEM_1"
    state["active_sessions"] = {"ITEM_1": {"card_id": "ITEM_1", "performer_stage": "implementing"}}
    state["github_service"] = MagicMock(move_card=AsyncMock())

    result = await cancel_active_card(state)

    assert result["status"] == "no_active_card", "idle must refuse, whatever state lingers"
    state["github_service"].move_card.assert_not_awaited()


# --- 377 review: resolving the card is not enough --------------------------


@pytest.mark.asyncio
async def test_a_card_readopted_after_restart_reports_that_it_could_not_stop() -> None:
    """The case this whole fix exists for, and where it nearly lied.

    `agent_dispatch` is deliberately not persisted -- state_store calls it a
    transient field "intentionally omitted" -- and check_board says a freshly
    readopted session has no `agent_dispatch.session_id`. So exactly when a card
    is readopted after a restart, there is no id to stop the performer with.

    Before this, cancel returned a bare "cancelled": the card was reset and
    moved to Todo while the performer kept running. That is worse than the
    original `no_active_card`, because it is a confident lie.
    """
    state = _restored_multi_session_state()
    state["agent_dispatch"] = {}  # not persisted, so empty after a restart

    result = await cancel_active_card(state)

    assert result["performer_stopped"] is False
    assert "could not reach the performer" in result.get("warning", "")
    state["agent_service"].relay_feedback.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_card_that_could_not_be_stopped_is_not_returned_to_todo() -> None:
    """Returning it would hand a live branch a second performer.

    A card in Todo is eligible for pickup. If the old performer is still alive,
    two of them end up on one branch pushing over each other. Leaving the card
    in place is recoverable; a double dispatch is not.
    """
    state = _restored_multi_session_state()
    state["agent_dispatch"] = {}

    result = await cancel_active_card(state)

    assert result["returned_to_todo"] is False
    state["github_service"].move_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_session_id_belonging_to_another_card_is_not_used() -> None:
    """Stale dispatch: a retired session leaves the old card's id behind.

    check_board can promote a new active_card_id without touching
    agent_dispatch, so the id can name a different card than the one resolved.
    Stopping that one would kill an unrelated performer and leave this card's
    own performer running.
    """
    state = _restored_multi_session_state()
    state["agent_dispatch"] = {"session_id": "s_other", "card_id": "SOME_OTHER_CARD"}

    result = await cancel_active_card(state)

    state["agent_service"].relay_feedback.assert_not_awaited()
    assert result["performer_stopped"] is False


@pytest.mark.asyncio
async def test_a_confirmed_stop_still_returns_the_card_to_todo() -> None:
    """The happy path must keep working: stopped means returned."""
    state = _restored_multi_session_state()  # carries a matching session id

    result = await cancel_active_card(state)

    assert result["performer_stopped"] is True
    assert result["returned_to_todo"] is True
    assert "warning" not in result
    state["github_service"].move_card.assert_awaited()
