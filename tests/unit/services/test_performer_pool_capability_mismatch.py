"""Unit tests for capability mismatch filtering in performer pool (spec 056, T045).

When a performer's reported capabilities lack a required backend or tool flag,
``select_for()`` skips it at selection time (SC-006). An explicit state
transition to ``unreachable`` with reason ``capability_mismatch`` can be
triggered by the operator or coordinare via ``mark_unreachable()``; the pool
does not do this automatically on capability mismatch alone.
"""

from __future__ import annotations

import pytest

from coordinare.models.performer_endpoint import (
    PerformerCapabilities,
    PerformerEndpointConfig,
)
from coordinare.services.performer_pool import PerformerPool


@pytest.mark.asyncio
async def test_capability_mismatch_browser_missing() -> None:
    """Test that a role requiring `browser` is marked unreachable when performer lacks it."""
    pool = PerformerPool()

    # Create a configuration for a performer without browser capability
    config = PerformerEndpointConfig(
        id="test-performer-1",
        mode="ephemeral",
        roles=["qa"],
        image="test:latest",
    )

    # Register the performer
    pool.register(config, service=None)

    # Simulate that poll_all has updated the capabilities without browser
    state = pool.get_state("test-performer-1")
    assert state is not None
    state.capabilities = PerformerCapabilities(
        backends=["claude_code"],
        tool_flags=["git", "python", "lint"],  # No browser
    )
    state.availability = "idle"

    # Now try to select for a role that requires browser
    selected = pool.select_for(
        role="qa",
        backend="claude_code",
        required_flags={"browser"},  # Requires browser!
    )

    # Selection should fail due to missing browser capability
    assert selected is None


@pytest.mark.asyncio
async def test_capability_mismatch_backend_missing() -> None:
    """Test that a role requiring a specific backend is marked unreachable when performer lacks it."""
    pool = PerformerPool()

    config = PerformerEndpointConfig(
        id="test-performer-2",
        mode="ephemeral",
        roles=["dev"],
        image="test:latest",
    )

    pool.register(config, service=None)

    state = pool.get_state("test-performer-2")
    assert state is not None
    state.capabilities = PerformerCapabilities(
        backends=["opencode"],  # Only opencode
        tool_flags=["git", "python"],
    )
    state.availability = "idle"

    # Try to select for a role requiring cursor backend
    selected = pool.select_for(
        role="dev",
        backend="cursor",  # Requires cursor, but performer only has opencode
        required_flags=set(),
    )

    assert selected is None


@pytest.mark.asyncio
async def test_capability_mismatch_mark_unreachable_reason() -> None:
    """Test that mark_unreachable accepts a reason code for capability_mismatch."""
    pool = PerformerPool()

    config = PerformerEndpointConfig(
        id="test-performer-3",
        mode="ephemeral",
        roles=["qa"],
        image="test:latest",
    )

    pool.register(config, service=None)

    # Mark the performer unreachable with capability_mismatch reason
    pool.mark_unreachable("test-performer-3", reason="capability_mismatch")

    state = pool.get_state("test-performer-3")
    assert state is not None
    assert state.availability == "unreachable"
    assert state.excluded_until_recovery is True


@pytest.mark.asyncio
async def test_capability_match_all_required_flags() -> None:
    """Test that selection succeeds when all required flags are present."""
    pool = PerformerPool()

    config = PerformerEndpointConfig(
        id="test-performer-4",
        mode="ephemeral",
        roles=["qa"],
        image="test:latest",
    )

    pool.register(config, service=None)

    state = pool.get_state("test-performer-4")
    assert state is not None
    state.capabilities = PerformerCapabilities(
        backends=["claude_code"],
        tool_flags=["git", "python", "browser", "lint", "test_runner"],
    )
    state.availability = "idle"

    # Select with required flags that are all present
    selected = pool.select_for(
        role="qa",
        backend="claude_code",
        required_flags={"browser", "test_runner"},
    )

    assert selected is not None
    assert selected.id == "test-performer-4"


@pytest.mark.asyncio
async def test_capability_mismatch_partially_missing_flags() -> None:
    """Test that selection fails when one of multiple required flags is missing."""
    pool = PerformerPool()

    config = PerformerEndpointConfig(
        id="test-performer-5",
        mode="ephemeral",
        roles=["qa"],
        image="test:latest",
    )

    pool.register(config, service=None)

    state = pool.get_state("test-performer-5")
    assert state is not None
    state.capabilities = PerformerCapabilities(
        backends=["claude_code"],
        tool_flags=["git", "python", "browser"],  # Has browser but not test_runner
    )
    state.availability = "idle"

    # Select requiring both browser and test_runner
    selected = pool.select_for(
        role="qa",
        backend="claude_code",
        required_flags={"browser", "test_runner"},
    )

    # Should fail because test_runner is missing
    assert selected is None


@pytest.mark.asyncio
async def test_capability_match_no_flags_required() -> None:
    """Test that selection succeeds for empty required flags when backend matches."""
    pool = PerformerPool()

    config = PerformerEndpointConfig(
        id="test-performer-6",
        mode="ephemeral",
        roles=["simple"],
        image="test:latest",
    )

    pool.register(config, service=None)

    state = pool.get_state("test-performer-6")
    assert state is not None
    state.capabilities = PerformerCapabilities(
        backends=["opencode"],
        tool_flags=["git", "python"],
    )
    state.availability = "idle"

    # Select with no required flags
    selected = pool.select_for(
        role="simple",
        backend="opencode",
        required_flags=set(),
    )

    assert selected is not None
    assert selected.id == "test-performer-6"
