"""Schema v6 → v7 migration contract test (spec 076 T014).

Asserts:
- ``CURRENT_SCHEMA_VERSION == 7``
- A synthetic v6 snapshot loads cleanly into the v7 PersistedSession with
  all five new fields defaulted (FR-019, FR-016, FR-024, FR-020, audit).
- A fresh v7 PersistedSession round-trips back through ``model_dump`` →
  ``model_validate`` with identical field values.
"""
from __future__ import annotations

from datetime import UTC, datetime

from coordinare.state_store import (
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    PersistedSession,
)


def test_current_schema_version_is_7() -> None:
    """Spec 076 bumps the snapshot schema."""
    assert CURRENT_SCHEMA_VERSION == 7


def test_min_supported_unchanged() -> None:
    """v1 snapshots must still load — we never regress legacy compatibility."""
    assert MIN_SUPPORTED_SCHEMA_VERSION == 1


def test_v6_shaped_snapshot_loads_into_v7_with_defaults() -> None:
    """A snapshot written before 076 (no 076 fields present) MUST load
    cleanly and populate every new field with its safe empty default."""
    v6_shape = {
        "card_id": "PVTI_X",
        "performer_stage": "implementing",
        "phase": "monitoring_performer",
        # 075 fields explicitly set; 076 fields intentionally omitted
        "bounce_counter": {"sha-abc": 1},
        "ci_gate_rollup_signature": "deadbeef12345678",
    }

    session = PersistedSession.model_validate(v6_shape)

    # 076 fields defaulted
    assert session.idle_timeout_retries == {}
    assert session.pr_artefacts_recorded_at is None
    assert session.multi_pr_divergence is None
    assert session.wedge_count_window == {}
    assert session.reconciliation_decisions_last_startup == {}

    # 075 fields preserved
    assert session.bounce_counter == {"sha-abc": 1}
    assert session.ci_gate_rollup_signature == "deadbeef12345678"


def test_v7_legacy_flat_wedge_window_coerced_to_empty_dict() -> None:
    """Round-3 review regression: an earlier 076 dev build wrote
    ``wedge_count_window`` as a flat ``list[datetime]``.  The v7
    Pydantic model declares it as ``dict[str, list[datetime]]``, so
    loading that legacy snapshot would normally raise ValidationError
    and prevent the daemon from starting.  The field validator coerces
    the legacy list to an empty dict on load."""
    legacy_shape = {
        "card_id": "PVTI_X",
        "performer_stage": "implementing",
        # Legacy flat-list shape (pre-spec-finalisation 076 dev build)
        "wedge_count_window": [
            "2026-05-28T22:00:00+00:00",
            "2026-05-28T22:30:00+00:00",
        ],
    }
    # MUST not raise ValidationError
    session = PersistedSession.model_validate(legacy_shape)
    assert session.wedge_count_window == {}
    assert session.card_id == "PVTI_X"


def test_v7_fields_round_trip_through_serialisation() -> None:
    """A populated v7 session, dumped and re-loaded, MUST preserve every
    field with byte-identical values."""
    session = PersistedSession(
        card_id="PVTI_X",
        performer_stage="implementing",
        phase="monitoring_performer",
        idle_timeout_retries={
            "PVTI_X:implementing": {
                "card_id": "PVTI_X",
                "performer_stage": "implementing",
                "window_start_at": "2026-05-28T22:00:00+00:00",
                "attempt_count": 1,
                "last_at": "2026-05-28T22:56:47+00:00",
            }
        },
        pr_artefacts_recorded_at=datetime(2026, 5, 28, 22, 5, 50, tzinfo=UTC),
        multi_pr_divergence={
            "card_id": "PVTI_X",
            "canonical_branch_prefix": "coordinare/PVTI_X/",
            "pr_numbers": [133, 148],
            "detected_at": "2026-05-28T22:05:47+00:00",
            "detected_at_trigger": "restart",
            "response": "card_blocked",
        },
        wedge_count_window={"PVTI_X": [datetime(2026, 5, 28, 21, 0, 0, tzinfo=UTC)]},
        reconciliation_decisions_last_startup={"PVTI_X": "adopted"},
    )

    blob = session.model_dump(mode="json")
    re_loaded = PersistedSession.model_validate(blob)

    assert re_loaded.idle_timeout_retries == session.idle_timeout_retries
    assert re_loaded.pr_artefacts_recorded_at == session.pr_artefacts_recorded_at
    assert re_loaded.multi_pr_divergence == session.multi_pr_divergence
    assert re_loaded.wedge_count_window == session.wedge_count_window
    assert (
        re_loaded.reconciliation_decisions_last_startup
        == session.reconciliation_decisions_last_startup
    )
