"""Unit tests for performer pool fallover (spec 056, T032).

Tests that the pool handles busy performers by trying the next candidate,
defers dispatch when all candidates are busy, and fails in-flight jobs
when a performer transitions to unreachable.
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
    return PerformerEndpointConfig(
        id="perf-1",
        mode="persistent",
        roles=["implementing"],
        image="performer:full",
        endpoint="http://localhost:8001",
    )


@pytest.fixture
def perf2_config() -> PerformerEndpointConfig:
    return PerformerEndpointConfig(
        id="perf-2",
        mode="persistent",
        roles=["implementing"],
        image="performer:full",
        endpoint="http://localhost:8002",
    )


def test_select_skips_first_busy_tries_second(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
    perf2_config: PerformerEndpointConfig,
) -> None:
    """Fallover: skip busy candidate and return the next idle one."""
    pool.register(perf1_config, None)
    pool.register(perf2_config, None)

    state1 = pool._registrations["perf-1"]
    state1.availability = "busy"
    state1.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"],
    )

    state2 = pool._registrations["perf-2"]
    state2.availability = "idle"
    state2.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"],
    )

    candidates = pool.select_for(role="implementing", backend="claude_code", required_flags=set())

    assert candidates is not None
    assert candidates.id == "perf-2"


def test_select_returns_none_when_all_busy(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
    perf2_config: PerformerEndpointConfig,
) -> None:
    """Deferral: return None when all candidates are busy."""
    pool.register(perf1_config, None)
    pool.register(perf2_config, None)

    state1 = pool._registrations["perf-1"]
    state1.availability = "busy"
    state1.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"],
    )

    state2 = pool._registrations["perf-2"]
    state2.availability = "busy"
    state2.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"],
    )

    candidates = pool.select_for(role="implementing", backend="claude_code", required_flags=set())

    # No idle candidates — dispatch deferred
    assert candidates is None


def test_mark_busy_sets_current_job_id(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
) -> None:
    """mark_busy() sets current_job_id on the registration."""
    pool.register(perf1_config, None)

    state = pool._registrations["perf-1"]
    state.availability = "idle"

    pool.mark_busy("perf-1", "job-123")

    assert state.availability == "busy"
    assert state.current_job_id == "job-123"


def test_mark_idle_clears_current_job_id(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
) -> None:
    """mark_idle() clears current_job_id and resets availability."""
    pool.register(perf1_config, None)

    state = pool._registrations["perf-1"]
    state.availability = "busy"
    state.current_job_id = "job-123"

    pool.mark_idle("perf-1")

    assert state.availability == "idle"
    assert state.current_job_id is None


def test_busy_to_unreachable_transition(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
) -> None:
    """When a busy performer transitions to unreachable, mark_unreachable clears the job."""
    pool.register(perf1_config, None)

    state = pool._registrations["perf-1"]
    state.availability = "busy"
    state.current_job_id = "job-123"

    # Transition to unreachable (e.g., readiness timeout or consecutive failures)
    pool.mark_unreachable("perf-1", reason="status_failures")

    assert state.availability == "unreachable"
    # Job in flight should be cleared when performer becomes unreachable
    assert state.current_job_id is None
