from __future__ import annotations

import pytest

from coordinare.graph.nodes.notify import notify
from coordinare.graph.state import initial_state
from coordinare.models.notification import EventType, NotificationEvent
from tests.utils.fake_notification import FakeNotificationService


@pytest.mark.asyncio
async def test_notify_dispatches_event() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "title": "Card",
        "status": "IN_PROGRESS",
        "previous_status": "TODO",
        "description": "Task",
    }
    state["notification_service"] = fake
    state["phase"] = "dispatching"

    result = await notify(state)

    assert result["current_card"]["title"] == "Card"
    assert len(fake.dispatched) == 1
    assert fake.dispatched[0].event_type == EventType.card_dispatched
    assert fake.dispatched[0].dedup_key == "card_dispatched:Card:IN_PROGRESS"


@pytest.mark.asyncio
async def test_notify_uses_card_id_for_dedup_key_when_available() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "id": "PROJ-42",
        "title": "Card",
        "status": "IN_PROGRESS",
        "previous_status": "TODO",
        "description": "Task",
    }
    state["notification_service"] = fake
    state["phase"] = "dispatching"

    await notify(state)

    assert fake.dispatched[0].dedup_key == "card_dispatched:PROJ-42:IN_PROGRESS"


@pytest.mark.asyncio
async def test_notify_returns_state_when_missing_services() -> None:
    state = initial_state()
    state["current_card"] = {"title": "Card", "status": "TODO"}

    result = await notify(state)

    assert result["current_card"]["title"] == "Card"


@pytest.mark.asyncio
async def test_notify_returns_state_when_no_card() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state["notification_service"] = fake

    await notify(state)

    assert len(fake.dispatched) == 0


@pytest.mark.asyncio
async def test_notify_includes_commit_summary_in_payload() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "title": "Card",
        "status": "DONE",
        "previous_status": "IN_REVIEW",
        "description": "Task",
    }
    state["notification_service"] = fake
    state["phase"] = "merging"
    state["commit_summary"] = "abc1234 Fix bug"

    await notify(state)

    assert len(fake.dispatched) == 1
    assert fake.dispatched[0].payload["commit_summary"] == "abc1234 Fix bug"
    assert fake.dispatched[0].event_type == EventType.card_merged


@pytest.mark.asyncio
async def test_notify_includes_open_questions() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state["current_card"] = {
        "title": "Card",
        "status": "BLOCKED",
        "previous_status": "TODO",
        "description": "Task",
    }
    state["notification_service"] = fake
    state["phase"] = "blocked"
    state["open_questions"] = ["What API?", "Which provider?"]

    await notify(state)

    assert len(fake.dispatched) == 1
    assert "What API?" in fake.dispatched[0].payload["open_questions"]


@pytest.mark.asyncio
async def test_notify_survives_dispatch_failure() -> None:
    """Dispatch errors should be caught and not raised."""

    class _FailingNotification:
        async def dispatch(self, event: NotificationEvent) -> None:
            raise ConnectionError("boom")

    state = initial_state()
    state["current_card"] = {
        "title": "Card",
        "status": "TODO",
        "description": "Task",
    }
    state["notification_service"] = _FailingNotification()

    result = await notify(state)
    assert result is not None
