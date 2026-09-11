"""The workflow step has to reach the operator, not just the session (#343).

Three seams, each of which has broken before in this area:
  - the session summary must carry the step (#348 was exactly this gap);
  - the SSE watcher must treat a step change as worth broadcasting, or the
    trail only refreshes when the stage happens to move, minutes apart;
  - the fields must round-trip per card and survive a restart.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

from coordinare.dashboard import DashboardStore
from coordinare.session import _SESSION_FIELDS

ENTERED = datetime(2026, 9, 11, 1, 16, 39, tzinfo=UTC)


def _make_metrics() -> MagicMock:
    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    metrics.build_info.labels.return_value._value.get.return_value = {
        "started_at": "2026-03-15T10:00:00+00:00"
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


def _session(**overrides) -> dict:
    session = {
        "current_card": {"title": "Weekly timesheets", "issue_number": 106},
        "phase": "monitoring_performer",
        "performer_stage": "implementing",
        "agent_dispatch": {"session_id": "s1", "backend": "codex"},
        "workflow_step": "implementer.baseline",
        "workflow_step_entered_at": ENTERED,
        "workflow_step_trail": [
            {"step": "implementer.intake", "entered_at": "2026-09-11T01:10:00+00:00"},
            {"step": "implementer.baseline", "entered_at": ENTERED.isoformat()},
        ],
    }
    session.update(overrides)
    return session


def _summaries(daemon) -> list[dict]:
    return DashboardStore().build_snapshot(daemon, _make_metrics(), _make_health())["active_sessions"]


class TestTheStepReachesTheSnapshot:
    def test_step_entry_time_and_trail_are_on_the_session_summary(self) -> None:
        daemon = _make_daemon({"active_sessions": {"c1": _session()}})
        summary = _summaries(daemon)[0]

        assert summary["workflow_step"] == "implementer.baseline"
        assert summary["workflow_step_entered_at"] == ENTERED.isoformat()
        assert [e["step"] for e in summary["workflow_step_trail"]] == [
            "implementer.intake",
            "implementer.baseline",
        ]

    def test_two_cards_report_their_own_steps(self) -> None:
        """Per-card by construction -- two performers are in different steps."""
        daemon = _make_daemon({
            "active_sessions": {
                "c1": _session(),
                "c2": _session(
                    performer_stage="architecting",
                    workflow_step="architect.blueprint",
                    workflow_step_trail=[{"step": "architect.blueprint", "entered_at": "x"}],
                ),
            }
        })
        by_id = {s["card_id"]: s for s in _summaries(daemon)}
        assert by_id["c1"]["workflow_step"] == "implementer.baseline"
        assert by_id["c2"]["workflow_step"] == "architect.blueprint"

    def test_a_session_with_no_step_reports_none_not_a_missing_key(self) -> None:
        """The freeform pre-164 path emits no markers; absence is the signal."""
        daemon = _make_daemon({
            "active_sessions": {
                "c1": _session(
                    workflow_step=None, workflow_step_entered_at=None, workflow_step_trail=None
                )
            }
        })
        summary = _summaries(daemon)[0]
        assert summary["workflow_step"] is None
        assert summary["workflow_step_entered_at"] is None
        assert summary["workflow_step_trail"] == []


class TestLiveUpdates:
    def test_a_step_change_trips_the_broadcast_fingerprint(self) -> None:
        """Without this the trail would only refresh on a stage/phase change."""
        before = _make_daemon({"active_sessions": {"c1": _session()}})
        after = _make_daemon({
            "active_sessions": {"c1": _session(workflow_step="implementer.quality")}
        })
        store = DashboardStore()
        assert store._active_sessions_fingerprint(before) != store._active_sessions_fingerprint(
            after
        )

    def test_an_unchanged_step_does_not_trip_it(self) -> None:
        a = _make_daemon({"active_sessions": {"c1": _session()}})
        b = _make_daemon({"active_sessions": {"c1": _session()}})
        store = DashboardStore()
        assert store._active_sessions_fingerprint(a) == store._active_sessions_fingerprint(b)


class TestSlowStepThreshold:
    """The trail warns on the stall threshold, not the quiet one (#343).

    The quiet threshold defaults to 300s, and a legitimate `implementer.baseline`
    on a Rails repo takes ~370s. Keying the warning there would paint a normal
    step amber and teach the operator to ignore the colour.
    """

    def _snapshot(self, daemon) -> dict:
        return DashboardStore().build_snapshot(daemon, _make_metrics(), _make_health())

    def test_stall_timeout_is_published(self) -> None:
        cfg = MagicMock()
        cfg.dispatcher_dedup.stall_timeout_seconds = 900
        daemon = _make_daemon({"active_sessions": {}, "coordinare_config": cfg})
        assert self._snapshot(daemon)["stall_timeout_seconds"] == 900

    def test_it_is_distinct_from_the_quiet_threshold(self) -> None:
        cfg = MagicMock()
        cfg.dispatcher_dedup.stall_timeout_seconds = 900
        daemon = _make_daemon({"active_sessions": {}, "coordinare_config": cfg})
        snap = self._snapshot(daemon)
        assert snap["stall_timeout_seconds"] != snap["activity_quiet_threshold_seconds"]

    def test_absent_config_publishes_zero_meaning_no_warning(self) -> None:
        daemon = _make_daemon({"active_sessions": {}, "coordinare_config": None})
        assert self._snapshot(daemon)["stall_timeout_seconds"] == 0


class TestRoundTripAndPersistence:
    def test_the_fields_round_trip_per_card(self) -> None:
        for field in ("workflow_step", "workflow_step_entered_at", "workflow_step_trail"):
            assert field in _SESSION_FIELDS, (
                f"{field} must be in _SESSION_FIELDS or it is silently dropped "
                "between graph state and the session -- the #348 failure mode"
            )

    def test_persisted_session_carries_the_fields_with_safe_defaults(self) -> None:
        from coordinare.state_store import CURRENT_SCHEMA_VERSION, PersistedSession

        blank = PersistedSession(card_id="c1")
        assert blank.workflow_step is None
        assert blank.workflow_step_entered_at is None
        assert blank.workflow_step_trail == []
        assert CURRENT_SCHEMA_VERSION >= 24

    def test_an_older_snapshot_loads_without_the_new_fields(self) -> None:
        """v23 and earlier snapshots must not fail to parse."""
        from coordinare.state_store import PersistedSession

        restored = PersistedSession(**{"card_id": "c1", "performer_stage": "implementing"})
        assert restored.workflow_step is None
        assert restored.workflow_step_trail == []
