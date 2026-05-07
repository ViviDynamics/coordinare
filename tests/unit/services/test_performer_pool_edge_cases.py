"""Unit tests for performer pool edge cases (spec 056, T036).

Tests for duplicate registration, None capabilities in selection, and other
error conditions in the performer pool.
"""

from __future__ import annotations

import pytest

from coordinare.models.performer_endpoint import (
    PerformerCapabilities,
    PerformerEndpointConfig,
)
from coordinare.services.performer_pool import PerformerPool


@pytest.fixture
def pool() -> PerformerPool:
    """Create an empty pool for testing."""
    return PerformerPool(failure_threshold=5)


@pytest.fixture
def perf_config() -> PerformerEndpointConfig:
    """A standard performer configuration."""
    return PerformerEndpointConfig(
        id="perf-1",
        mode="persistent",
        roles=["implementing"],
        image="performer:full",
        endpoint="http://localhost:8001",
    )


def test_register_duplicate_performer_raises_valueerror(
    pool: PerformerPool,
    perf_config: PerformerEndpointConfig,
) -> None:
    """Registering a performer with an id that already exists raises ValueError."""
    # Register the first time
    pool.register(perf_config, None)
    assert perf_config.id in pool._registrations

    # Try to register again with the same id
    with pytest.raises(ValueError, match="already registered"):
        pool.register(perf_config, None)


def test_selection_skips_performer_with_none_capabilities(
    pool: PerformerPool,
    perf_config: PerformerEndpointConfig,
) -> None:
    """select_for skips performers with None capabilities (SC-006 prevention)."""
    pool.register(perf_config, None)

    # Mark performer idle but leave capabilities as None (simulating unknown state)
    state = pool._registrations[perf_config.id]
    state.availability = "idle"
    state.capabilities = None  # Explicitly None

    # Try to select for a role and backend
    result = pool.select_for(
        role="implementing",
        backend="claude_code",
        required_flags=set(),
    )

    # Should return None because no performers have capabilities
    assert result is None


def test_selection_with_none_capabilities_and_other_candidates(
    pool: PerformerPool,
    perf_config: PerformerEndpointConfig,
) -> None:
    """select_for skips performers with None capabilities and returns next match."""
    # Create two performers
    perf1_config = PerformerEndpointConfig(
        id="perf-1",
        mode="persistent",
        roles=["implementing"],
        image="performer:full",
        endpoint="http://localhost:8001",
    )
    perf2_config = PerformerEndpointConfig(
        id="perf-2",
        mode="persistent",
        roles=["implementing"],
        image="performer:full",
        endpoint="http://localhost:8002",
    )

    pool.register(perf1_config, None)
    pool.register(perf2_config, None)

    # Mark perf-1 idle with None capabilities (should be skipped)
    state1 = pool._registrations[perf1_config.id]
    state1.availability = "idle"
    state1.capabilities = None

    # Mark perf-2 idle with valid capabilities
    state2 = pool._registrations[perf2_config.id]
    state2.availability = "idle"
    state2.capabilities = PerformerCapabilities(
        backends=["claude_code"],
        tool_flags=["git"],
    )

    # Select should skip perf-1 and return perf-2
    result = pool.select_for(
        role="implementing",
        backend="claude_code",
        required_flags=set(),
    )

    assert result is not None
    assert result.id == "perf-2"
