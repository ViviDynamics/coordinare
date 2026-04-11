"""Unit tests for daemon system alert hooks (006 US2, T021).

Tests:
- daemon_restart dispatch on start()
- prolonged_idle dispatch after threshold
- dedup_key set for prolonged_idle
- last_activity_at resets when phase changes from idle
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.models.notification import EventType
from tests.utils.fake_notification import FakeNotificationService


class _IdleGraph:
    """Graph that always returns idle phase."""
    async def ainvoke(self, state):
        state["phase"] = "idle"
        return state


class _ActiveGraph:
    """Graph that returns monitoring_agent on first call, idle on subsequent."""
    def __init__(self) -> None:
        self._call_count = 0

    async def ainvoke(self, state):
        self._call_count += 1
        if self._call_count == 1:
            state["phase"] = "monitoring_agent"
        else:
            state["phase"] = "idle"
        return state


@pytest.mark.asyncio
async def test_daemon_restart_notification_dispatched() -> None:
    """Daemon start() dispatches daemon_restart event."""
    fake = FakeNotificationService()
    daemon = CoordinareDaemon(
        _IdleGraph(),
        max_cycles=1,
        sleep_func=AsyncMock(),
        idle_threshold_seconds=9999,  # Don't trigger idle
    )
    daemon.state["notification_service"] = fake

    await daemon.start()

    restart_events = [e for e in fake.dispatched if e.event_type == EventType.daemon_restart]
    assert len(restart_events) == 1
    assert restart_events[0].source == "daemon"
    assert "Coordinare restarted" in restart_events[0].payload["summary"]


@pytest.mark.asyncio
async def test_prolonged_idle_dispatched_after_threshold() -> None:
    """Prolonged idle event fires when idle duration exceeds threshold."""
    fake = FakeNotificationService()
    daemon = CoordinareDaemon(
        _IdleGraph(),
        max_cycles=2,
        sleep_func=AsyncMock(),
        idle_threshold_seconds=0,  # Immediately trigger idle
    )
    daemon.state["notification_service"] = fake

    await daemon.start()

    idle_events = [e for e in fake.dispatched if e.event_type == EventType.prolonged_idle]
    assert len(idle_events) >= 1
    assert idle_events[0].dedup_key == "prolonged_idle"
    assert idle_events[0].source == "daemon"


@pytest.mark.asyncio
async def test_prolonged_idle_has_dedup_key() -> None:
    """The prolonged_idle event uses dedup_key to prevent repeats."""
    fake = FakeNotificationService()
    daemon = CoordinareDaemon(
        _IdleGraph(),
        max_cycles=1,
        sleep_func=AsyncMock(),
        idle_threshold_seconds=0,
    )
    daemon.state["notification_service"] = fake

    await daemon.start()

    idle_events = [e for e in fake.dispatched if e.event_type == EventType.prolonged_idle]
    assert all(e.dedup_key == "prolonged_idle" for e in idle_events)


@pytest.mark.asyncio
async def test_last_activity_resets_when_phase_changes_from_idle() -> None:
    """When phase is not idle, prolonged_idle timer resets."""
    fake = FakeNotificationService()
    # ActiveGraph returns monitoring_agent on first call, idle on second
    daemon = CoordinareDaemon(
        _ActiveGraph(),
        max_cycles=2,
        sleep_func=AsyncMock(),
        idle_threshold_seconds=9999,  # Very high threshold
    )
    daemon.state["notification_service"] = fake

    await daemon.start()

    # The first cycle is non-idle (monitoring_agent) which resets last_activity_at.
    # The second cycle is idle but hasn't been idle long enough.
    idle_events = [e for e in fake.dispatched if e.event_type == EventType.prolonged_idle]
    assert len(idle_events) == 0


@pytest.mark.asyncio
async def test_no_notification_when_service_missing() -> None:
    """If notification_service is not in state, no errors raised."""
    daemon = CoordinareDaemon(
        _IdleGraph(),
        max_cycles=1,
        sleep_func=AsyncMock(),
    )
    # No notification_service in state

    # Should complete without errors
    await daemon.start()
