"""Spec 076 T065 — notify.py reconciliation-decision dedup.

Per contracts/notification-dedup.md, the card_dispatched event MUST be
suppressed when the most recent reconciliation pass produced an
ADOPTED or SKIPPED_PERSISTENT decision for the card.  REAPED_AND_REPLACED
and FRESH_DISPATCHED outcomes MUST still emit the notification.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from coordinare.graph.nodes.notify import notify
from coordinare.graph.state import initial_state
from coordinare.models.notification import EventType


class _FakeNotificationService:
    def __init__(self) -> None:
        self.dispatched: list[EventType] = []

    async def dispatch(self, event, **kwargs):
        self.dispatched.append(event.event_type)

    async def is_event_dedup_window_active(self, *a, **kw):
        return False


@pytest.fixture
def _state_with_dispatched_card():
    """Build a state that would normally trigger card_dispatched."""
    state = initial_state()
    state["current_card"] = {
        "id": "PVTI_X",
        "title": "Test card",
        "status": "IN_PROGRESS",
        "issue_url": "https://github.com/x/y/issues/1",
        "issue_number": 1,
    }
    state["phase"] = "monitoring_performer"
    state["performer_stage"] = "implementing"
    state["dispatched_notified_stages"] = []
    state["active_sessions"] = {
        "PVTI_X": {
            "current_card": state["current_card"],
            "performer_stage": "implementing",
            "phase": "monitoring_performer",
            "dispatched_notified_stages": [],
        }
    }
    state["active_card_id"] = "PVTI_X"
    state["notification_service"] = _FakeNotificationService()
    state["resilience_service"] = MagicMock()
    state["resilience_service"].record_event = MagicMock()
    return state


@pytest.mark.asyncio
async def test_card_dispatched_suppressed_when_adopted(_state_with_dispatched_card) -> None:
    """Reconciliation said ADOPTED → operator already saw the original
    dispatch notification from the prior daemon; suppress this one."""
    state = _state_with_dispatched_card
    state["reconciliation_decisions_last_startup"] = {"PVTI_X": "adopted"}

    result = await notify(state)

    svc: _FakeNotificationService = result["notification_service"]
    assert EventType.card_dispatched not in svc.dispatched


@pytest.mark.asyncio
async def test_card_dispatched_suppressed_when_skipped_persistent(_state_with_dispatched_card) -> None:
    """SKIPPED_PERSISTENT → no actual new dispatch happened, no
    notification."""
    state = _state_with_dispatched_card
    state["reconciliation_decisions_last_startup"] = {"PVTI_X": "skipped_persistent"}

    result = await notify(state)

    svc: _FakeNotificationService = result["notification_service"]
    assert EventType.card_dispatched not in svc.dispatched


@pytest.mark.asyncio
async def test_card_dispatched_emitted_when_reaped_and_replaced(_state_with_dispatched_card) -> None:
    """REAPED_AND_REPLACED IS a new dispatch from the operator's
    perspective; emit the notification."""
    state = _state_with_dispatched_card
    state["reconciliation_decisions_last_startup"] = {"PVTI_X": "reaped_and_replaced"}

    result = await notify(state)

    svc: _FakeNotificationService = result["notification_service"]
    assert EventType.card_dispatched in svc.dispatched


@pytest.mark.asyncio
async def test_card_dispatched_emitted_when_fresh_dispatched(_state_with_dispatched_card) -> None:
    """FRESH_DISPATCHED → standard new dispatch, normal notification."""
    state = _state_with_dispatched_card
    state["reconciliation_decisions_last_startup"] = {"PVTI_X": "fresh_dispatched"}

    result = await notify(state)

    svc: _FakeNotificationService = result["notification_service"]
    assert EventType.card_dispatched in svc.dispatched


@pytest.mark.asyncio
async def test_card_dispatched_emitted_when_no_reconciliation_decision(_state_with_dispatched_card) -> None:
    """No prior reconciliation pass (cold mid-run dispatch) → no
    suppression; notification fires normally."""
    state = _state_with_dispatched_card
    state["reconciliation_decisions_last_startup"] = {}

    result = await notify(state)

    svc: _FakeNotificationService = result["notification_service"]
    assert EventType.card_dispatched in svc.dispatched
