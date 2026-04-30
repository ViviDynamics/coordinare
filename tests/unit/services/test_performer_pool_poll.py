"""Unit tests for PerformerPool polling, mark_*, and registration edge cases (spec 056)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coordinare.models.performer_endpoint import (
    PerformerCapabilities,
    PerformerEndpointConfig,
)
from coordinare.services.performer_pool import PerformerPool


def _persistent(id: str, port: int = 8001) -> PerformerEndpointConfig:
    return PerformerEndpointConfig(
        id=id,
        mode="persistent",
        roles=["implementing"],
        image="performer:full",
        endpoint=f"http://localhost:{port}",
    )


@pytest.fixture
def pool() -> PerformerPool:
    return PerformerPool(failure_threshold=3)


# ---------------------------------------------------------------------------
# Registration edge cases
# ---------------------------------------------------------------------------


def test_register_subprocess_raises(pool: PerformerPool) -> None:
    config = PerformerEndpointConfig(id="sub-1", mode="subprocess", roles=["r"])
    with pytest.raises(ValueError, match="subprocess"):
        pool.register(config, None)


def test_unregister_missing_id_is_noop(pool: PerformerPool) -> None:
    pool.unregister("nonexistent")  # must not raise


def test_list_all_returns_registrations_in_order(pool: PerformerPool) -> None:
    pool.register(_persistent("p1", 8001), None)
    pool.register(_persistent("p2", 8002), None)
    states = pool.list_all()
    assert [s.id for s in states] == ["p1", "p2"]


def test_get_state_returns_none_for_missing(pool: PerformerPool) -> None:
    assert pool.get_state("nonexistent") is None


# ---------------------------------------------------------------------------
# select_for edge cases
# ---------------------------------------------------------------------------


def test_select_for_skips_none_capabilities(pool: PerformerPool) -> None:
    pool.register(_persistent("p1"), None)
    state = pool._registrations["p1"]
    state.availability = "idle"
    state.capabilities = None  # unknown — must skip (SC-006)
    result = pool.select_for(role="r", backend="claude_code", required_flags=set())
    assert result is None


# ---------------------------------------------------------------------------
# mark_* no-ops for missing ids
# ---------------------------------------------------------------------------


def test_mark_busy_missing_id_is_noop(pool: PerformerPool) -> None:
    pool.mark_busy("nonexistent", "job-1")


def test_mark_idle_missing_id_is_noop(pool: PerformerPool) -> None:
    pool.mark_idle("nonexistent")


def test_mark_unreachable_missing_id_is_noop(pool: PerformerPool) -> None:
    pool.mark_unreachable("nonexistent")


def test_mark_recovered_missing_id_is_noop(pool: PerformerPool) -> None:
    pool.mark_recovered("nonexistent")


# ---------------------------------------------------------------------------
# poll_all / _poll_one — success paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_all_empty_pool_does_not_raise(pool: PerformerPool) -> None:
    await pool.poll_all()  # should be a no-op


@pytest.mark.asyncio
async def test_poll_one_success_resets_failures_and_updates_availability(
    pool: PerformerPool,
) -> None:
    svc = AsyncMock()
    svc.check_health = AsyncMock(return_value={"availability": "busy"})
    pool.register(_persistent("p1"), svc)
    state = pool._registrations["p1"]
    state.consecutive_failures = 2

    await pool.poll_all()

    assert state.consecutive_failures == 0
    assert state.availability == "busy"


@pytest.mark.asyncio
async def test_poll_one_success_updates_capabilities(pool: PerformerPool) -> None:
    svc = AsyncMock()
    svc.check_health = AsyncMock(
        return_value={
            "availability": "idle",
            "capabilities": {"backends": ["claude_code"], "tool_flags": ["git"]},
        }
    )
    pool.register(_persistent("p1"), svc)

    await pool.poll_all()

    state = pool._registrations["p1"]
    assert state.capabilities is not None
    assert "claude_code" in state.capabilities.backends


@pytest.mark.asyncio
async def test_poll_one_recovery_from_excluded_on_success(pool: PerformerPool) -> None:
    svc = AsyncMock()
    svc.check_health = AsyncMock(return_value={"availability": "idle"})
    pool.register(_persistent("p1"), svc)
    state = pool._registrations["p1"]
    state.excluded_until_recovery = True

    await pool.poll_all()

    assert state.excluded_until_recovery is False
    assert state.availability == "idle"


# ---------------------------------------------------------------------------
# poll_all / _poll_one — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_one_failure_increments_counter(pool: PerformerPool) -> None:
    svc = AsyncMock()
    svc.check_health = AsyncMock(side_effect=Exception("connection refused"))
    pool.register(_persistent("p1"), svc)

    await pool.poll_all()

    assert pool._registrations["p1"].consecutive_failures == 1


@pytest.mark.asyncio
async def test_poll_one_failure_at_threshold_excludes_performer(
    pool: PerformerPool,
) -> None:
    svc = AsyncMock()
    svc.check_health = AsyncMock(side_effect=Exception("timeout"))
    pool.register(_persistent("p1"), svc)
    state = pool._registrations["p1"]
    state.consecutive_failures = pool._failure_threshold - 1  # one short of threshold

    await pool.poll_all()

    assert state.excluded_until_recovery is True
    assert state.availability == "unreachable"


@pytest.mark.asyncio
async def test_poll_one_failure_clears_in_flight_job_on_exclusion(
    pool: PerformerPool,
) -> None:
    pool._failure_threshold = 1
    svc = AsyncMock()
    svc.check_health = AsyncMock(side_effect=Exception("down"))
    pool.register(_persistent("p1"), svc)
    pool._registrations["p1"].current_job_id = "job-xyz"

    await pool.poll_all()

    assert pool._registrations["p1"].current_job_id is None


@pytest.mark.asyncio
async def test_poll_one_skips_when_service_is_none(pool: PerformerPool) -> None:
    pool.register(_persistent("p1"), None)
    # _poll_one returns early without crashing when service is None
    await pool.poll_all()
    state = pool._registrations["p1"]
    assert state.consecutive_failures == 0  # unchanged


@pytest.mark.asyncio
async def test_poll_one_unknown_availability_does_not_update_state(
    pool: PerformerPool,
) -> None:
    svc = AsyncMock()
    svc.check_health = AsyncMock(return_value={"availability": "unknown"})
    pool.register(_persistent("p1"), svc)
    state = pool._registrations["p1"]
    state.availability = "idle"

    await pool.poll_all()

    # "unknown" is not in the valid set — availability must stay unchanged
    assert state.availability == "idle"
    assert state.consecutive_failures == 0


def test_select_for_skips_excluded_until_recovery(pool: PerformerPool) -> None:

    pool.register(_persistent("p1"), None)
    state = pool._registrations["p1"]
    state.availability = "idle"
    state.excluded_until_recovery = True
    state.capabilities = PerformerCapabilities(backends=["claude_code"], tool_flags=[])

    result = pool.select_for(role="implementing", backend="claude_code", required_flags=set())
    assert result is None
