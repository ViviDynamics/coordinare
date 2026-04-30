"""Unit tests for performer pool selection logic (spec 056, T031).

Tests that the pool returns candidates in deterministic order (by registration),
filters by idle availability, capability matching, and exclusion status.
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
def perf1_config() -> PerformerEndpointConfig:
    """First performer: supports claude_code + git, node."""
    return PerformerEndpointConfig(
        id="perf-1",
        mode="persistent",
        roles=["implementing", "debugging"],
        image="performer:full",
        endpoint="http://localhost:8001",
    )


@pytest.fixture
def perf2_config() -> PerformerEndpointConfig:
    """Second performer: supports codex + git."""
    return PerformerEndpointConfig(
        id="perf-2",
        mode="persistent",
        roles=["writing", "implementing"],
        image="performer:slim-codex",
        endpoint="http://localhost:8002",
    )


@pytest.fixture
def perf3_config() -> PerformerEndpointConfig:
    """Third performer: supports claude_code, codex."""
    return PerformerEndpointConfig(
        id="perf-3",
        mode="ephemeral",
        roles=["implementing"],
        image="performer:full",
    )


def test_selection_deterministic_by_registration_order(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
    perf2_config: PerformerEndpointConfig,
    perf3_config: PerformerEndpointConfig,
) -> None:
    """Selection returns candidates in registration order."""
    # Register in specific order
    pool.register(perf1_config, None)
    pool.register(perf2_config, None)
    pool.register(perf3_config, None)

    # Mark all idle with matching capabilities
    state1 = pool._registrations["perf-1"]
    state1.availability = "idle"
    state1.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git", "node"]
    )

    state2 = pool._registrations["perf-2"]
    state2.availability = "idle"
    state2.capabilities = PerformerCapabilities(
        backends=["codex"], tool_flags=["git"]
    )

    state3 = pool._registrations["perf-3"]
    state3.availability = "idle"
    state3.capabilities = PerformerCapabilities(
        backends=["claude_code", "codex"], tool_flags=["git"]
    )

    # Select for role needing claude_code + [git] (no tool restrictions in this test)
    candidates = pool.select_for(role="implementing", backend="claude_code", required_flags=set())

    # Should return perf-1 first (registered first), perf-3 second
    assert candidates is not None
    assert candidates.id == "perf-1"


def test_selection_skips_ineligible_candidates(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
    perf2_config: PerformerEndpointConfig,
) -> None:
    """Selection skips candidates with mismatched backends or missing tools."""
    pool.register(perf1_config, None)
    pool.register(perf2_config, None)

    state1 = pool._registrations["perf-1"]
    state1.availability = "idle"
    state1.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git", "node"]
    )

    state2 = pool._registrations["perf-2"]
    state2.availability = "idle"
    state2.capabilities = PerformerCapabilities(
        backends=["codex"], tool_flags=["git"]
    )

    # Select for codex + [git, node]
    # perf-1 has wrong backend (claude_code, not codex)
    # perf-2 has codex but missing node
    candidates = pool.select_for(role="writing", backend="codex", required_flags={"node"})

    # Neither candidate qualifies
    assert candidates is None


def test_selection_returns_first_idle_candidate(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
    perf2_config: PerformerEndpointConfig,
) -> None:
    """Selection returns the first idle candidate if multiple match."""
    pool.register(perf1_config, None)
    pool.register(perf2_config, None)

    state1 = pool._registrations["perf-1"]
    state1.availability = "busy"  # Not available
    state1.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"]
    )

    state2 = pool._registrations["perf-2"]
    state2.availability = "idle"
    state2.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"]
    )

    candidates = pool.select_for(role="implementing", backend="claude_code", required_flags=set())

    # Should skip busy perf-1 and return idle perf-2
    assert candidates is not None
    assert candidates.id == "perf-2"


def test_selection_skips_excluded_candidates(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
    perf2_config: PerformerEndpointConfig,
) -> None:
    """Selection skips candidates marked excluded_until_recovery."""
    pool.register(perf1_config, None)
    pool.register(perf2_config, None)

    state1 = pool._registrations["perf-1"]
    state1.availability = "idle"
    state1.excluded_until_recovery = True
    state1.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"]
    )

    state2 = pool._registrations["perf-2"]
    state2.availability = "idle"
    state2.excluded_until_recovery = False
    state2.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"]
    )

    candidates = pool.select_for(role="implementing", backend="claude_code", required_flags=set())

    # Should skip excluded perf-1 and return perf-2
    assert candidates is not None
    assert candidates.id == "perf-2"


def test_selection_ignores_unreachable_candidates(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
    perf2_config: PerformerEndpointConfig,
) -> None:
    """Selection skips unreachable performers."""
    pool.register(perf1_config, None)
    pool.register(perf2_config, None)

    state1 = pool._registrations["perf-1"]
    state1.availability = "unreachable"
    state1.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"]
    )

    state2 = pool._registrations["perf-2"]
    state2.availability = "idle"
    state2.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"]
    )

    candidates = pool.select_for(role="implementing", backend="claude_code", required_flags=set())

    # Should skip unreachable perf-1 and return idle perf-2
    assert candidates is not None
    assert candidates.id == "perf-2"


def test_selection_returns_none_when_no_candidates(
    pool: PerformerPool,
) -> None:
    """Selection returns None when no candidates match."""
    result = pool.select_for(role="implementing", backend="claude_code", required_flags=set())
    assert result is None


def test_selection_requires_all_flags(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
) -> None:
    """Selection validates that candidate has ALL required flags."""
    pool.register(perf1_config, None)

    state = pool._registrations["perf-1"]
    state.availability = "idle"
    state.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git", "node"]
    )

    # Require git + node + python
    candidates = pool.select_for(
        role="implementing",
        backend="claude_code",
        required_flags={"git", "node", "python"},
    )

    # Should not return (missing python)
    assert candidates is None

    # Require git + node (present)
    candidates = pool.select_for(
        role="implementing",
        backend="claude_code",
        required_flags={"git", "node"},
    )

    # Should return perf-1
    assert candidates is not None
    assert candidates.id == "perf-1"


def test_selection_empty_required_flags(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
) -> None:
    """Selection succeeds when required_flags is empty."""
    pool.register(perf1_config, None)

    state = pool._registrations["perf-1"]
    state.availability = "idle"
    state.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git", "node"]
    )

    candidates = pool.select_for(
        role="implementing",
        backend="claude_code",
        required_flags=set(),
    )

    assert candidates is not None
    assert candidates.id == "perf-1"
