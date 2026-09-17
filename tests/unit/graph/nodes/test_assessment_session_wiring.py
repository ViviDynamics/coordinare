"""166 (T010): the assessment field is wired through daemon, session, and state."""
from __future__ import annotations

from typing import Any

from coordinare.graph.state import initial_state
from coordinare.session import _SESSION_FIELDS, CardSession, session_to_state, state_to_session
from coordinare.state_store import WorkflowSnapshot

_ASSESSMENT = {
    "ready": True,
    "goal": "Add deliverable categories",
    "expected_behavior": "Users can select from a dropdown",
    "out_of_scope": [],
    "questions": [],
    "assumptions": [],
    "criteria": [{"surface": "/", "action": "open", "expected": "ok", "kind": "functional"}],
    "criteria_source": "card",
    "clarifications": [],
    "assessment_hash": "deadbeef",
}


def test_assessment_is_in_session_fields() -> None:
    """The assessment field is tracked in _SESSION_FIELDS for round-trip."""
    assert "assessment" in _SESSION_FIELDS


def test_assessment_in_card_session() -> None:
    """CardSession accepts assessment as a field."""
    sess = CardSession(
        card_id="c1",
        assessment=_ASSESSMENT,
    )
    assert sess["assessment"] == _ASSESSMENT


def test_assessment_in_initial_state() -> None:
    """graph/state.py initial_state has assessment as None."""
    state = initial_state()
    assert "assessment" in state
    assert state["assessment"] is None


def test_assessment_round_trips_through_session_to_state() -> None:
    """session_to_state projects assessment into the state dict."""
    sess = CardSession(
        card_id="c1",
        performer_stage="assessing",
        assessment=_ASSESSMENT,
    )
    state_dict: dict[str, Any] = {}
    session_to_state(sess, state_dict)
    assert state_dict.get("assessment") == _ASSESSMENT


def test_assessment_round_trips_through_state_to_session() -> None:
    """state_to_session projects assessment back into CardSession."""
    state = {
        "card_id": "c1",
        "performer_stage": "assessing",
        "assessment": _ASSESSMENT,
    }
    sess = state_to_session(state)
    assert sess["assessment"] == _ASSESSMENT


def test_daemon_save_and_restore_preserves_assessment() -> None:
    """Daemon's _persist_active_sessions and load flow preserve assessment."""
    from coordinare.daemon import _persist_active_sessions

    live = {
        "c1": {
            "card_id": "c1",
            "performer_stage": "architecting",
            "assessment": _ASSESSMENT,
        },
    }
    out = _persist_active_sessions(live)
    persisted_sess = out["c1"]
    assert persisted_sess.assessment == _ASSESSMENT

    # Round-trip through snapshot
    snap = WorkflowSnapshot.model_validate_json(
        WorkflowSnapshot(
            snapshot_at="2026-09-06T12:00:00+00:00",
            phase="idle",
            active_sessions=out,
        ).model_dump_json(),
    )
    restored_sess = snap.active_sessions["c1"]
    assert restored_sess.assessment == _ASSESSMENT
