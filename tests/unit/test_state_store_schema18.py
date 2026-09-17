"""166 (schema v19): the assessor's structured assessment persists per card
and survives a restart; older snapshots load with None."""
from __future__ import annotations

from coordinare.daemon import _persist_active_sessions
from coordinare.state_store import (
    CURRENT_SCHEMA_VERSION,
    PersistedSession,
    WorkflowSnapshot,
)

_ASSESSMENT = {
    "ready": True,
    "goal": "Add deliverable categories to the time entry form",
    "expected_behavior": "Users can select from a dropdown of categories",
    "out_of_scope": ["Category management UI"],
    "questions": [],
    "assumptions": [],
    "criteria": [
        {
            "surface": "/time_entries/new",
            "action": "open",
            "expected": "category select visible",
            "kind": "functional",
        },
    ],
    "criteria_source": "card",
    "clarifications": [],
    "assessment_hash": "deadbeef",
}


def test_schema_version_is_24() -> None:
    assert CURRENT_SCHEMA_VERSION == 24  # 343: + workflow step trail


def test_new_assessment_field_defaults_to_none() -> None:
    s = PersistedSession(card_id="c1")
    assert s.assessment is None


def test_a_v17_snapshot_loads_with_assessment_none() -> None:
    snap = WorkflowSnapshot.model_validate(
        {
            "schema_version": 17,
            "snapshot_at": "2026-09-06T12:00:00+00:00",
            "phase": "idle",
            "active_sessions": {"c1": {"card_id": "c1", "performer_stage": "assessing"}},
        },
    )
    sess = snap.active_sessions["c1"]
    assert sess.assessment is None


def test_assessment_persists_from_the_live_session() -> None:
    live = {
        "c1": {
            "assessment": _ASSESSMENT,
        },
    }
    out = _persist_active_sessions(live)
    assert out["c1"].assessment == _ASSESSMENT


def test_a_corrupt_assessment_record_drops_to_none_not_a_save_failure() -> None:
    """A corrupt assessment (missing goal or ready field) loads as None."""
    out = _persist_active_sessions({"c1": {"assessment": {}}})  # empty dict
    assert out["c1"].assessment is None
    out2 = _persist_active_sessions({"c1": {"assessment": {"goal": "test"}}})  # missing ready
    assert out2["c1"].assessment is None
    out3 = _persist_active_sessions({"c1": {"assessment": {"ready": True}}})  # missing goal
    assert out3["c1"].assessment is None
    out4 = _persist_active_sessions({"c1": {"assessment": "garbage"}})
    assert out4["c1"].assessment is None


def test_round_trip_through_a_snapshot_keeps_assessment() -> None:
    from datetime import UTC, datetime

    sess = PersistedSession(
        card_id="c1",
        assessment=_ASSESSMENT,
    )
    snap = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC), phase="idle", active_sessions={"c1": sess},
    )
    reloaded = WorkflowSnapshot.model_validate_json(snap.model_dump_json())
    got = reloaded.active_sessions["c1"]
    assert got.assessment == _ASSESSMENT
