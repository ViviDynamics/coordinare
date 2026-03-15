"""Unit tests for daemon polling with webhook trigger (015 T038)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.daemon import CoordinareDaemon


def _make_daemon(
    poll_interval: int = 30,
    webhook_trigger: asyncio.Event | None = None,
) -> CoordinareDaemon:
    graph = MagicMock()
    graph.ainvoke = AsyncMock(side_effect=StopIteration)  # prevent infinite loop
    return CoordinareDaemon(
        graph=graph,
        poll_interval_seconds=poll_interval,
        webhook_trigger=webhook_trigger,
    )


@pytest.mark.asyncio
async def test_poll_zero_blocks_on_trigger_event() -> None:
    """With poll=0, _wait_for_next_cycle should unblock only when trigger is set."""
    trigger = asyncio.Event()
    daemon = _make_daemon(poll_interval=0, webhook_trigger=trigger)

    result: list[str] = []

    async def waiter():
        await daemon._wait_for_next_cycle()
        result.append("done")

    task = asyncio.ensure_future(waiter())
    await asyncio.sleep(0)  # let the waiter start and block
    assert not result  # must still be blocked

    trigger.set()
    await asyncio.sleep(0)
    await task
    assert result == ["done"]


@pytest.mark.asyncio
async def test_poll_nonzero_wakes_early_when_trigger_set() -> None:
    """With poll>0, _wait_for_next_cycle should unblock immediately when trigger fires."""
    trigger = asyncio.Event()
    daemon = _make_daemon(poll_interval=3600, webhook_trigger=trigger)  # very long timeout

    done = asyncio.Event()

    async def waiter():
        await daemon._wait_for_next_cycle()
        done.set()

    task = asyncio.ensure_future(waiter())
    await asyncio.sleep(0)
    assert not done.is_set()

    trigger.set()
    await asyncio.sleep(0.05)
    assert done.is_set()
    await task  # should already be done


@pytest.mark.asyncio
async def test_trigger_is_cleared_after_wake() -> None:
    """After _wait_for_next_cycle returns, the trigger event must be cleared."""
    trigger = asyncio.Event()
    daemon = _make_daemon(poll_interval=0, webhook_trigger=trigger)

    trigger.set()
    await daemon._wait_for_next_cycle()

    assert not trigger.is_set()


@pytest.mark.asyncio
async def test_poll_nonzero_trigger_cleared_after_wake() -> None:
    trigger = asyncio.Event()
    daemon = _make_daemon(poll_interval=3600, webhook_trigger=trigger)
    trigger.set()

    await daemon._wait_for_next_cycle()
    assert not trigger.is_set()


@pytest.mark.asyncio
async def test_stop_event_unblocks_poll_zero_loop() -> None:
    """With poll=0, the stop event should also unblock _wait_for_next_cycle."""
    trigger = asyncio.Event()
    daemon = _make_daemon(poll_interval=0, webhook_trigger=trigger)

    done = asyncio.Event()

    async def waiter():
        await daemon._wait_for_next_cycle()
        done.set()

    task = asyncio.ensure_future(waiter())
    await asyncio.sleep(0)
    assert not done.is_set()

    # Fire the stop event instead of the trigger
    daemon._stop_event.set()
    await asyncio.sleep(0.05)
    assert done.is_set()
    await task


@pytest.mark.asyncio
async def test_poll_nonzero_trigger_not_cleared_when_sleep_wins() -> None:
    """If the sleep expires before a webhook fires, the trigger must not be cleared.

    Regression guard for the race: a webhook arriving just as the poll
    interval completes would previously be silently discarded.
    """
    trigger = asyncio.Event()
    # Use a very short poll so the sleep always wins (trigger never set before wake)
    daemon = _make_daemon(poll_interval=1, webhook_trigger=trigger)

    # Override _sleep to complete immediately without setting the trigger
    from unittest.mock import AsyncMock
    daemon._sleep = AsyncMock()

    await daemon._wait_for_next_cycle()

    # Trigger was never set, so it should still be unset after the sleep wins
    assert not trigger.is_set()

    # Now simulate a webhook arriving AFTER the poll woke up via sleep
    trigger.set()
    # A second call should see the trigger and clear it
    await daemon._wait_for_next_cycle()
    assert not trigger.is_set()


@pytest.mark.asyncio
async def test_daemon_accepts_external_webhook_trigger() -> None:
    """CoordinareDaemon should use the webhook_trigger passed at construction."""
    external_trigger = asyncio.Event()
    daemon = _make_daemon(poll_interval=0, webhook_trigger=external_trigger)
    assert daemon._webhook_trigger is external_trigger


@pytest.mark.asyncio
async def test_daemon_creates_default_trigger_when_none_passed() -> None:
    """CoordinareDaemon creates its own asyncio.Event when webhook_trigger=None."""
    daemon = _make_daemon(poll_interval=30, webhook_trigger=None)
    assert isinstance(daemon._webhook_trigger, asyncio.Event)
