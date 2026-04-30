"""Unit tests for performer pool concurrency safety (spec 056, T034).

Tests that per-registration asyncio.Lock prevents races between the dispatch
path and the status poll path during busy/idle transitions.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from coordinare.models.performer_endpoint import (
    PerformerCapabilities,
    PerformerEndpointConfig,
)
from coordinare.services.performer_pool import PerformerPool


@pytest.fixture
def pool() -> PerformerPool:
    return PerformerPool(failure_threshold=5)


@pytest.fixture
def perf_config() -> PerformerEndpointConfig:
    return PerformerEndpointConfig(
        id="perf-1",
        mode="persistent",
        roles=["implementing"],
        image="performer:full",
        endpoint="http://localhost:8001",
    )


@pytest.mark.asyncio
async def test_per_registration_lock_exists(
    pool: PerformerPool,
    perf_config: PerformerEndpointConfig,
) -> None:
    """Each registration has its own asyncio.Lock."""
    pool.register(perf_config, None)

    # Lock should be available via _locks dict
    assert hasattr(pool, "_locks")
    assert "perf-1" in pool._locks
    assert isinstance(pool._locks["perf-1"], asyncio.Lock)


@pytest.mark.asyncio
async def test_poll_one_uses_lock_for_failure_counter(
    pool: PerformerPool,
    perf_config: PerformerEndpointConfig,
) -> None:
    """Concurrent _poll_one calls increment consecutive_failures via the pool's own lock."""
    svc = AsyncMock()
    svc.check_health = AsyncMock(side_effect=Exception("timeout"))
    pool.register(perf_config, svc)

    # Run two concurrent failing polls; the lock inside _poll_one serialises
    # the counter increments so we end up with exactly 2.
    await asyncio.gather(pool._poll_one("perf-1"), pool._poll_one("perf-1"))

    state = pool._registrations["perf-1"]
    assert state.consecutive_failures == 2


@pytest.mark.asyncio
async def test_lock_does_not_block_selection(
    pool: PerformerPool,
    perf_config: PerformerEndpointConfig,
) -> None:
    """Selection (read-only) doesn't acquire the lock; it's always non-blocking."""
    pool.register(perf_config, None)
    state = pool._registrations["perf-1"]
    state.availability = "idle"
    state.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"]
    )

    lock = pool._locks["perf-1"]
    selection_succeeded = False

    async def hold_lock():
        async with lock:
            await asyncio.sleep(0.1)  # Hold lock for 100ms

    async def try_select():
        nonlocal selection_succeeded
        # Selection should NOT block waiting for the lock
        result = pool.select_for(
            role="implementing", backend="claude_code", required_flags=set()
        )
        selection_succeeded = result is not None

    # Run selection while lock is held — should succeed immediately
    await asyncio.gather(hold_lock(), try_select())
    assert selection_succeeded


def test_unregister_cleans_up_lock(
    pool: PerformerPool,
    perf_config: PerformerEndpointConfig,
) -> None:
    """unregister() removes the performer's lock."""
    pool.register(perf_config, None)
    assert "perf-1" in pool._locks

    pool.unregister("perf-1")

    assert "perf-1" not in pool._locks
    assert "perf-1" not in pool._registrations
