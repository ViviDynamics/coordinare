"""Tests for the performer pool dashboard widget (spec 056, T040)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.dashboard import render_performer_pool_widget
from coordinare.models.performer_endpoint import (
    PerformerCapabilities,
    PerformerEndpointConfig,
)
from coordinare.services.performer_pool import PerformerPool


@pytest.fixture
def pool() -> PerformerPool:
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
        mode="ephemeral",
        roles=["writing"],
        image="performer:full",
    )


def test_render_empty_pool() -> None:
    """Rendering None pool returns empty widget state."""
    result = render_performer_pool_widget(None)

    assert result == {
        "performers": [],
        "total_registered": 0,
        "total_excluded": 0,
        "total_idle": 0,
        "total_busy": 0,
    }


def test_render_single_idle_performer(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
) -> None:
    """Rendering a single idle performer shows its state."""
    pool.register(perf1_config, None)

    state = pool._registrations["perf-1"]
    state.availability = "idle"
    state.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git", "node"]
    )
    state.last_status_at = datetime.now(UTC)

    result = render_performer_pool_widget(pool)

    assert result["total_registered"] == 1
    assert result["total_idle"] == 1
    assert result["total_busy"] == 0
    assert result["total_excluded"] == 0

    perf = result["performers"][0]
    assert perf["id"] == "perf-1"
    assert perf["mode"] == "persistent"
    assert perf["availability"] == "idle"
    assert perf["endpoint"] == "http://localhost:8001/"
    assert perf["current_job_id"] is None
    assert perf["consecutive_failures"] == 0
    assert perf["excluded_until_recovery"] is False
    assert perf["capabilities"]["backends"] == ["claude_code"]
    assert perf["capabilities"]["tool_flags"] == ["git", "node"]
    assert perf["last_status_at"] is not None


def test_render_busy_performer(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
) -> None:
    """Rendering a busy performer shows the current job."""
    pool.register(perf1_config, None)

    state = pool._registrations["perf-1"]
    state.availability = "busy"
    state.current_job_id = "job-123"
    state.capabilities = PerformerCapabilities(
        backends=["claude_code"], tool_flags=["git"]
    )

    result = render_performer_pool_widget(pool)

    assert result["total_busy"] == 1
    assert result["total_idle"] == 0

    perf = result["performers"][0]
    assert perf["availability"] == "busy"
    assert perf["current_job_id"] == "job-123"


def test_render_excluded_performer(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
) -> None:
    """Rendering an excluded performer shows excluded flag."""
    pool.register(perf1_config, None)

    state = pool._registrations["perf-1"]
    state.availability = "unreachable"
    state.excluded_until_recovery = True
    state.consecutive_failures = 5

    result = render_performer_pool_widget(pool)

    assert result["total_excluded"] == 1

    perf = result["performers"][0]
    assert perf["excluded_until_recovery"] is True
    assert perf["consecutive_failures"] == 5
    assert perf["availability"] == "unreachable"


def test_render_multiple_performers_with_mixed_states(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
    perf2_config: PerformerEndpointConfig,
) -> None:
    """Rendering multiple performers aggregates counts correctly."""
    pool.register(perf1_config, None)
    pool.register(perf2_config, None)

    state1 = pool._registrations["perf-1"]
    state1.availability = "idle"
    state1.capabilities = PerformerCapabilities(backends=["claude_code"], tool_flags=[])

    state2 = pool._registrations["perf-2"]
    state2.availability = "busy"
    state2.current_job_id = "job-456"
    state2.capabilities = PerformerCapabilities(backends=["codex"], tool_flags=[])

    result = render_performer_pool_widget(pool)

    assert result["total_registered"] == 2
    assert result["total_idle"] == 1
    assert result["total_busy"] == 1
    assert result["total_excluded"] == 0

    assert result["performers"][0]["id"] == "perf-1"
    assert result["performers"][1]["id"] == "perf-2"


def test_render_performer_without_endpoint(
    pool: PerformerPool,
    perf2_config: PerformerEndpointConfig,
) -> None:
    """Rendering an ephemeral performer shows None endpoint until started."""
    pool.register(perf2_config, None)

    state = pool._registrations["perf-2"]
    state.availability = "unknown"

    result = render_performer_pool_widget(pool)

    perf = result["performers"][0]
    assert perf["endpoint"] is None
    assert perf["availability"] == "unknown"


def test_render_performer_without_capabilities(
    pool: PerformerPool,
    perf1_config: PerformerEndpointConfig,
) -> None:
    """Rendering a performer without capabilities shows None."""
    pool.register(perf1_config, None)

    state = pool._registrations["perf-1"]
    state.availability = "starting"
    state.capabilities = None

    result = render_performer_pool_widget(pool)

    perf = result["performers"][0]
    assert perf["capabilities"] is None
    assert perf["availability"] == "starting"
