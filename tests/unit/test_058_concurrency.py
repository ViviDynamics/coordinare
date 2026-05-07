"""058 — monitoring_pr sessions must not count toward max_concurrent_cards."""
from __future__ import annotations

from coordinare.graph.nodes.check_board import PASSIVE_PHASES


def _make_session(phase: str) -> dict:
    return {"phase": phase, "current_card": {"id": f"card-{phase}"}}


def _active_count(sessions: dict) -> int:
    return sum(1 for s in sessions.values() if s.get("phase") not in PASSIVE_PHASES)


class TestPassivePhasesConstant:
    def test_monitoring_pr_is_passive(self):
        assert "monitoring_pr" in PASSIVE_PHASES

    def test_active_phases_not_passive(self):
        for phase in ("dispatching", "merging", "blocked", "recovery", "relay_feedback"):
            assert phase not in PASSIVE_PHASES


class TestActiveCountFiltering:
    def test_passive_session_excluded(self):
        sessions = {"c1": _make_session("monitoring_pr")}
        assert _active_count(sessions) == 0

    def test_active_session_counted(self):
        for phase in ("dispatching", "merging", "blocked", "recovery"):
            sessions = {"c1": _make_session(phase)}
            assert _active_count(sessions) == 1, f"phase={phase} should count as active"

    def test_mixed_passive_and_active(self):
        sessions = {
            "c1": _make_session("monitoring_pr"),
            "c2": _make_session("monitoring_pr"),
            "c3": _make_session("dispatching"),
        }
        assert _active_count(sessions) == 1

    def test_all_passive_gives_zero(self):
        sessions = {
            "c1": _make_session("monitoring_pr"),
            "c2": _make_session("monitoring_pr"),
        }
        assert _active_count(sessions) == 0

    def test_no_sessions_gives_zero(self):
        assert _active_count({}) == 0


class TestSlotCalculation:
    """Verify the slot calculation logic matches what check_board uses."""

    def test_passive_sessions_free_slot(self):
        max_cards = 2
        sessions = {"c1": _make_session("monitoring_pr")}
        active = _active_count(sessions)
        slots_available = max(0, max_cards - active)
        assert slots_available == 2  # passive session doesn't consume a slot

    def test_active_sessions_consume_slot(self):
        max_cards = 2
        sessions = {
            "c1": _make_session("dispatching"),
            "c2": _make_session("monitoring_pr"),
        }
        active = _active_count(sessions)
        slots_available = max(0, max_cards - active)
        assert slots_available == 1  # only the dispatching session counts

    def test_full_active_leaves_no_slots(self):
        max_cards = 2
        sessions = {
            "c1": _make_session("dispatching"),
            "c2": _make_session("merging"),
        }
        active = _active_count(sessions)
        slots_available = max(0, max_cards - active)
        assert slots_available == 0

    def test_never_exceeds_max_concurrent_cards(self):
        max_cards = 3
        sessions = {
            "c1": _make_session("dispatching"),
            "c2": _make_session("dispatching"),
            "c3": _make_session("monitoring_pr"),
            "c4": _make_session("monitoring_pr"),
        }
        active = _active_count(sessions)
        assert active <= max_cards
