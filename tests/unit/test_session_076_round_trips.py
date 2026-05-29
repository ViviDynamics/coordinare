"""Spec 076 round-trip tests for the 5 new PersistedSession / CardSession fields.

These tests guard the FR-019 (idle_timeout_retries), FR-016
(pr_artefacts_recorded_at audit timestamp), FR-024 (multi_pr_divergence),
FR-020 (wedge_count_window) and reconciliation_decisions_last_startup
fields against accidental removal from ``_SESSION_FIELDS`` — if any of
them stops round-tripping, the dispatcher-dedup invariants silently lose
the ability to survive a daemon restart.
"""
from __future__ import annotations

from datetime import UTC, datetime

from coordinare.graph.state import initial_state
from coordinare.session import (
    create_session_from_card,
    session_to_state,
    state_to_session,
)


def _sample_card(card_id: str = "PVTI_X", title: str = "Sample card") -> dict:
    return {
        "id": card_id,
        "title": title,
        "status": "IN_PROGRESS",
        "description": "Sample",
    }


def test_idle_timeout_retries_round_trips() -> None:
    """FR-019: per-(card_id, stage) idle-timeout retry counters MUST persist
    across daemon restart, else the qwen-stall path can launder away the
    counter and re-attempt forever."""
    record = {
        "card_id": "PVTI_X",
        "performer_stage": "implementing",
        "window_start_at": datetime(2026, 5, 28, 22, 0, 0, tzinfo=UTC).isoformat(),
        "attempt_count": 1,
        "last_at": datetime(2026, 5, 28, 22, 56, 47, tzinfo=UTC).isoformat(),
    }
    retries = {"PVTI_X:implementing": record}

    session = create_session_from_card(_sample_card())
    session["idle_timeout_retries"] = retries

    state = initial_state()
    session_to_state(session, state)
    assert state["idle_timeout_retries"] == retries

    recovered = state_to_session(state)
    assert recovered["idle_timeout_retries"] == retries


def test_pr_artefacts_recorded_at_round_trips() -> None:
    """FR-016: the audit timestamp proves PR fields were written-through
    after the last successful turn; without it we cannot detect the
    "successful turn forgotten" failure mode."""
    ts = datetime(2026, 5, 28, 22, 5, 50, tzinfo=UTC)

    session = create_session_from_card(_sample_card())
    session["pr_artefacts_recorded_at"] = ts

    state = initial_state()
    session_to_state(session, state)
    assert state["pr_artefacts_recorded_at"] == ts

    recovered = state_to_session(state)
    assert recovered["pr_artefacts_recorded_at"] == ts


def test_multi_pr_divergence_round_trips() -> None:
    """FR-024: when coordinare detects > 1 open PR for a card, the
    surfaced record MUST persist so the divergence-induced BLOCKED state
    is restored on restart and coordinare doesn't re-dispatch into the
    same divergent state."""
    divergence = {
        "card_id": "PVTI_X",
        "canonical_branch_prefix": "coordinare/PVTI_X/",
        "pr_numbers": [133, 148],
        "detected_at": datetime(2026, 5, 28, 22, 5, 47, tzinfo=UTC).isoformat(),
        "detected_at_trigger": "restart",
        "response": "card_blocked",
    }

    session = create_session_from_card(_sample_card())
    session["multi_pr_divergence"] = divergence

    state = initial_state()
    session_to_state(session, state)
    assert state["multi_pr_divergence"] == divergence

    recovered = state_to_session(state)
    assert recovered["multi_pr_divergence"] == divergence


def test_wedge_count_window_round_trips() -> None:
    """FR-020: per-card rolling window of wedge timestamps (data-model
    §8: ``dict[str, list[datetime]]``).  Restart MUST preserve so an
    operator restarting after each wedge cannot launder the counter
    and miss the BLOCKED promotion."""
    timestamps = {
        "PVTI_X": [
            datetime(2026, 5, 28, 21, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 28, 21, 30, 0, tzinfo=UTC),
        ],
        "PVTI_Y": [datetime(2026, 5, 28, 22, 0, 0, tzinfo=UTC)],
    }

    session = create_session_from_card(_sample_card())
    session["wedge_count_window"] = timestamps

    state = initial_state()
    session_to_state(session, state)
    assert state["wedge_count_window"] == timestamps

    recovered = state_to_session(state)
    assert recovered["wedge_count_window"] == timestamps


def test_reconciliation_decisions_last_startup_round_trips() -> None:
    """Reconciliation audit trail (data-model §8): tracks which decision
    was made for which card at the most recent startup.  Notification
    layer reads this to suppress duplicate `card_dispatched` events on
    ADOPTED outcomes."""
    decisions = {"PVTI_X": "adopted", "PVTI_Y": "fresh_dispatched"}

    session = create_session_from_card(_sample_card())
    session["reconciliation_decisions_last_startup"] = decisions

    state = initial_state()
    session_to_state(session, state)
    assert state["reconciliation_decisions_last_startup"] == decisions

    recovered = state_to_session(state)
    assert recovered["reconciliation_decisions_last_startup"] == decisions


def test_fresh_session_initialises_076_fields_to_safe_defaults() -> None:
    """A brand-new CardSession from create_session_from_card MUST have
    every 076 field defaulted (no None for collection-typed fields, no
    crashes on access)."""
    session = create_session_from_card(_sample_card())

    assert session["idle_timeout_retries"] == {}
    assert session["pr_artefacts_recorded_at"] is None
    assert session["multi_pr_divergence"] is None
    assert session["wedge_count_window"] == {}
    assert session["reconciliation_decisions_last_startup"] == {}
