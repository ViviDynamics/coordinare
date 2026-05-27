"""Tests for 035: Multi-card parallelism — CardSession abstraction."""
from __future__ import annotations

from coordinare.graph.state import initial_state
from coordinare.session import (
    _SESSION_FIELDS,
    create_session_from_card,
    session_to_state,
    state_to_session,
)

# ---------------------------------------------------------------------------
# CardSession creation
# ---------------------------------------------------------------------------


def _sample_card(card_id: str = "PVI_1", title: str = "Fix bug") -> dict:
    return {
        "id": card_id,
        "title": title,
        "status": "TODO",
        "description": "Some description",
    }


def test_create_session_from_card_basic() -> None:
    card = _sample_card()
    session = create_session_from_card(card)

    assert session["current_card"] is card
    assert session["performer_stage"] == "implementing"
    assert session["phase"] == "dispatching"
    assert session["agent_dispatch"] == {}
    assert session["workspace_path"] is None
    assert session["performer_events"] == []
    assert session["card_tokens_total"] == 0
    assert session["card_cost_estimate"] == 0.0
    assert session["card_budget_alert_sent"] is False
    assert session["open_questions"] == []
    assert session["card_clarifications"] == []
    assert session["relay_feedback"] == []
    assert session["system_error_count"] == 0
    assert session["system_error_notified"] is False
    assert session["commit_summary"] is None
    assert session["agent_health_status"] is None
    assert session["pending_override"] is None
    assert session["requirements_changed"] is False
    assert session["requirements_changed_details"] == {}


def test_create_session_preserves_card_data() -> None:
    card = _sample_card("PVI_42", "Implement feature X")
    session = create_session_from_card(card)
    assert session["current_card"]["id"] == "PVI_42"
    assert session["current_card"]["title"] == "Implement feature X"


# ---------------------------------------------------------------------------
# session_to_state and state_to_session round-trip
# ---------------------------------------------------------------------------


def test_session_to_state_copies_fields() -> None:
    state = initial_state()
    card = _sample_card()
    session = create_session_from_card(card)
    session["performer_stage"] = "reviewing"
    session["card_tokens_total"] = 42

    session_to_state(session, state)

    assert state["current_card"] is card
    assert state["performer_stage"] == "reviewing"
    assert state["card_tokens_total"] == 42
    assert state["phase"] == "dispatching"


def test_state_to_session_copies_fields() -> None:
    state = initial_state()
    state["current_card"] = _sample_card("PVI_99")
    state["performer_stage"] = "qa"
    state["card_tokens_total"] = 100
    state["phase"] = "monitoring_agent"

    session = state_to_session(state)

    assert session["current_card"]["id"] == "PVI_99"
    assert session["performer_stage"] == "qa"
    assert session["card_tokens_total"] == 100
    assert session["phase"] == "monitoring_agent"


def test_round_trip_session_state_session() -> None:
    """Create session -> copy to state -> copy back -> values match."""
    card = _sample_card("PVI_7")
    original = create_session_from_card(card)
    original["performer_stage"] = "security"
    original["card_tokens_total"] = 500
    original["card_cost_estimate"] = 1.5

    state = initial_state()
    session_to_state(original, state)
    recovered = state_to_session(state)

    for field in _SESSION_FIELDS:
        if field in original:
            assert recovered.get(field) == original.get(field), f"Mismatch on {field}"


def test_last_blocked_slack_delivered_at_round_trips() -> None:
    """Regression: notify.py stamps this watermark on the session dict; if
    it's missing from _SESSION_FIELDS the daemon's cycle merge drops it and
    the Slack cooldown gate falls through, producing repeated blocked posts."""
    from datetime import UTC, datetime

    stamp = datetime.now(UTC)
    session = create_session_from_card(_sample_card())
    session["last_blocked_slack_delivered_at"] = stamp

    state = initial_state()
    session_to_state(session, state)
    assert state["last_blocked_slack_delivered_at"] == stamp

    recovered = state_to_session(state)
    assert recovered["last_blocked_slack_delivered_at"] == stamp


def test_lifecycle_completed_at_round_trips() -> None:
    """Regression: monitor_performer._advance_stage stamps this cutoff when a
    card hands off to monitoring_pr; if it's missing from _SESSION_FIELDS the
    daemon's fanout merge drops it.  The next cycle's monitor_pr runs with
    cutoff=None, accepts pre-handoff automation reviews as actionable human
    feedback, and classify_human_feedback's keyword fallback re-dispatches the
    card to implementing — kicking a cleared card back out of human review."""
    from datetime import UTC, datetime

    stamp = datetime.now(UTC)
    session = create_session_from_card(_sample_card())
    session["lifecycle_completed_at"] = stamp

    state = initial_state()
    session_to_state(session, state)
    assert state["lifecycle_completed_at"] == stamp

    recovered = state_to_session(state)
    assert recovered["lifecycle_completed_at"] == stamp


def test_processed_review_ids_round_trips() -> None:
    """Regression: classify_human_feedback writes processed_review_ids to flat
    state; if it's missing from _SESSION_FIELDS the same review re-classifies
    every cycle until the next persistence snapshot."""
    session = create_session_from_card(_sample_card())
    session["processed_review_ids"] = {"PRR_abc", "PRR_xyz"}

    state = initial_state()
    session_to_state(session, state)
    assert state["processed_review_ids"] == {"PRR_abc", "PRR_xyz"}

    recovered = state_to_session(state)
    assert recovered["processed_review_ids"] == {"PRR_abc", "PRR_xyz"}


def test_round_trip_preserves_non_session_fields() -> None:
    """session_to_state must not clobber non-session fields on state."""
    state = initial_state()
    state["error_count"] = 5
    state["board_snapshot"] = {"TODO": ["PVI_1"]}

    session = create_session_from_card(_sample_card())
    session_to_state(session, state)

    # Non-session fields must be untouched
    assert state["error_count"] == 5
    assert state["board_snapshot"] == {"TODO": ["PVI_1"]}


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


def test_two_sessions_are_independent() -> None:
    """Mutating one session does not affect another."""
    session_a = create_session_from_card(_sample_card("A"))
    session_b = create_session_from_card(_sample_card("B"))

    session_a["performer_stage"] = "reviewing"
    session_a["card_tokens_total"] = 200

    assert session_b["performer_stage"] == "implementing"
    assert session_b["card_tokens_total"] == 0


def test_state_copy_isolation() -> None:
    """After copying session A to state, session B is unaffected."""
    state = initial_state()
    session_a = create_session_from_card(_sample_card("A"))
    session_b = create_session_from_card(_sample_card("B"))

    session_a["performer_stage"] = "reviewing"
    session_to_state(session_a, state)

    # session_b must still be fresh
    assert session_b["performer_stage"] == "implementing"
    assert state["performer_stage"] == "reviewing"


# ---------------------------------------------------------------------------
# active_sessions on initial_state
# ---------------------------------------------------------------------------


def test_initial_state_has_active_sessions() -> None:
    state = initial_state()
    assert "active_sessions" in state
    assert state["active_sessions"] == {}


# ---------------------------------------------------------------------------
# _SESSION_FIELDS completeness
# ---------------------------------------------------------------------------


def test_session_fields_match_card_session_keys() -> None:
    """All keys in _SESSION_FIELDS should be valid CardSession keys."""
    session = create_session_from_card(_sample_card())
    for field in _SESSION_FIELDS:
        assert field in session, f"{field} missing from CardSession"


def test_session_fields_all_present_in_initial_state_or_coordinare_state() -> None:
    """Most session fields should also exist in initial_state (except
    those that are optional/unset in initial state)."""
    state = initial_state()
    # Fields that may not appear in initial_state but are on CoordinareState
    optional_in_initial = {
        "agent_dispatch_at", "performer_metrics", "system_error_last_at",
        "system_error_reason", "commit_summary", "agent_health_status",
        "agent_dispatch", "current_card", "pending_reviews",
        "pending_override", "requirements_changed",
        "requirements_changed_details", "system_error_notified",
        "phase_entered_at", "last_blocked_notified_at",
        "last_blocked_slack_delivered_at",
        "head_at_dispatch", "head_at_last_turn",
        "clarifications_count_at_dispatch",
        "dispatched_notified_stages",
        "lifecycle_completed_at",
        "processed_review_ids",
    }
    for field in _SESSION_FIELDS:
        if field not in optional_in_initial:
            assert field in state, f"{field} missing from initial_state"
