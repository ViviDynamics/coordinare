"""Unit tests for the in-flight guard (spec 076 T035, T032).

Covers ``check_inflight`` happy + refuse paths, the empty / missing
session_id cases, and the probe-failure fallback.  Mutex behaviour is
exercised separately in ``test_dispatch_guard_mutex.py``.
"""
from __future__ import annotations

import pytest

from coordinare.services.dispatch_guard import (
    InFlightGuardResult,
    check_inflight,
)


class _StubService:
    """Service stub implementing only ``has_live_session``."""

    def __init__(self, *, returns: bool | None = None, raises: Exception | None = None) -> None:
        self._returns = returns
        self._raises = raises
        self.calls: list[str] = []

    def has_live_session(self, session_id: str) -> bool:
        self.calls.append(session_id)
        if self._raises is not None:
            raise self._raises
        return bool(self._returns)


@pytest.mark.asyncio
async def test_check_inflight_empty_state_proceeds() -> None:
    """No agent_dispatch set → proceed; no service call made."""
    state: dict = {}
    result = await check_inflight(state, "PVTI_X", "implementing")
    assert isinstance(result, InFlightGuardResult)
    assert result.advice == "proceed"
    assert result.is_in_flight is False
    assert result.session_id is None


@pytest.mark.asyncio
async def test_check_inflight_no_session_id_proceeds() -> None:
    """agent_dispatch exists but session_id is empty/missing → proceed."""
    state: dict = {"agent_dispatch": {"session_id": ""}}
    result = await check_inflight(state, "PVTI_X", "implementing")
    assert result.advice == "proceed"
    assert result.is_in_flight is False


@pytest.mark.asyncio
async def test_check_inflight_session_alive_refuses() -> None:
    """FR-001: when service reports the session is alive, refuse dispatch."""
    service = _StubService(returns=True)
    state: dict = {
        "agent_dispatch": {"session_id": "uuid-abc"},
        "performer_services": {"implementing": service},
    }
    result = await check_inflight(state, "PVTI_X", "implementing")
    assert result.advice == "refuse"
    assert result.is_in_flight is True
    assert result.session_id == "uuid-abc"
    assert service.calls == ["uuid-abc"]


@pytest.mark.asyncio
async def test_check_inflight_session_dead_proceeds() -> None:
    """Service reports session dead → proceed (the stale-session path is
    handled elsewhere by handle_potentially_stale_session)."""
    service = _StubService(returns=False)
    state: dict = {
        "agent_dispatch": {"session_id": "uuid-stale"},
        "performer_services": {"implementing": service},
    }
    result = await check_inflight(state, "PVTI_X", "implementing")
    assert result.advice == "proceed"
    assert result.is_in_flight is False
    assert result.session_id == "uuid-stale"


@pytest.mark.asyncio
async def test_check_inflight_missing_service_proceeds() -> None:
    """No service for this stage → cannot prove liveness → proceed.

    This preserves backward compatibility for snapshot-restored sessions
    where performer_services is repopulated after the first cycle.
    """
    state: dict = {
        "agent_dispatch": {"session_id": "uuid-abc"},
        "performer_services": {},  # implementing key missing
    }
    result = await check_inflight(state, "PVTI_X", "implementing")
    assert result.advice == "proceed"


@pytest.mark.asyncio
async def test_check_inflight_service_without_has_live_session_proceeds() -> None:
    """Service that doesn't implement has_live_session (e.g. test stub)
    → cannot prove liveness → proceed."""

    class _NoLiveCheck:
        pass

    state: dict = {
        "agent_dispatch": {"session_id": "uuid-abc"},
        "performer_services": {"implementing": _NoLiveCheck()},
    }
    result = await check_inflight(state, "PVTI_X", "implementing")
    assert result.advice == "proceed"


@pytest.mark.asyncio
async def test_check_inflight_probe_failure_proceeds(caplog) -> None:
    """If has_live_session raises, log a warning and proceed (fail-open
    style, FR-012 spirit).  We never refuse on a probe failure."""
    service = _StubService(raises=RuntimeError("probe blew up"))
    state: dict = {
        "agent_dispatch": {"session_id": "uuid-abc"},
        "performer_services": {"implementing": service},
    }
    result = await check_inflight(state, "PVTI_X", "implementing")
    assert result.advice == "proceed"
    assert result.is_in_flight is False
    assert service.calls == ["uuid-abc"]
