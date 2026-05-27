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
async def test_notify_suppresses_dispatched_re_emit_for_same_stage() -> None:
    """Once card_dispatched has fired for (card, performer_stage), a later
    pass on the same active session must not re-emit even after the
    NotificationService dedup window would have expired.

    Reproduces 2026-05-26 incident where #138 produced a duplicate
    'dispatched to implementing' Slack post 11 min apart while env_bootstrap
    was still running (phase stayed in monitoring_performer; dedup TTL=600s
    elapsed and the same dedup_key re-fired).
    """
    state = initial_state()
    card = {
        "id": "card-138",
        "title": "Add robots.txt",
        "status": "IN_PROGRESS",
    }
    state["current_card"] = card
    state["phase"] = "monitoring_performer"
    state["performer_stage"] = "implementing"
    state["active_sessions"] = {"card-138": {}}

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock()
    state["notification_service"] = notification_service

    await notify(state)
    assert notification_service.dispatch.await_count == 1

    await notify(state)
    assert notification_service.dispatch.await_count == 1

    # Stage transition produces a fresh notification.
    state["performer_stage"] = "reviewing"
    await notify(state)
    assert notification_service.dispatch.await_count == 2


@pytest.mark.asyncio
async def test_notify_dispatched_re_emits_after_simulated_restart() -> None:
    """The dispatched-stages list is in-memory only; if a restart drops it
    the next pass MUST re-emit so operators see which cards came back.
    """
    state = initial_state()
    state["current_card"] = {"id": "card-138", "title": "Add robots.txt", "status": "IN_PROGRESS"}
    state["phase"] = "monitoring_performer"
    state["performer_stage"] = "implementing"
    state["active_sessions"] = {"card-138": {}}  # session entry present, list absent

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock()
    state["notification_service"] = notification_service

    await notify(state)
    assert notification_service.dispatch.await_count == 1
    assert state["active_sessions"]["card-138"]["dispatched_notified_stages"] == ["implementing"]


@pytest.mark.asyncio
async def test_notify_dispatched_does_not_stamp_when_session_missing() -> None:
    """When active_sessions has no entry for the card the throwaway sess
    dict cannot retain the stamp; verify we never re-emit-suppress against
    a discarded dict (gate is open until the session entry exists).
    """
    state = initial_state()
    state["current_card"] = {"id": "card-999", "title": "Orphan", "status": "IN_PROGRESS"}
    state["phase"] = "monitoring_performer"
    state["performer_stage"] = "implementing"
    state["active_sessions"] = {}  # no entry for card-999

    notification_service = MagicMock()
    notification_service.dispatch = AsyncMock()
    state["notification_service"] = notification_service

    await notify(state)
    await notify(state)
    # Both fire — the upstream DeduplicationWindow is what protects against
    # within-window repeats. The dispatched-once gate only engages once the
    # session entry is registered.
    assert notification_service.dispatch.await_count == 2
    assert "card-999" not in state["active_sessions"]


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
