"""Spec 076 T094 (FR-020 regression) — wedged-state recovery.

Reconstructs the fourth anomaly from today's incident: card #101 with
active_card pinned but active_sessions empty and phase=idle, coordinare
stuck without picking up the card.  Asserts that one cycle through
the wedge invariant releases the pin so the next cycle re-picks the
card via normal eligibility.
"""
from __future__ import annotations

from coordinare.services.dispatcher_dedup_models import WedgeResolution
from coordinare.services.reconciliation import detect_wedged_state


def test_fr020_today_incident_state_releases_pin() -> None:
    """Today's exact wedge: active_card.status=IN_PROGRESS, no session,
    phase=None.  Default action MUST release the pin so the next poll
    cycle re-picks the card from the board normally."""
    # Reconstruct the state observed at 19:01 on 2026-05-28
    state = {
        "active_card": {
            "id": "PVTI_lADOBjmjsc4ApgHnzgq-8Gc",
            "issue_number": 101,
            "title": "Feature: Time tracking schema and model foundation",
            "status": "IN_PROGRESS",
        },
        "current_card": {
            "id": "PVTI_lADOBjmjsc4ApgHnzgq-8Gc",
            "issue_number": 101,
            "status": "IN_PROGRESS",
        },
        "active_card_id": "PVTI_lADOBjmjsc4ApgHnzgq-8Gc",
        "active_sessions": {},
        "performer_stage": None,
        "phase": None,
    }

    resolution = detect_wedged_state(state)

    assert resolution == WedgeResolution.RELEASED
    # Pin released — next cycle's eligibility filter is free to re-pick
    assert state["active_card"] is None
    assert state["current_card"] is None
    assert state["active_card_id"] is None


def test_fr020_repeated_wedges_eventually_block() -> None:
    """If the same card wedges 3+ times in 24h, the orchestrator promotes
    to BLOCKED instead of releasing — operator gets notified rather than
    silently letting the wedge loop forever."""
    state = {
        "active_card": {"id": "PVTI_PROBLEM", "title": "card"},
        "current_card": {"id": "PVTI_PROBLEM", "title": "card"},
        "active_sessions": {},
        "performer_stage": None,
        "phase": None,
        "wedge_count_window": {},
    }

    # First 3 wedges (= threshold) — release the pin each time
    for _ in range(3):
        result = detect_wedged_state(state, wedge_block_threshold=3)
        assert result == WedgeResolution.RELEASED
        # Re-establish the wedge for the next iteration
        state["active_card"] = {"id": "PVTI_PROBLEM", "title": "card"}
        state["current_card"] = {"id": "PVTI_PROBLEM", "title": "card"}
        state["phase"] = None

    # 4th wedge (> threshold) → promoted to BLOCKED
    result = detect_wedged_state(state, wedge_block_threshold=3)
    assert result == WedgeResolution.BLOCKED
    assert state["phase"] == "blocked"
    # Pin retained so operator can investigate without the card flapping
    assert state["active_card"] is not None
