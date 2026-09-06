"""165 (schema v17): the architect blueprint and the documenter side run
persist per card and survive a restart; older snapshots load with None."""
from __future__ import annotations

from coordinare.daemon import _persist_active_sessions
from coordinare.state_store import (
    DocumentingSideRun,
    PersistedSession,
    WorkflowSnapshot,
)

_BLUEPRINT = {
    "summary": "Add deliverable categories",
    "milestones": [{"goal": "migration", "scope": ["db/migrate"], "done_when": "table exists"}],
    "criteria": [{"surface": "/time_entries/new", "action": "open", "expected": "category select", "kind": "functional"}],
    "docs": [],
    "size": "small",
    "blueprint_hash": "abc123",
    "created_at": "2026-09-06T12:00:00+00:00",
}


def test_new_fields_default_to_none_so_v16_sessions_load_unchanged() -> None:
    s = PersistedSession(card_id="c1")
    assert s.blueprint is None
    assert s.documenting_side is None


def test_a_v16_snapshot_loads_with_both_fields_none() -> None:
    snap = WorkflowSnapshot.model_validate(
        {
            "schema_version": 16,
            "snapshot_at": "2026-09-06T12:00:00+00:00",
            "phase": "idle",
            "active_sessions": {"c1": {"card_id": "c1", "performer_stage": "implementing"}},
        }
    )
    sess = snap.active_sessions["c1"]
    assert sess.blueprint is None and sess.documenting_side is None


def test_blueprint_and_side_run_persist_from_the_live_session() -> None:
    live = {
        "c1": {
            "blueprint": _BLUEPRINT,
            "documenting_side": {
                "status": "running",
                "blueprint_hash": "abc123",
                "dispatched_at": "2026-09-06T12:05:00+00:00",
                "session_id": "s-9",
            },
        }
    }
    out = _persist_active_sessions(live)
    assert out["c1"].blueprint == _BLUEPRINT
    side = out["c1"].documenting_side
    assert isinstance(side, DocumentingSideRun)
    assert side.status == "running" and side.blueprint_hash == "abc123" and side.session_id == "s-9"


def test_a_corrupt_side_run_record_drops_to_none_not_a_save_failure() -> None:
    out = _persist_active_sessions({"c1": {"documenting_side": {"status": "running"}}})  # no hash
    assert out["c1"].documenting_side is None
    out2 = _persist_active_sessions({"c1": {"documenting_side": "garbage", "blueprint": "not-a-dict"}})
    assert out2["c1"].documenting_side is None and out2["c1"].blueprint is None


def test_side_run_status_is_constrained() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DocumentingSideRun(status="exploded", blueprint_hash="x")


def test_round_trip_through_a_snapshot_keeps_both_fields() -> None:
    sess = PersistedSession(
        card_id="c1",
        blueprint=_BLUEPRINT,
        documenting_side=DocumentingSideRun(status="done", blueprint_hash="abc123", head_sha="deadbeef"),
    )
    from datetime import UTC, datetime

    snap = WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="idle", active_sessions={"c1": sess})
    reloaded = WorkflowSnapshot.model_validate_json(snap.model_dump_json())
    got = reloaded.active_sessions["c1"]
    assert got.blueprint == _BLUEPRINT
    assert got.documenting_side is not None and got.documenting_side.head_sha == "deadbeef"
