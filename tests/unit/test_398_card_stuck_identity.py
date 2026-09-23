"""398 — card_stuck must name the stuck card and not collapse distinct stalls.

Two live failures: the alert fired with blank card_id/number/title whenever
``current_card`` was absent (the common case in ``dispatching``, where the
card has not been adopted yet), and the dedup key degraded to ``stuck::phase``,
so every cardless stall in a phase collapsed into ONE bucket — a second stuck
card was not merely anonymous, it was silent.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing

from coordinare.config import StuckAlertConfig
from coordinare.daemon import (
    CoordinareDaemon,
    resolve_stuck_card,
    stuck_dedup_key,
)
from coordinare.models.notification import EventType
from coordinare.services.activity_log import ActivityLog
from tests.utils.fake_notification import FakeNotificationService

_ENTRY_A = datetime.now(UTC) - timedelta(hours=2)
_ENTRY_B = datetime.now(UTC) - timedelta(hours=1)
_SOLO_CARD = {"id": "CARD_SOLO", "title": "Solo card", "issue_number": 7}


class _CardlessStuckGraph:
    """Stuck in ``dispatching`` with no ``current_card`` and no session in flight.

    The phase entry time alternates between two fixed instants, so a
    multi-cycle run produces two distinct stall episodes deterministically;
    with ``alternate_episodes=False`` every cycle is the same episode.
    """

    def __init__(self, *, alternate_episodes: bool = True) -> None:
        self._alternate = alternate_episodes
        self._calls = 0

    async def ainvoke(self, state):
        self._calls += 1
        entry = (_ENTRY_A if self._calls % 2 == 0 else _ENTRY_B) if self._alternate else _ENTRY_A
        state["phase"] = "dispatching"
        state["phase_entered_at"] = entry
        state["current_card"] = None
        state["active_sessions"] = {}
        if state.get("config") is None:
            cfg = MagicMock()
            cfg.stuck_alerts = StuckAlertConfig(
                threshold_seconds=60,
                per_phase_thresholds={"dispatching": 60},
                cooldown_seconds=0,  # the daemon-wide stamp must not mask the key policy
            )
            state["config"] = cfg
        return state


async def _run(graph: Any, cycles: int) -> tuple[list[dict], list, list[dict]]:
    fake = FakeNotificationService()
    log = ActivityLog()
    daemon = CoordinareDaemon(
        graph,
        max_cycles=cycles,
        sleep_func=AsyncMock(),
        idle_threshold_seconds=9999,
    )
    daemon.state["notification_service"] = fake
    daemon.state["activity_log"] = log
    with structlog.testing.capture_logs() as captured:
        await daemon.start()
    warnings = [e for e in captured if e.get("event") == "card_stuck"]
    dispatches = [e for e in fake.dispatched if e.event_type == EventType.card_stuck]
    feed = [e for e in log.snapshot() if e["activity_type"] == "stuck"]
    return warnings, dispatches, feed


class TestResolveStuckCard:
    """The identity behind the alert, tested directly.

    The daemon's multi-session fanout round-trips session dicts through
    ``state_to_session``, so a raw in-flight session is not stable in a
    daemon-driven test; the resolver itself is a pure read of state.
    """

    def test_the_mirror_wins_when_a_card_is_pinned(self) -> None:
        card = {"id": "CARD_1", "title": "T", "issue_number": 1}
        state = {"current_card": card, "active_sessions": {}}
        assert resolve_stuck_card(state) is card

    def test_a_single_in_flight_session_names_the_stuck_card(self) -> None:
        state = {
            "current_card": None,
            "active_sessions": {"CARD_SOLO": {"current_card": dict(_SOLO_CARD)}},
        }
        assert resolve_stuck_card(state) == _SOLO_CARD

    def test_zero_sessions_name_nothing(self) -> None:
        state = {"current_card": None, "active_sessions": {}}
        assert resolve_stuck_card(state) == {}

    def test_several_active_sessions_are_not_a_guess(self) -> None:
        """With several cards in flight there is no single card to name."""
        sessions = {
            "CARD_1": {"current_card": {"id": "CARD_1", "title": "One", "issue_number": 1}},
            "CARD_2": {"current_card": {"id": "CARD_2", "title": "Two", "issue_number": 2}},
        }
        assert resolve_stuck_card({"current_card": None, "active_sessions": sessions}) == {}

    def test_a_session_without_a_card_slot_is_not_a_crash(self) -> None:
        state = {"current_card": None, "active_sessions": {"CARD_X": "not-a-dict"}}
        assert resolve_stuck_card(state) == {}


class TestCardlessStallsDoNotCollapseIntoOneKey:
    """At minimum (the issue's own floor): distinct stalls, distinct keys."""

    @pytest.mark.asyncio
    async def test_two_distinct_cardless_stalls_each_alert(self) -> None:
        warnings, dispatches, _ = await _run(_CardlessStuckGraph(), cycles=3)
        keys = [e.dedup_key for e in dispatches]
        assert len(keys) == 2, f"distinct cardless stalls collapsed: {keys}"
        assert all(k.startswith("stuck:(no-card):dispatching:") for k in keys)
        assert keys[0] != keys[1]
        assert len(warnings) == 2, "the log must not go silent for the second stall"

    @pytest.mark.asyncio
    async def test_one_cardless_stall_is_still_logged_once_per_episode(self) -> None:
        """Within one episode the cooldown still holds — no per-cycle spam.

        The dispatch is deduped downstream by the service's dedup_key (repeats
        share one key here); the always-available log surface fires once.
        """
        warnings, dispatches, _ = await _run(
            _CardlessStuckGraph(alternate_episodes=False), cycles=3,
        )
        assert len(warnings) == 1, "the same episode must not re-log within the cooldown"
        assert len({e.dedup_key for e in dispatches}) == 1, "repeats share one dedup key"

    @pytest.mark.asyncio
    async def test_the_cardless_alert_still_names_the_phase_and_age(self) -> None:
        warnings, _, _ = await _run(_CardlessStuckGraph(), cycles=2)
        assert warnings[0]["stage"] == "dispatching"
        assert warnings[0]["stuck_minutes"] >= 120, (
            "the entry is two hours back; a longer suite run only adds minutes"
        )
        assert warnings[0]["card_id"] == ""


class TestStuckDedupKeyPolicy:
    """The key derivation, tested directly rather than asserted on source."""

    def test_a_card_keys_its_own_stall(self) -> None:
        assert stuck_dedup_key("CARD_1", "dispatching", _ENTRY_A) == "stuck:CARD_1:dispatching"

    def test_distinct_cardless_episodes_get_distinct_keys(self) -> None:
        first = stuck_dedup_key("", "dispatching", _ENTRY_A)
        second = stuck_dedup_key("", "dispatching", _ENTRY_B)
        assert first != second

    def test_the_same_episode_shares_a_key(self) -> None:
        assert (
            stuck_dedup_key("", "dispatching", _ENTRY_A)
            == stuck_dedup_key("", "dispatching", _ENTRY_A)
        )

    def test_a_cardless_key_is_not_the_degraded_blank_id_form(self) -> None:
        """The old key was ``stuck::phase`` — indistinguishable from card X at id ''."""
        key = stuck_dedup_key("", "dispatching", _ENTRY_A)
        assert key.startswith("stuck:(no-card):")


class TestTheKeyIsDerivedOnceAndSharedByBothPaths:
    """139's invariant, restated for 398: one policy, not two."""

    def test_the_stall_log_and_the_dispatch_use_the_same_derivation(self) -> None:
        import inspect

        from coordinare import daemon

        source = inspect.getsource(daemon)
        key_at = source.index("_stuck_key = stuck_dedup_key(")
        log_at = source.index("if should_log_stall(")
        dispatch_at = source.index("dedup_key=_stuck_key,")
        assert log_at > key_at, "the log must use the derived key"
        assert dispatch_at > key_at, "the dispatch must use the derived key"
        assert source.count("_stuck_key = stuck_dedup_key(") == 1, (
            "the key must be derived exactly once — two derivations drift apart"
        )
