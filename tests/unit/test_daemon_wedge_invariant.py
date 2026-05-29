"""Spec 076 T093 — wedge invariant unit tests.

Covers FR-020 / clarification Q1 (default release-the-pin) and
FR-020 BLOCKED promotion after N wedges in the window.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from coordinare.services.dispatcher_dedup_models import WedgeResolution
from coordinare.services.reconciliation import detect_wedged_state


def _wedged_state(card_id: str = "PVTI_X") -> dict:
    """Build the forbidden state combination from FR-020.

    Accepts ``card_id`` so per-card isolation tests can wedge different
    cards on the same state object.
    """
    return {
        "active_card": {"id": card_id, "title": "Card"},
        "current_card": {"id": card_id, "title": "Card"},
        "active_card_id": card_id,
        "active_sessions": {},  # MISSING — that's the wedge
        "performer_stage": None,
        "phase": None,  # or "idle"
    }


def test_no_wedge_returns_none() -> None:
    """If active_card is None or a session exists, no wedge."""
    state: dict = {"active_card": None, "active_sessions": {}}
    assert detect_wedged_state(state) is None


def test_session_present_means_no_wedge() -> None:
    state = _wedged_state()
    state["active_sessions"] = {"PVTI_X": {"phase": "monitoring_performer"}}
    assert detect_wedged_state(state) is None


def test_non_idle_phase_means_no_wedge() -> None:
    state = _wedged_state()
    state["phase"] = "dispatching"
    assert detect_wedged_state(state) is None


def test_performer_stage_set_means_no_wedge() -> None:
    state = _wedged_state()
    state["performer_stage"] = "implementing"
    assert detect_wedged_state(state) is None


def test_wedge_default_releases_the_pin() -> None:
    """Q1: default recovery is release-the-pin (active_card=None)."""
    state = _wedged_state()
    resolution = detect_wedged_state(state)
    assert resolution == WedgeResolution.RELEASED
    assert state["active_card"] is None
    assert state["current_card"] is None
    assert state["active_card_id"] is None


def test_wedge_records_timestamp_in_window() -> None:
    state = _wedged_state()
    detect_wedged_state(state)
    window = state["wedge_count_window"]
    # Per-card dict (data-model §8) — entry keyed by card_id
    assert isinstance(window, dict)
    assert "PVTI_X" in window
    assert len(window["PVTI_X"]) == 1
    assert isinstance(window["PVTI_X"][0], datetime)


def test_repeated_wedges_within_window_accumulate() -> None:
    state = _wedged_state()
    detect_wedged_state(state)
    # Re-establish the wedge for a second detection
    state["active_card"] = {"id": "PVTI_X", "title": "Card"}
    state["current_card"] = {"id": "PVTI_X", "title": "Card"}
    state["phase"] = None
    detect_wedged_state(state)
    assert len(state["wedge_count_window"]["PVTI_X"]) == 2


def test_different_cards_have_isolated_wedge_windows() -> None:
    """Per-card isolation: card A's wedges MUST NOT count toward
    card B's BLOCKED threshold."""
    # Wedge card A 2 times
    state_a = _wedged_state(card_id="PVTI_A")
    detect_wedged_state(state_a)
    state_a["active_card"] = {"id": "PVTI_A", "title": "Card A"}
    state_a["current_card"] = {"id": "PVTI_A", "title": "Card A"}
    state_a["phase"] = None
    detect_wedged_state(state_a)

    # Now wedge card B once on the SAME state (carries A's window)
    state_a["active_card"] = {"id": "PVTI_B", "title": "Card B"}
    state_a["current_card"] = {"id": "PVTI_B", "title": "Card B"}
    state_a["phase"] = None
    detect_wedged_state(state_a, wedge_block_threshold=3)

    windows = state_a["wedge_count_window"]
    assert len(windows["PVTI_A"]) == 2
    assert len(windows["PVTI_B"]) == 1
    # Card B's single wedge MUST NOT have promoted to BLOCKED — only
    # A's 2 wedges + B's 1 wedge would have done so if the windows
    # were not isolated.


def test_blocked_promotion_after_threshold() -> None:
    """FR-020: after N wedges in the window, the NEXT wedge promotes to
    BLOCKED.  With threshold=3, the FOURTH wedge promotes."""
    state = _wedged_state()
    now = datetime.now(UTC)
    # Pre-populate 3 recent wedges (per-card shape) — the next wedge promotes
    state["wedge_count_window"] = {
        "PVTI_X": [
            now - timedelta(minutes=15),
            now - timedelta(minutes=10),
            now - timedelta(minutes=5),
        ],
    }
    resolution = detect_wedged_state(state, wedge_block_threshold=3)
    assert resolution == WedgeResolution.BLOCKED
    assert state["phase"] == "blocked"
    # Pin NOT released (operator must intervene)
    assert state["active_card"] is not None


def test_exactly_threshold_wedges_still_releases() -> None:
    """Boundary: exactly threshold wedges in the window still releases
    the pin.  The promotion fires only on the wedge AFTER threshold —
    e.g. with threshold=3, wedges 1/2/3 release and wedge 4 promotes."""
    state = _wedged_state()
    now = datetime.now(UTC)
    state["wedge_count_window"] = {
        "PVTI_X": [now - timedelta(minutes=10), now - timedelta(minutes=5)],
    }
    resolution = detect_wedged_state(state, wedge_block_threshold=3)
    # Window now has 3 entries (2 pre-populated + this one); threshold=3,
    # 3 is NOT > 3 → RELEASED
    assert resolution == WedgeResolution.RELEASED


def test_window_expired_entries_drop_off() -> None:
    """Entries older than window_hours should not count toward the
    promotion threshold."""
    state = _wedged_state()
    old = datetime.now(UTC) - timedelta(hours=25)
    state["wedge_count_window"] = {"PVTI_X": [old, old]}
    resolution = detect_wedged_state(state, wedge_block_threshold=3, wedge_block_window_hours=24)
    assert resolution == WedgeResolution.RELEASED  # both prior entries dropped
    assert len(state["wedge_count_window"]["PVTI_X"]) == 1


def test_no_active_card_means_no_wedge() -> None:
    """Without an active_card pin, there's nothing to wedge."""
    state: dict = {
        "active_card": None,
        "active_sessions": {},
        "performer_stage": None,
        "phase": "idle",
    }
    assert detect_wedged_state(state) is None


def test_active_card_with_empty_id_returns_none() -> None:
    """Defensive: a card dict with no/empty id can't be wedge-detected
    (we'd have no key to track in wedge_count_window)."""
    state: dict = {
        "active_card": {"id": "", "title": "no id"},
        "current_card": {"id": "", "title": "no id"},
        "active_sessions": {},
        "performer_stage": None,
        "phase": None,
    }
    assert detect_wedged_state(state) is None
