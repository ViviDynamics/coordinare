"""138 US2: stall / stuck / recovery reach the UI with no notification channel.

This is the regression suite for the reported bug: the signal used to be lost
at *delivery* — detection ran, then handed the decision to a notification
service with nothing to route it to. The feed is a destination that always
exists.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.config import StuckAlertConfig
from coordinare.daemon import CoordinareDaemon
from coordinare.graph.nodes.check_board import _attempt_blocked_card_recovery
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state
from coordinare.models.notification import EventType
from coordinare.services.activity_log import ActivityLog
from coordinare.services.progress_fingerprint import progress_fingerprint
from tests.utils.fake_notification import FakeNotificationService

# ---------------------------------------------------------------------------
# T030 / T033 — stuck
# ---------------------------------------------------------------------------


class _StuckGraph:
    """Keeps the phase in monitoring_performer with a long-past entry time."""

    def __init__(self, *, cooldown: int) -> None:
        self._cooldown = cooldown

    async def ainvoke(self, state: dict) -> dict:
        state["phase"] = "monitoring_performer"
        state["phase_entered_at"] = datetime.now(UTC) - timedelta(hours=2)
        if state.get("config") is None:
            cfg = MagicMock()
            cfg.stuck_alerts = StuckAlertConfig(
                threshold_seconds=60,
                per_phase_thresholds={"monitoring_performer": 60},
                cooldown_seconds=self._cooldown,
            )
            state["config"] = cfg
        state["current_card"] = {"id": "CARD_1", "title": "Wedged card", "issue_number": 42}
        return state


async def _run_stuck_daemon(
    *, cycles: int, cooldown: int, notifications: Any = None
) -> ActivityLog:
    log = ActivityLog()
    daemon = CoordinareDaemon(
        _StuckGraph(cooldown=cooldown),
        max_cycles=cycles,
        sleep_func=AsyncMock(),
        idle_threshold_seconds=9999,
    )
    daemon.state["activity_log"] = log
    if notifications is not None:
        daemon.state["notification_service"] = notifications
    await daemon.start()
    return log


@pytest.mark.asyncio
async def test_stuck_reaches_the_feed_with_zero_notification_channels() -> None:
    """FR-009: the reported bug. No notification service at all, entry anyway."""
    # Two cycles: the stuck check runs against the phase the previous cycle set.
    log = await _run_stuck_daemon(cycles=2, cooldown=9999)
    stuck = [e for e in log.snapshot() if e["activity_type"] == "stuck"]
    assert len(stuck) == 1
    assert stuck[0]["card_id"] == "CARD_1"
    assert stuck[0]["card_number"] == 42
    assert stuck[0]["stage"] == "monitoring_performer"
    assert "monitoring_performer" in stuck[0]["text"]
    assert "min" in stuck[0]["text"]


@pytest.mark.asyncio
async def test_stuck_repeats_stay_bounded_by_cooldown_with_zero_channels() -> None:
    """FR-013, SC-006: the cooldown stamp must land even when nothing dispatched.

    Left inside the try (and behind the dispatch), a None service raised, the
    stamp never landed, and the feed took an entry every single cycle.
    """
    log = await _run_stuck_daemon(cycles=5, cooldown=9999)
    stuck = [e for e in log.snapshot() if e["activity_type"] == "stuck"]
    assert len(stuck) == 1, f"cooldown did not hold: {len(stuck)} entries over 5 cycles"


@pytest.mark.asyncio
async def test_stuck_coexists_with_a_configured_channel() -> None:
    """FR-011: the feed entry appears AND the notification still dispatches."""
    fake = FakeNotificationService()
    log = await _run_stuck_daemon(cycles=3, cooldown=9999, notifications=fake)
    dispatched = [e for e in fake.dispatched if e.event_type == EventType.card_stuck]
    assert len(dispatched) == 1, "dispatch behaviour changed"
    assert len([e for e in log.snapshot() if e["activity_type"] == "stuck"]) == 1


# ---------------------------------------------------------------------------
# T031 — stall
# ---------------------------------------------------------------------------


class _WedgedPerformer:
    async def check_status(self, session_id: str, **_: object) -> dict:
        _ = session_id
        return {"status": "working", "events": [{"type": "progress", "text": "still working"}]}


@pytest.mark.asyncio
async def test_stall_watchdog_trip_reaches_the_feed() -> None:
    """FR-010: with stall_timeout_seconds configured, the trip is visible."""
    log = ActivityLog()
    state = initial_state()
    state["performer_services"] = {"implementing": _WedgedPerformer()}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "CARD_S", "status": "IN_PROGRESS", "title": "Hung", "issue_number": 9}
    state["agent_dispatch"] = {"session_id": "sess-1"}
    state["activity_log"] = log
    dd = MagicMock()
    dd.stall_timeout_seconds = 120
    dd.idle_timeout_retries = 2
    dd.idle_timeout_window_hours = 24
    dd.drain_budget_seconds = 0.0
    dd.reap_budget_seconds = 0.0
    coordinare_cfg = MagicMock()
    coordinare_cfg.dispatcher_dedup = dd
    state["coordinare_config"] = coordinare_cfg
    # Progress last observed well past the stall threshold.
    state["last_progress_at"] = datetime.now(UTC) - timedelta(seconds=600)
    # 329: derive the prior fingerprint from the same helper the node uses,
    # so this test cannot drift from the implementation's format.
    state["last_progress_fingerprint"] = progress_fingerprint(
        [{"type": "progress", "text": "still working"}]
    )

    await monitor_performer(state)

    stall = [e for e in log.snapshot() if e["activity_type"] == "stall"]
    assert len(stall) == 1
    assert stall[0]["card_id"] == "CARD_S"
    assert stall[0]["stage"] == "implementing"
    assert "no progress" in stall[0]["text"]


# ---------------------------------------------------------------------------
# T032 — auto-recovery
# ---------------------------------------------------------------------------


class _FakeGitHub:
    def __init__(self, ctx: dict) -> None:
        self._ctx = ctx
        self.moved: list[tuple[str, str]] = []

    async def find_pr_for_issue(self, issue_node: str) -> dict:
        _ = issue_node
        return {"pr_node_id": "PR1"}

    async def get_pr_review_context(self, pr_id: str) -> dict:
        _ = pr_id
        return self._ctx

    async def move_card(self, card_id: str, column: str) -> None:
        self.moved.append((card_id, column))


_RECOVERY_CTX = {
    "reviews": [
        {"id": "RVW_1", "author_login": "jason", "state": "CHANGES_REQUESTED", "commit_oid": "oldsha"}
    ],
    "review_threads": [{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}],
    "head_oid": "newsha",
    "review_decision": "CHANGES_REQUESTED",
}


@pytest.mark.asyncio
async def test_auto_recovery_reaches_the_feed_with_zero_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-012: recorded before the notification branch, so no channel is needed."""
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    log = ActivityLog()
    gh = _FakeGitHub(_RECOVERY_CTX)
    state: dict = {"human_reviewers": ["jason"], "activity_log": log}  # no notification_service
    await _attempt_blocked_card_recovery(
        state, gh, ["CARD_1"], {"content_node_ids": {"CARD_1": "ISSUE_1"}}
    )

    assert ("CARD_1", "IN_REVIEW") in gh.moved
    recovered = [e for e in log.snapshot() if e["activity_type"] == "recovered"]
    assert len(recovered) == 1
    assert recovered[0]["card_id"] == "CARD_1"
    assert "IN_REVIEW" in recovered[0]["text"]


# ---------------------------------------------------------------------------
# T034 — recording is observation, never intervention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recording_never_alters_phase_retries_or_outcome() -> None:
    """FR-031, SC-012: identical routing with and without a log attached."""

    def _fresh() -> dict:
        state = initial_state()
        state["performer_services"] = {"implementing": _WedgedPerformer()}
        state["performer_stage"] = "implementing"
        state["lifecycle_sequence"] = ["implementing", "reviewing"]
        state["current_card"] = {
            "id": "CARD_C", "status": "IN_PROGRESS", "title": "Card", "issue_number": 3
        }
        state["agent_dispatch"] = {"session_id": "sess-1"}
        return state

    watched = _fresh()
    watched["activity_log"] = ActivityLog()
    without = await monitor_performer(_fresh())
    with_log = await monitor_performer(watched)

    outcome_fields = (
        "phase", "performer_stage", "system_error_count",
        "system_error_reason", "feedback_cycle_count", "idle_timeout_retries",
    )
    for field in outcome_fields:
        assert with_log.get(field) == without.get(field), f"{field} diverged"
    assert with_log.get("performer_events") == without.get("performer_events")
