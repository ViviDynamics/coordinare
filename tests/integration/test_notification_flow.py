from __future__ import annotations

import pytest

from coordinare.graph.nodes.notify import notify
from coordinare.graph.state import initial_state
from coordinare.models.notification import EventType
from tests.utils.fake_notification import FakeNotificationService


@pytest.mark.asyncio
async def test_notification_pipeline_dispatches_event() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state.update(
        {
            "current_card": {
                "title": "Card",
                "status": "IN_PROGRESS",
                "previous_status": "TODO",
                "description": "Task",
            },
            "notification_service": fake,
            # monitoring_performer is the real post-dispatch phase that emits
            # card_dispatched; phase "dispatching" at notify time now means the
            # dispatch was HELD and is intentionally suppressed (076 QA #150).
            "phase": "monitoring_performer",
        }
    )

    await notify(state)

    assert len(fake.dispatched) == 1
    assert fake.dispatched[0].event_type == EventType.card_dispatched


@pytest.mark.asyncio
async def test_notification_latency_target_smoke() -> None:
    fake = FakeNotificationService()
    state = initial_state()
    state.update(
        {
            "current_card": {
                "title": "Card",
                "status": "IN_PROGRESS",
                "previous_status": "TODO",
                "description": "Task",
            },
            "notification_service": fake,
            "phase": "monitoring_agent",
        }
    )

    await notify(state)

    assert len(fake.dispatched) == 1
