"""Per-session performer telemetry on the dashboard snapshot (issue #348).

In shared-pool / multi-symphony mode the daemon aggregates only
``active_sessions`` and ``phase`` from the per-symphony states. Everything
monitor_performer writes about a live performer -- ``performer_events``,
``performer_metrics``, ``session_stats``, ``backend_ui_url`` -- lands on the
*session*, so the dashboard's top-level reads returned None and every live
panel on /performers rendered its empty string while a performer was working.

These tests pin the plumbing at the seam that was missing: each session
summary in the SSE snapshot must carry its own telemetry, and log resolution
must go through the slot that card actually holds.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from coordinare.dashboard import SESSION_EVENT_LIMIT, DashboardStore
from coordinare.session import SessionStats

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_metrics() -> MagicMock:
    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    metrics.build_info.labels.return_value._value.get.return_value = {
        "started_at": "2026-03-15T10:00:00+00:00",
    }
    return metrics


def _make_health() -> MagicMock:
    health = MagicMock()
    health.snapshot.return_value.probes = []
    return health


def _make_daemon(state: dict) -> MagicMock:
    daemon = MagicMock()
    daemon._cycle_active = False
    daemon._webhook_trigger = asyncio.Event()
    daemon.running = True
    base = {"phase": "dispatching", "error_count": 0}
    base.update(state)
    daemon.state = base
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    return daemon


def _session(**overrides):
    session = {
        "current_card": {"title": "Weekly timesheets", "issue_number": 106},
        "phase": "monitoring_performer",
        "performer_stage": "implementing",
        "agent_dispatch": {
            "session_id": "sess-106",
            "container_id": "abc123def456",
            "backend": "claude_code",
        },
        "performer_events": [{"type": "tool_use", "text": "Edit app.rb"}],
        "performer_metrics": {"pid": 42, "cpu_percent": 12.5, "memory_bytes": 1024},
        "session_stats": SessionStats(
            title="Weekly timesheets", files_changed=3, lines_added=40, lines_removed=5,
        ),
        "backend_ui_url": "http://127.0.0.1:7788/",
    }
    session.update(overrides)
    return session


def _snapshot(daemon) -> dict:
    return DashboardStore().build_snapshot(daemon, _make_metrics(), _make_health())


def _only_summary(daemon) -> dict:
    summaries = _snapshot(daemon)["active_sessions"]
    assert len(summaries) == 1
    return summaries[0]


# ---------------------------------------------------------------------------
# The regression: telemetry must ride on the session, not top-level state
# ---------------------------------------------------------------------------


class TestSessionTelemetryOnSummary:
    def test_events_metrics_stats_travel_with_the_session(self) -> None:
        # Top-level state is deliberately bare -- this is exactly the shared-pool
        # shape, where the daemon folds sessions up and nothing else.
        daemon = _make_daemon({"active_sessions": {"card-1": _session()}})
        summary = _only_summary(daemon)

        assert summary["performer_events"] == [{"type": "tool_use", "text": "Edit app.rb"}]
        assert summary["performer_metrics"] == {
            "pid": 42,
            "cpu_percent": 12.5,
            "memory_bytes": 1024,
        }
        assert summary["session_stats"]["files_changed"] == 3
        assert summary["backend_ui_url"] == "http://127.0.0.1:7788/"
        assert summary["performer_backend"] == "claude_code"
        assert summary["session_id"] == "sess-106"

    def test_telemetry_is_not_read_from_top_level_state(self) -> None:
        """A second session must not inherit the first one's numbers.

        The pre-fix UI read one global blob, so every role's panel showed the
        same card. Two sessions with different telemetry pins that apart.
        """
        daemon = _make_daemon({
            "active_sessions": {
                "card-1": _session(),
                "card-2": _session(
                    performer_stage="architecting",
                    performer_events=[{"type": "text", "text": "drafting blueprint"}],
                    performer_metrics={"pid": 99, "cpu_percent": 1.0, "memory_bytes": 8},
                    agent_dispatch={"session_id": "sess-142", "backend": "junie"},
                    session_stats=None,
                    backend_ui_url=None,
                ),
            },
        })
        by_id = {s["card_id"]: s for s in _snapshot(daemon)["active_sessions"]}

        assert by_id["card-1"]["performer_metrics"]["pid"] == 42
        assert by_id["card-2"]["performer_metrics"]["pid"] == 99
        assert by_id["card-2"]["performer_events"][0]["text"] == "drafting blueprint"
        assert by_id["card-2"]["session_stats"] is None
        assert by_id["card-2"]["performer_backend"] == "junie"

    def test_events_are_bounded_per_session(self) -> None:
        """The writer caps at 100; the wire cap keeps N live cards bounded."""
        many = [{"type": "text", "text": f"e{i}"} for i in range(100)]
        daemon = _make_daemon({"active_sessions": {"card-1": _session(performer_events=many)}})
        events = _only_summary(daemon)["performer_events"]

        assert len(events) == SESSION_EVENT_LIMIT
        # The *newest* events survive -- truncating the tail instead would drop
        # the most recent cost event and silently zero the token readout.
        assert events[-1]["text"] == "e99"

    def test_missing_telemetry_serialises_as_empty_not_absent(self) -> None:
        """A dispatching session has no telemetry yet; keys must still exist."""
        daemon = _make_daemon({
            "active_sessions": {
                "card-1": _session(
                    phase="dispatching",
                    performer_events=None,
                    performer_metrics=None,
                    session_stats=None,
                    backend_ui_url=None,
                    agent_dispatch={},
                ),
            },
        })
        summary = _only_summary(daemon)

        assert summary["performer_events"] == []
        assert summary["performer_metrics"] is None
        assert summary["session_stats"] is None
        assert summary["backend_ui_url"] is None
        assert summary["performer_backend"] is None
        assert summary["session_id"] is None


class TestSingleSymphonyFallbackRow:
    """The synthesized row must carry the same keys, so the UI has one path."""

    def test_synthesized_row_mirrors_top_level_telemetry(self) -> None:
        daemon = _make_daemon({
            "phase": "monitoring_performer",
            "active_sessions": {},
            "current_card": {"title": "Legacy card", "issue_number": 7},
            "performer_stage": "implementing",
            "performer_events": [{"type": "cost", "text": "5,000 tokens"}],
            "performer_metrics": {"pid": 7},
            "session_stats": SessionStats(
                title="Legacy card", files_changed=1, lines_added=2, lines_removed=0,
            ),
            "backend_ui_url": "http://localhost:9999/",
            "agent_dispatch": {"session_id": "legacy-1", "backend": "claude_code"},
        })
        summary = _only_summary(daemon)

        assert summary["performer_events"] == [{"type": "cost", "text": "5,000 tokens"}]
        assert summary["performer_metrics"] == {"pid": 7}
        assert summary["session_stats"]["files_changed"] == 1
        assert summary["session_stats"]["title"] == "Legacy card"
        assert summary["backend_ui_url"] == "http://localhost:9999/"
        assert summary["session_id"] == "legacy-1"


# ---------------------------------------------------------------------------
# Log resolution: by slot, and read-only
# ---------------------------------------------------------------------------


class _Pool:
    def __init__(self, services, active_slots):
        self.services = services
        self.active_slots = active_slots


class _Slot:
    def __init__(self, service_index):
        self.service_index = service_index


class _Service:
    def __init__(self, lines):
        self._lines = lines

    def get_agent_logs(self):
        return list(self._lines)


class TestPerCardLogResolution:
    def test_logs_come_from_the_slot_the_card_holds(self) -> None:
        """With two slots on one role, each card gets its own buffer.

        The buffer lives on the service instance, so reading the primary
        service returned whichever card ran last -- the defect this replaces.
        """
        slot_mgr = SimpleNamespace(pools={
            "implementing": _Pool(
                services=[_Service(["card-1 line"]), _Service(["card-2 line"])],
                active_slots={"card-1": _Slot(0), "card-2": _Slot(1)},
            ),
        })
        daemon = _make_daemon({
            "slot_manager": slot_mgr,
            "active_sessions": {"card-1": _session(), "card-2": _session()},
        })
        by_id = {s["card_id"]: s for s in _snapshot(daemon)["active_sessions"]}

        assert by_id["card-1"]["performer_logs"] == ["card-1 line"]
        assert by_id["card-2"]["performer_logs"] == ["card-2 line"]

    def test_card_without_a_slot_gets_no_logs_rather_than_someone_elses(self) -> None:
        slot_mgr = SimpleNamespace(pools={
            "implementing": _Pool(
                services=[_Service(["someone else's output"])],
                active_slots={"card-other": _Slot(0)},
            ),
        })
        daemon = _make_daemon({
            "slot_manager": slot_mgr,
            "active_sessions": {"card-1": _session()},
        })

        assert _only_summary(daemon)["performer_logs"] == []

    def test_resolution_never_allocates_a_slot(self) -> None:
        """Rendering a page must not consume performer capacity."""
        acquire = MagicMock()
        slot_mgr = SimpleNamespace(
            pools={"implementing": _Pool(services=[_Service(["x"])], active_slots={"card-1": _Slot(0)})},
            acquire=acquire,
        )
        daemon = _make_daemon({
            "slot_manager": slot_mgr,
            "active_sessions": {"card-1": _session()},
        })
        _snapshot(daemon)

        assert not acquire.called

    def test_legacy_single_service_mode_still_reports_logs(self) -> None:
        """No slot pools at all: fall back to the legacy agent_service."""
        daemon = _make_daemon({
            "slot_manager": SimpleNamespace(pools={}),
            "agent_service": _Service(["legacy line"]),
            "active_sessions": {"card-1": _session()},
        })

        assert _only_summary(daemon)["performer_logs"] == ["legacy line"]

    def test_a_raising_service_does_not_break_the_snapshot(self) -> None:
        class _Boom:
            def get_agent_logs(self):
                raise RuntimeError("transport gone")

        slot_mgr = SimpleNamespace(pools={
            "implementing": _Pool(services=[_Boom()], active_slots={"card-1": _Slot(0)}),
        })
        daemon = _make_daemon({
            "slot_manager": slot_mgr,
            "active_sessions": {"card-1": _session()},
        })

        assert _only_summary(daemon)["performer_logs"] == []
