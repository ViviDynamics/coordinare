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


def test_persona_scope_round_trips() -> None:
    """074 FR-011 regression: classify_scope writes persona_scope to flat state;
    if it's missing from _SESSION_FIELDS the daemon's fanout merge drops it and
    the next cycle re-classifies from scratch, losing the previous-cycle
    fallback chain in FR-006.  Mirrors the four existing round-trip regression
    tests fixed on 069/072/073."""
    scope = {
        "computed_at": "2026-05-27T18:30:00Z",
        "cycle_index": 7,
        "classifier_model": "anthropic_api:claude-haiku-4-5",
        "head_sha": "abc123",
        "files_summary": [{"path": "README.md", "added": 3, "removed": 1, "classes": ["docs"]}],
        "personas": {
            "reviewer": {"depth": "skim", "focus": "spot-check", "overrides": []},
            "security": {"depth": "skip", "focus": "no security paths", "overrides": []},
        },
    }
    session = create_session_from_card(_sample_card())
    session["persona_scope"] = scope

    state = initial_state()
    session_to_state(session, state)
    assert state["persona_scope"] == scope

    recovered = state_to_session(state)
    assert recovered["persona_scope"] == scope


def test_bounce_counter_round_trips() -> None:
    """075: monitor_performer increments bounce_counter[head_sha] on each
    failing-required-checks decision; if it's missing from _SESSION_FIELDS
    the gate forgets bounces between cycles and never escalates to
    needs_human_review.  Mirrors the persona_scope regression test."""
    session = create_session_from_card(_sample_card())
    session["bounce_counter"] = {"sha-abc": 2, "sha-def": 1}

    state = initial_state()
    session_to_state(session, state)
    assert state["bounce_counter"] == {"sha-abc": 2, "sha-def": 1}

    recovered = state_to_session(state)
    assert recovered["bounce_counter"] == {"sha-abc": 2, "sha-def": 1}


def test_inheritance_repair_counter_round_trips() -> None:
    """090-L3: monitor_performer increments inheritance_repair_counter[head_sha]
    at repair dispatch; if it's missing from _SESSION_FIELDS the per-head repair
    budget resets every cycle and the gate never escalates on exhaustion.
    Mirrors the bounce_counter regression test."""
    session = create_session_from_card(_sample_card())
    session["inheritance_repair_counter"] = {"a" * 40: 1, "b" * 40: 2}

    state = initial_state()
    session_to_state(session, state)
    assert state["inheritance_repair_counter"] == {"a" * 40: 1, "b" * 40: 2}

    recovered = state_to_session(state)
    assert recovered["inheritance_repair_counter"] == {"a" * 40: 1, "b" * 40: 2}


def test_repair_audit_round_trips() -> None:
    """090-L3: the append-only repair_audit trail (dispatch/static_guard/reviewer/
    acceptance/rejection/escalation records) must round-trip through
    _SESSION_FIELDS or the daemon's fanout merge drops the audit history between
    cycles (FR-023)."""
    audit = [
        {
            "head_sha": "a" * 40,
            "attempt": 1,
            "kind": "dispatch",
            "is_safe": None,
            "flagged_patterns": [],
            "detail": "Dispatched autonomous baseline-repair attempt 1/1.",
            "decided_at": "2026-06-14T00:00:00Z",
        },
        {
            "head_sha": "a" * 40,
            "attempt": 1,
            "kind": "escalation",
            "is_safe": None,
            "flagged_patterns": [],
            "detail": "Autonomous baseline-repair budget exhausted.",
            "decided_at": "2026-06-14T00:01:00Z",
        },
    ]
    session = create_session_from_card(_sample_card())
    session["repair_audit"] = audit

    state = initial_state()
    session_to_state(session, state)
    assert state["repair_audit"] == audit

    recovered = state_to_session(state)
    assert recovered["repair_audit"] == audit


def test_latest_ci_gate_decision_round_trips() -> None:
    """075: monitor_performer writes the most recent CIGateDecision to
    flat state for notify.py to consume on the next cycle.  Must round-trip
    through _SESSION_FIELDS or notify.py sees None and skips the rollup
    comment."""
    decision = {
        "verdict": "bounce",
        "head_sha": "sha-abc",
        "resolver_source": "persona_check_map",
        "required": ["lint", "unit-tests"],
        "failing": ["lint"],
    }
    session = create_session_from_card(_sample_card())
    session["latest_ci_gate_decision"] = decision

    state = initial_state()
    session_to_state(session, state)
    assert state["latest_ci_gate_decision"] == decision

    recovered = state_to_session(state)
    assert recovered["latest_ci_gate_decision"] == decision


def test_ci_gate_advisory_failures_round_trips() -> None:
    """075: advisory failures (failed checks NOT in the resolved required
    set) surface to notify.py through this field; must round-trip through
    _SESSION_FIELDS or reviewers won't see non-required failures on a
    PASS verdict."""
    advisory = [{"name": "flaky-perf", "conclusion": "failure"}]
    session = create_session_from_card(_sample_card())
    session["ci_gate_advisory_failures"] = advisory

    state = initial_state()
    session_to_state(session, state)
    assert state["ci_gate_advisory_failures"] == advisory

    recovered = state_to_session(state)
    assert recovered["ci_gate_advisory_failures"] == advisory


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
        "persona_scope",
        "bounce_counter",
        "local_fix_counter",
        "inheritance_repair_counter",
        "repair_audit",
        "review_empty_retry_count",
        "last_progress_at",
        "last_progress_fingerprint",
        "latest_ci_gate_decision",
        "ci_gate_rollup_signature",
        "ci_gate_advisory_failures",
        "env_blocked",
    }
    for field in _SESSION_FIELDS:
        if field not in optional_in_initial:
            assert field in state, f"{field} missing from initial_state"
