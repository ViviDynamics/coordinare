"""354: the dashboard session summary carries the queued-for-slot marker.

The board row renders "Queued for <stage> · <wait>" from the summary's
``slot_queued_since``; if the builder drops the field the row falls back
to the misleading "Dispatching" presentation this spec removes.
"""
from __future__ import annotations

from datetime import UTC, datetime

from coordinare.dashboard import DashboardStore
from tests.unit.test_dashboard import _make_mock_daemon, _make_mock_health, _make_mock_metrics


def test_session_summary_includes_slot_queued_since() -> None:
    queued_at = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["active_sessions"] = {
        "PVTI_Q": {
            "phase": "dispatching",
            "performer_stage": "implementing",
            "slot_queued_since": queued_at,
        },
    }
    snap = store.build_snapshot(daemon, _make_mock_metrics(), _make_mock_health())
    summary = next(
        s for s in snap["active_sessions"] if s["card_id"] == "PVTI_Q"
    )
    assert summary["slot_queued_since"] == "2026-09-23T12:00:00+00:00"


def test_session_summary_slot_queued_since_absent_is_none() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["active_sessions"] = {
        "PVTI_ACTIVE": {
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
        },
    }
    snap = store.build_snapshot(daemon, _make_mock_metrics(), _make_mock_health())
    summary = next(
        s for s in snap["active_sessions"] if s["card_id"] == "PVTI_ACTIVE"
    )
    assert summary["slot_queued_since"] is None
