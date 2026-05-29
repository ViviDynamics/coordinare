"""In-flight guard contract test (spec 076 T037).

Asserts the 3 log events from ``contracts/in-flight-guard.md`` are
emitted with their required fields under each triggering branch.
"""
from __future__ import annotations

import asyncio

import pytest

from coordinare.services import dispatch_guard as dg_mod
from coordinare.services.dispatch_guard import check_inflight, dispatch_mutex


class _LiveService:
    def has_live_session(self, session_id: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_in_flight_guard_tripped_event_shape(monkeypatch) -> None:
    """When the guard refuses dispatch, the warning event MUST include
    card_id, performer_stage, session_id, and service_has_live_session=True."""
    warn_calls: list[tuple[str, dict]] = []
    original_warn = dg_mod.logger.warning

    def _capture(event, **kwargs):  # type: ignore[no-untyped-def]
        warn_calls.append((event, kwargs))
        return original_warn(event, **kwargs)

    monkeypatch.setattr(dg_mod.logger, "warning", _capture)

    state = {
        "agent_dispatch": {"session_id": "session-uuid-xyz"},
        "performer_services": {"implementing": _LiveService()},
    }
    result = await check_inflight(state, "PVTI_CONTRACT", "implementing")
    assert result.advice == "refuse"

    tripped = [c for c in warn_calls if c[0] == "dispatch_performer.in_flight_guard_tripped"]
    assert len(tripped) == 1
    fields = tripped[0][1]
    assert fields["card_id"] == "PVTI_CONTRACT"
    assert fields["performer_stage"] == "implementing"
    assert fields["session_id"] == "session-uuid-xyz"
    assert fields["service_has_live_session"] is True


@pytest.mark.asyncio
async def test_in_flight_guard_probe_failed_event_shape(monkeypatch) -> None:
    """If has_live_session raises, the probe-failure warning event MUST
    include card_id, performer_stage, session_id, and error."""

    class _BrokenService:
        def has_live_session(self, session_id: str) -> bool:  # pragma: no cover
            raise RuntimeError("probe blew up")

    warn_calls: list[tuple[str, dict]] = []
    original_warn = dg_mod.logger.warning

    def _capture(event, **kwargs):  # type: ignore[no-untyped-def]
        warn_calls.append((event, kwargs))
        return original_warn(event, **kwargs)

    monkeypatch.setattr(dg_mod.logger, "warning", _capture)

    state = {
        "agent_dispatch": {"session_id": "session-uuid-abc"},
        "performer_services": {"implementing": _BrokenService()},
    }
    result = await check_inflight(state, "PVTI_PROBE", "implementing")
    # Probe-failure path proceeds (fail-open) — never refuses
    assert result.advice == "proceed"

    probe_failed = [c for c in warn_calls if c[0] == "dispatch_performer.in_flight_guard_probe_failed"]
    assert len(probe_failed) == 1
    fields = probe_failed[0][1]
    assert fields["card_id"] == "PVTI_PROBE"
    assert fields["performer_stage"] == "implementing"
    assert fields["session_id"] == "session-uuid-abc"
    assert "probe blew up" in fields["error"]


@pytest.mark.asyncio
async def test_mutex_waited_event_shape(monkeypatch) -> None:
    """When the mutex was contested, the info event MUST carry
    card_id, performer_stage, and a non-negative wait_ms."""
    info_calls: list[tuple[str, dict]] = []
    original_info = dg_mod.logger.info

    def _capture(event, **kwargs):  # type: ignore[no-untyped-def]
        info_calls.append((event, kwargs))
        return original_info(event, **kwargs)

    monkeypatch.setattr(dg_mod.logger, "info", _capture)

    async def task() -> None:
        async with dispatch_mutex("PVTI_MUTEX_CONTRACT", "implementing"):
            await asyncio.sleep(0.01)

    await asyncio.gather(task(), task())
    waited = [c for c in info_calls if c[0] == "dispatch_performer.mutex_waited"]
    assert len(waited) == 1
    fields = waited[0][1]
    assert fields["card_id"] == "PVTI_MUTEX_CONTRACT"
    assert fields["performer_stage"] == "implementing"
    assert fields["wait_ms"] >= 0
