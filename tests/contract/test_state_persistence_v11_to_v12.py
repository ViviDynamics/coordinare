"""Schema v11 → v12 migration contract test (spec 123 T023).

Asserts (data-model.md; FR-006/FR-009/FR-010):
- ``CURRENT_SCHEMA_VERSION == 12`` and ``MIN_SUPPORTED_SCHEMA_VERSION`` unchanged.
- A synthetic v11 snapshot (no per-card content_feedback_cycles /
  transient_error_cycles / assessor_open_questions) loads cleanly and migrates
  to v12 with the split-budget counters at 0 and the Q&A carry-forward empty.
- A legacy ``feedback_cycle_count`` on a persisted session migrates into
  ``content_feedback_cycles`` (FR-009) so an in-flight card's content budget
  survives the upgrade.
- The three new fields round-trip through ``model_dump(mode="json")`` →
  ``model_validate`` so the split budget + assessor Q&A survive a daemon restart.
"""
from __future__ import annotations

from datetime import UTC, datetime

from coordinare.state_store import (
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    PersistedSession,
    WorkflowSnapshot,
)


def test_current_schema_version_at_least_12() -> None:
    """Spec 123 bumped the snapshot schema to v12 (split bounce budget +
    assessor Q&A carryover). Later bumps (e.g. 124 → v13) keep the v12 fields,
    so this migration contract only requires the current version be >= 12."""
    assert CURRENT_SCHEMA_VERSION >= 12


def test_min_supported_unchanged() -> None:
    assert MIN_SUPPORTED_SCHEMA_VERSION == 1


def test_v11_snapshot_loads_into_v12_with_defaults() -> None:
    v11_shape = {
        "schema_version": 11,
        "snapshot_at": datetime.now(UTC).isoformat(),
        "phase": "idle",
    }
    snap = WorkflowSnapshot.model_validate(v11_shape)
    assert snap is not None

    sess = PersistedSession.model_validate({"card_id": "PVTI_X"})
    assert sess.content_feedback_cycles == 0
    assert sess.transient_error_cycles == 0
    assert sess.assessor_open_questions == []


def test_legacy_feedback_cycle_count_migrates_to_content_counter() -> None:
    """FR-009: a persisted session carrying only the legacy feedback_cycle_count
    seeds content_feedback_cycles on load."""
    sess = PersistedSession.model_validate(
        {"card_id": "PVTI_X", "feedback_cycle_count": 4}
    )
    assert sess.content_feedback_cycles == 4
    assert sess.transient_error_cycles == 0


def test_split_budget_and_qa_round_trip() -> None:
    session = PersistedSession(
        card_id="PVTI_X",
        content_feedback_cycles=3,
        transient_error_cycles=2,
        assessor_open_questions=[{"question": "Use OAuth?", "answer": "Yes"}],
    )
    re_loaded = PersistedSession.model_validate(session.model_dump(mode="json"))
    assert re_loaded.content_feedback_cycles == 3
    assert re_loaded.transient_error_cycles == 2
    assert re_loaded.assessor_open_questions == [
        {"question": "Use OAuth?", "answer": "Yes"}
    ]
