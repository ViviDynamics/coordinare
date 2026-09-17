"""Spec 076 T077 (FR-015 regression) — PR artefacts survive daemon restart.

Reconstructs the second anomaly from today's incident: implementer
opened PR #148, but coordinare's state.active_card.pr_url stayed on PR
#133.  Asserts that after a successful turn updates the PR fields,
both state.active_card AND the active_sessions entry mirror the new
values — so a daemon restart loads the new PR from the snapshot, not
the stale one.
"""
from __future__ import annotations

from coordinare.graph.nodes.monitor_performer import _record_pr_artefacts
from coordinare.graph.state import initial_state
from coordinare.session import session_to_state, state_to_session


def test_pr_artefacts_recorded_survive_session_round_trip() -> None:
    """After ``_record_pr_artefacts`` mirrors the new PR to
    ``active_sessions[card_id].current_card``, the standard session ↔
    state round-trip MUST preserve every artefact field.  This is the
    direct precondition for surviving a daemon restart: the snapshot is
    built from active_sessions, not from current_card."""
    state = initial_state()
    card_id = "PVTI_TODAY"
    initial_card = {
        "id": card_id,
        "title": "Time tracking schema",
        "status": "IN_PROGRESS",
        "pr_url": "https://github.com/x/y/pull/133",  # stale PR (the bug)
        "pr_node_id": "PR_kwDO_133",
    }
    state["current_card"] = dict(initial_card)
    state["active_sessions"] = {
        card_id: {
            "current_card": dict(initial_card),
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
        },
    }

    # Implementer opens PR #148 and reports the new artefacts
    _record_pr_artefacts(
        state,
        {
            "pr_url": "https://github.com/x/y/pull/148",
            "pr_node_id": "PR_kwDO_148",
            "pr_number": 148,
            "head_sha": "2b6569b7520db31f41ab2aca829ecd020e03ff05",
            "pushed_branch": "coordinare/PVTI_TODAY/feature-time-tracking",
        },
    )

    # In-flight active_sessions reflects the NEW PR
    sess_card = state["active_sessions"][card_id]["current_card"]
    assert sess_card["pr_url"] == "https://github.com/x/y/pull/148"
    assert sess_card["pr_node_id"] == "PR_kwDO_148"
    assert sess_card["pr_number"] == 148
    # Audit timestamp captured
    assert state["active_sessions"][card_id]["pr_artefacts_recorded_at"] is not None

    # Simulate daemon restart: session → snapshot → fresh state → session
    sess_obj = state["active_sessions"][card_id]
    fresh_state = initial_state()
    session_to_state(sess_obj, fresh_state)  # snapshot write
    rehydrated = state_to_session(fresh_state)  # snapshot read on next boot

    # The new PR survived: a fresh daemon reads the v7 snapshot and the
    # rehydrated session carries the new PR identifiers, NOT the stale ones.
    assert rehydrated["current_card"]["pr_url"] == "https://github.com/x/y/pull/148"
    assert rehydrated["current_card"]["pr_number"] == 148
    assert rehydrated["current_card"]["pushed_branch"] == "coordinare/PVTI_TODAY/feature-time-tracking"
    assert rehydrated["pr_artefacts_recorded_at"] is not None
