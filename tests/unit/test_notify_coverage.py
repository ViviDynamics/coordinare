"""Coverage tests for coordinare/graph/nodes/notify.py.

Targets line 54 — the pr_url branch:
    if card.get("pr_url"):
        payload["pr_url"] = str(card["pr_url"])
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.graph.nodes.notify import notify
from coordinare.graph.state import initial_state
from coordinare.models.notification import NotificationEvent


@pytest.mark.asyncio
async def test_notify_includes_pr_url_in_payload_when_set() -> None:
    """pr_url on the card is forwarded into the notification payload.

    Uses a card_dispatched scenario rather than the merging phase because
    042 fixed the bug where merging phase fired notifications without a
    real merge — see test_notify_merging_phase_without_commit_summary_does_not_fire.
    """
    state = initial_state()
    state["current_card"] = {
        "id": "card-42",
        "title": "Add feature",
        "status": "IN_PROGRESS",
        "previous_status": "TODO",
        "pr_url": "https://github.com/org/repo/pull/99",
    }
    state["phase"] = "dispatching"

    dispatched: list[NotificationEvent] = []

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock(side_effect=dispatched.append)
    state["notification_service"] = notification_service

    result = await notify(state)

    assert isinstance(result, dict)
    notification_service.dispatch.assert_awaited_once()
    event: NotificationEvent = dispatched[0]
    assert "pr_url" in event.payload
    assert event.payload["pr_url"] == "https://github.com/org/repo/pull/99"


@pytest.mark.asyncio
async def test_notify_excludes_pr_url_from_payload_when_not_set() -> None:
    """When card has no pr_url the payload key is absent."""
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "title": "Some card",
        "status": "TODO",
        "previous_status": "",
    }
    state["phase"] = "dispatching"

    dispatched: list[NotificationEvent] = []

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock(side_effect=dispatched.append)
    state["notification_service"] = notification_service

    await notify(state)

    event: NotificationEvent = dispatched[0]
    assert "pr_url" not in event.payload


@pytest.mark.asyncio
async def test_notify_handles_dispatch_exception_gracefully() -> None:
    """If dispatch() raises, notify() must still return state without propagating."""
    state = initial_state()
    state["current_card"] = {
        "id": "card-5",
        "title": "Card",
        "status": "BLOCKED",
        "pr_url": "https://github.com/org/repo/pull/1",
    }
    state["phase"] = "blocked"

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock(side_effect=RuntimeError("downstream error"))
    state["notification_service"] = notification_service

    result = await notify(state)
    assert isinstance(result, dict)
