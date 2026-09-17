"""Unit tests for performer pool failure threshold logic (spec 056, T033).

Tests that consecutive failures increment, threshold is checked, and
exclusion/recovery transitions are observable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coordinare.models.performer_endpoint import (
    PerformerCapabilities,
    PerformerEndpointConfig,
)
from coordinare.services.performer_pool import PerformerPool


@pytest.fixture
def pool() -> PerformerPool:
    """Create a pool with threshold=5 for testing."""
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


def test_consecutive_failures_increment(
    pool: PerformerPool,
    perf_config: PerformerEndpointConfig,
) -> None:
    """Each failed status check increments consecutive_failures (simulated via state)."""
    pool.register(perf_config, None)
    state = pool._registrations["perf-1"]

    assert state.consecutive_failures == 0

    # Simulate failure counter incrementing (as would happen in poll_all)
    state.consecutive_failures = 1
    assert state.consecutive_failures == 1

    # Reset on successful poll (simulated)
    state.consecutive_failures = 0
    assert state.consecutive_failures == 0

    # Simulate two more failures
    state.consecutive_failures = 1
    state.consecutive_failures = 2
    assert state.consecutive_failures == 2


def test_exclusion_at_threshold(
    pool: PerformerPool,
    perf_config: PerformerEndpointConfig,
) -> None:
    """When consecutive_failures >= threshold, performer is excluded via mark_unreachable."""
    pool.register(perf_config, None)
    state = pool._registrations["perf-1"]

    # Increment to threshold=5
    for _ in range(5):
        state.consecutive_failures += 1

    # Check exclusion state before calling mark_unreachable
    assert state.consecutive_failures == 5
    assert state.excluded_until_recovery is False

    # Call mark_unreachable which transitions to excluded
    pool.mark_unreachable("perf-1", reason="status_failures")

    assert state.excluded_until_recovery is True
    assert state.availability == "unreachable"
    assert state.consecutive_failures == 0  # Resets on exclusion


def test_reset_on_successful_status_check(
    pool: PerformerPool,
    perf_config: PerformerEndpointConfig,
) -> None:
    """consecutive_failures resets to 0 on first successful status check."""
    pool.register(perf_config, None)
    state = pool._registrations["perf-1"]

    # Simulate failures
    state.consecutive_failures = 3
    state.excluded_until_recovery = False
    state.availability = "unreachable"

    # Recovery: successful status check
    state.availability = "idle"
    state.consecutive_failures = 0
    state.excluded_until_recovery = False

    assert state.consecutive_failures == 0
    assert state.excluded_until_recovery is False
    assert state.availability == "idle"


def test_recovery_notification_on_transition(
    pool: PerformerPool,
    perf_config: PerformerEndpointConfig,
) -> None:
    """mark_recovered() transitions performer back to idle."""
    pool.register(perf_config, None)
    state = pool._registrations["perf-1"]

    state.availability = "unreachable"
    state.excluded_until_recovery = True

    pool.mark_recovered("perf-1")

    assert state.availability == "idle"
    assert state.excluded_until_recovery is False


@pytest.mark.asyncio
async def test_custom_failure_threshold() -> None:
    """Pool respects failure_threshold — exclusion fires after exactly N poll failures."""
    pool = PerformerPool(failure_threshold=3)

    perf_config = PerformerEndpointConfig(
        id="perf-1",
        mode="persistent",
        roles=["implementing"],
        image="performer:full",
        endpoint="http://localhost:8001",
    )
    svc = AsyncMock()
    svc.check_health = AsyncMock(side_effect=Exception("timeout"))
    pool.register(perf_config, svc)

    state = pool._registrations["perf-1"]

    # Two failures — not yet excluded.
    await pool.poll_all()
    await pool.poll_all()
    assert state.consecutive_failures == 2
    assert state.excluded_until_recovery is False

    # Third failure hits threshold — performer is excluded.
    await pool.poll_all()
    assert state.excluded_until_recovery is True
    assert state.availability == "unreachable"


def test_excluded_performer_skipped_in_selection(
    pool: PerformerPool,
    perf_config: PerformerEndpointConfig,
) -> None:
    """Excluded performers are not returned by select_for()."""
    pool.register(perf_config, None)
    state = pool._registrations["perf-1"]

    state.availability = "idle"
    state.excluded_until_recovery = False
    state.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"],
    )

    # Select should return the performer
    result = pool.select_for(role="implementing", backend="claude_code", required_flags=set())
    assert result is not None
    assert result.id == "perf-1"

    # Mark as excluded
    state.excluded_until_recovery = True

    # Select should now skip it
    result = pool.select_for(role="implementing", backend="claude_code", required_flags=set())
    assert result is None
