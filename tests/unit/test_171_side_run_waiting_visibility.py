"""171 review fix: a side run waiting for a slot is visible to the operator.

It logs once at info when it starts waiting, records a skip reason on the card (which
the dashboard snapshot carries as ``session_skip_reasons``), and adds an activity-feed
entry. The reason clears when the run starts.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from coordinare.daemon import _compute_session_eligibilities
from tests.unit.test_171_side_run_budget import (
    _busy,
    _daemon,
    _drain,
    _parked,
    _run,
    _Svc,
)


def _waiting_daemon(sessions: dict[str, Any], svc: _Svc) -> Any:
    d = _daemon(sessions, svc, limit=1)
    d._state["activity_log"] = MagicMock()
    return d


@pytest.mark.asyncio
async def test_a_waiting_side_run_is_recorded_as_a_skip_reason_for_the_snapshot() -> None:
    sessions = {"A": _busy("A"), "P": _parked("P")}
    svc = _Svc()
    d = _waiting_daemon(sessions, svc)
    await _run(d, sessions, {"A", "P"})
    await _drain(d)
    assert svc.dispatched == []
    _compute_session_eligibilities(d._state, sessions, 1)
    reason = (d._state.get("session_skip_reasons") or {}).get("P")
    assert reason is not None and reason["reason"] == "side_run_waiting"
    assert "waiting for a free slot" in reason["detail"]


@pytest.mark.asyncio
async def test_a_waiting_side_run_logs_once_at_info_and_records_an_activity_entry() -> None:
    sessions = {"A": _busy("A"), "P": _parked("P")}
    svc = _Svc()
    d = _waiting_daemon(sessions, svc)
    with capture_logs() as logs:
        await _run(d, sessions, {"A", "P"})
        await _run(d, sessions, {"A", "P"})
        await _drain(d)
    waiting = [e for e in logs if e.get("event") == "documenting_side.waiting_for_slot"]
    assert len(waiting) == 1
    assert waiting[0]["log_level"] == "info" and waiting[0]["card_id"] == "P"
    activity = d._state["activity_log"]
    assert activity.record.call_args.kwargs["activity_type"] == "side_run_waiting"
    assert activity.record.call_args.kwargs["card_id"] == "P"


@pytest.mark.asyncio
async def test_the_waiting_reason_clears_when_the_side_run_starts() -> None:
    sessions = {"A": _busy("A"), "P": _parked("P")}
    svc = _Svc()
    d = _waiting_daemon(sessions, svc)
    await _run(d, sessions, {"A", "P"})
    _compute_session_eligibilities(d._state, sessions, 1)
    assert "P" in (d._state.get("session_skip_reasons") or {})
    sessions["A"]["phase"] = "done"
    sessions["A"]["agent_dispatch"] = {}
    await _run(d, sessions, {"A", "P"})
    await _drain(d)
    assert svc.dispatched == ["P"]
    _compute_session_eligibilities(d._state, sessions, 1)
    assert "P" not in (d._state.get("session_skip_reasons") or {})
