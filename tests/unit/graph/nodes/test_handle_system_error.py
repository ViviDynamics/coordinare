from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.graph.nodes.handle_system_error import (
    _MAX_RETRIES,
    _RETRY_INTERVAL,
    handle_system_error,
)
from coordinare.graph.state import initial_state


def _state_with_error(count: int = 1, seconds_ago: float = _RETRY_INTERVAL + 1) -> dict:
    state = initial_state()
    state["system_error_count"] = count
    state["system_error_last_at"] = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    state["system_error_reason"] = "push failed"
    state["current_card"] = {"id": "CARD_1", "title": "My feature"}
    return state


# ------------------------------------------------------------------
# Still within retry window — should wait
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_waiting_when_interval_not_elapsed() -> None:
    state = _state_with_error(count=1, seconds_ago=10.0)
    result = await handle_system_error(state)
    # Keep phase="system_error" during wait so check_board's session-error
    # early-return re-routes back to handle_system_error on the next cycle —
    # phase="idle" used to wedge the card permanently (069 follow-up).
    assert result["phase"] == "system_error"
    assert result.get("agent_dispatch") != {}  # dispatch not cleared yet


@pytest.mark.asyncio
async def test_waiting_preserves_error_count() -> None:
    state = _state_with_error(count=2, seconds_ago=30.0)
    result = await handle_system_error(state)
    assert result["phase"] == "system_error"
    assert result["system_error_count"] == 2


# ------------------------------------------------------------------
# Interval elapsed — should retry
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_when_interval_elapsed() -> None:
    state = _state_with_error(count=1, seconds_ago=100.0)
    result = await handle_system_error(state)
    assert result["phase"] == "dispatching"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None


@pytest.mark.asyncio
async def test_retry_on_second_attempt() -> None:
    state = _state_with_error(count=2, seconds_ago=100.0)
    result = await handle_system_error(state)
    assert result["phase"] == "dispatching"


@pytest.mark.asyncio
async def test_retry_when_no_last_at() -> None:
    """No timestamp means first invocation — treat as elapsed, retry immediately."""
    state = initial_state()
    state["system_error_count"] = 1
    state["system_error_last_at"] = None
    state["current_card"] = {"id": "C1"}
    result = await handle_system_error(state)
    assert result["phase"] == "dispatching"


# ------------------------------------------------------------------
# Max retries exhausted — notify and block
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_retries_notifies_operator() -> None:
    state = _state_with_error(count=_MAX_RETRIES, seconds_ago=200.0)
    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock()
    state["notification_service"] = notification_service

    github = MagicMock()
    github.move_card = AsyncMock()
    state["github_service"] = github

    result = await handle_system_error(state)

    assert result["phase"] == "idle"
    assert result["system_error_notified"] is True
    notification_service.dispatch.assert_called_once()
    github.move_card.assert_called_once_with("CARD_1", "BLOCKED")


@pytest.mark.asyncio
async def test_max_retries_no_duplicate_notification() -> None:
    """If system_error_notified is already True, skip notification and move_card."""
    state = _state_with_error(count=_MAX_RETRIES)
    state["system_error_notified"] = True
    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock()
    state["notification_service"] = notification_service

    github = MagicMock()
    github.move_card = AsyncMock()
    state["github_service"] = github

    result = await handle_system_error(state)

    assert result["phase"] == "idle"
    notification_service.dispatch.assert_not_called()
    github.move_card.assert_not_called()


@pytest.mark.asyncio
async def test_max_retries_no_notification_service() -> None:
    """Gracefully handles missing notification_service."""
    state = _state_with_error(count=_MAX_RETRIES)
    github = MagicMock()
    github.move_card = AsyncMock()
    state["github_service"] = github

    result = await handle_system_error(state)

    assert result["phase"] == "idle"
    assert result["system_error_notified"] is True
    github.move_card.assert_called_once_with("CARD_1", "BLOCKED")


@pytest.mark.asyncio
async def test_max_retries_notification_failure_is_swallowed() -> None:
    """Notification errors don't propagate."""
    state = _state_with_error(count=_MAX_RETRIES)
    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock(side_effect=RuntimeError("slack down"))
    state["notification_service"] = notification_service

    github = MagicMock()
    github.move_card = AsyncMock()
    state["github_service"] = github

    result = await handle_system_error(state)
    assert result["phase"] == "idle"
    assert result["system_error_notified"] is True


@pytest.mark.asyncio
async def test_max_retries_move_card_failure_is_swallowed() -> None:
    """GitHub move_card errors don't propagate."""
    state = _state_with_error(count=_MAX_RETRIES)
    github = MagicMock()
    github.move_card = AsyncMock(side_effect=RuntimeError("github down"))
    state["github_service"] = github

    result = await handle_system_error(state)
    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_max_retries_no_github_service() -> None:
    state = _state_with_error(count=_MAX_RETRIES)
    result = await handle_system_error(state)
    assert result["phase"] == "idle"
    assert result["system_error_notified"] is True


@pytest.mark.asyncio
async def test_retry_tears_down_orphan_container() -> None:
    """On retry, the previous ephemeral container is cleaned up before re-dispatch."""
    state = _state_with_error(count=1, seconds_ago=100.0)
    state["agent_dispatch"] = {"session_id": "sess-abc"}
    state["performer_stage"] = "developer"
    cleanup = AsyncMock()
    service = MagicMock()
    service._cleanup_ephemeral_job_by_id = cleanup
    state["performer_services"] = {"developer": service}

    result = await handle_system_error(state)

    assert result["phase"] == "dispatching"
    assert result["agent_dispatch"] == {}
    cleanup.assert_awaited_once_with("sess-abc")


@pytest.mark.asyncio
async def test_retry_cleanup_failure_is_swallowed() -> None:
    """Cleanup errors during retry don't propagate or block re-dispatch."""
    state = _state_with_error(count=1, seconds_ago=100.0)
    state["agent_dispatch"] = {"session_id": "sess-xyz"}
    state["performer_stage"] = "developer"
    service = MagicMock()
    service._cleanup_ephemeral_job_by_id = AsyncMock(side_effect=RuntimeError("docker gone"))
    state["performer_services"] = {"developer": service}

    result = await handle_system_error(state)

    assert result["phase"] == "dispatching"
    assert result["agent_dispatch"] == {}


@pytest.mark.asyncio
async def test_retry_without_session_id_skips_cleanup() -> None:
    """No session_id means nothing to clean up — retry still proceeds."""
    state = _state_with_error(count=1, seconds_ago=100.0)
    state["agent_dispatch"] = {}
    service = MagicMock()
    service._cleanup_ephemeral_job_by_id = AsyncMock()
    state["performer_services"] = {"developer": service}
    state["performer_stage"] = "developer"

    result = await handle_system_error(state)

    assert result["phase"] == "dispatching"
    service._cleanup_ephemeral_job_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_max_retries_no_card_id() -> None:
    """No card_id means move_card is skipped."""
    state = initial_state()
    state["system_error_count"] = _MAX_RETRIES
    state["current_card"] = {}
    github = MagicMock()
    github.move_card = AsyncMock()
    state["github_service"] = github

    result = await handle_system_error(state)
    assert result["phase"] == "idle"
    github.move_card.assert_not_called()
