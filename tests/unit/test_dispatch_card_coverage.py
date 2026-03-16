"""Coverage tests for coordinare/graph/nodes/dispatch_card.py.

Targets the workspace teardown failure path (lines 99-100):

    except Exception:
        logger.warning("workspace_teardown_failed.after_transport_error", card_id=card_id)

This fires when workspace_manager.teardown() raises during the TransportError handler.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.graph.nodes.dispatch_card import dispatch_card
from coordinare.graph.state import initial_state
from coordinare.transport.base import TransportError


def _make_state_with_card() -> dict:
    state = initial_state()
    state["current_card"] = {
        "id": "card-001",
        "title": "Test Card",
        "status": "TODO",
    }
    return state


@pytest.mark.asyncio
async def test_workspace_teardown_failure_after_transport_error_does_not_propagate() -> None:
    """When teardown() raises during TransportError handling the exception is swallowed."""
    state = _make_state_with_card()

    # Agent health returns healthy so dispatch proceeds
    agent = MagicMock()
    agent.check_health = AsyncMock(return_value={"status": "healthy"})
    # dispatch_card raises TransportError to trigger the error path
    agent.dispatch_card = AsyncMock(side_effect=TransportError("network failure"))

    github = MagicMock()
    github.move_card = AsyncMock()

    # Workspace prepare succeeds, but teardown raises
    fake_workspace_info = MagicMock()
    fake_workspace_info.path = Path("/tmp/fake-workspace")
    fake_workspace_info.branch = "coordinare/card-001"

    workspace_manager = MagicMock()
    workspace_manager.prepare = AsyncMock(return_value=fake_workspace_info)
    workspace_manager.teardown = AsyncMock(side_effect=Exception("teardown failed"))

    state["agent_service"] = agent
    state["github_service"] = github
    state["workspace_manager"] = workspace_manager

    # Must not raise despite teardown() throwing
    result = await dispatch_card(state)

    # Function returns a state dict
    assert isinstance(result, dict)
    # The TransportError path sets phase to system_error
    assert result.get("phase") == "system_error"
    # workspace fields are cleared even when teardown raised
    assert result.get("workspace_path") is None
    assert result.get("workspace_branch") is None
    # teardown was attempted
    workspace_manager.teardown.assert_awaited_once()


@pytest.mark.asyncio
async def test_workspace_teardown_not_called_when_workspace_info_is_none() -> None:
    """Teardown is skipped (no workspace was set up) on TransportError."""
    state = _make_state_with_card()

    agent = MagicMock()
    agent.check_health = AsyncMock(return_value={"status": "healthy"})
    agent.dispatch_card = AsyncMock(side_effect=TransportError("network failure"))

    github = MagicMock()
    github.move_card = AsyncMock()

    # workspace_manager present but prepare is never called — simulate no workspace_info
    # by not adding a workspace_manager at all; the workspace_info stays None
    state["agent_service"] = agent
    state["github_service"] = github
    # No workspace_manager in state → workspace_info stays None

    result = await dispatch_card(state)

    assert result.get("phase") == "system_error"


@pytest.mark.asyncio
async def test_performer_error_status_sets_system_error_phase() -> None:
    """Lines 119-125: performer returns status='error' → phase becomes system_error."""
    state = _make_state_with_card()

    agent = MagicMock()
    agent.check_health = AsyncMock(return_value={"status": "healthy"})
    agent.dispatch_card = AsyncMock(return_value={"status": "error", "reason": "performer failure"})

    github = MagicMock()
    github.move_card = AsyncMock()

    state["agent_service"] = agent
    state["github_service"] = github

    result = await dispatch_card(state)

    assert result.get("phase") == "system_error"
    assert "performer failure" in result.get("system_error_reason", "")
    assert result.get("system_error_count", 0) >= 1
