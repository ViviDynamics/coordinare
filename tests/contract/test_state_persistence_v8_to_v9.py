"""Schema v8 → v9 migration contract test (spec 090 T008).

Asserts (data-model.md §9; FR-026, SC-009):
- ``CURRENT_SCHEMA_VERSION == 9`` and ``MIN_SUPPORTED_SCHEMA_VERSION`` unchanged.
- A synthetic v8 snapshot (no ``inheritance_repair_counter``, no ``repair_audit``)
  loads cleanly and migrates to v9 with ``inheritance_repair_counter == {}`` **and**
  ``repair_audit == []``.
- An empty counter means **zero attempts taken** (not unlimited) — the configured
  per-head budget still applies.
- A fresh v9 session, including a ``repair_audit`` populated with
  ``RepairDecisionRecord``s, round-trips through ``model_dump(mode="json")`` →
  ``model_validate`` with byte-identical field values.
"""
from __future__ import annotations

from coordinare.state_store import (
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    PersistedSession,
    RepairDecisionRecord,
)


def test_current_schema_version_is_9() -> None:
    """Spec 090 bumps the snapshot schema to v9 (inheritance_repair_counter + repair_audit)."""
    assert CURRENT_SCHEMA_VERSION == 9


def test_min_supported_unchanged() -> None:
    """v1 snapshots must still load — we never regress legacy compatibility."""
    assert MIN_SUPPORTED_SCHEMA_VERSION == 1


def test_v8_shaped_snapshot_loads_into_v9_with_defaults() -> None:
    """A snapshot written before 090 (no 090 fields present) MUST load cleanly and
    populate both new fields with their safe empty defaults (data-model §9)."""
    v8_shape = {
        "card_id": "PVTI_X",
        "performer_stage": "implementing",
        "phase": "monitoring_performer",
        # 075/089 fields explicitly set; 090 fields intentionally omitted
        "bounce_counter": {"sha-abc": 1},
        "local_fix_counter": {"sha-abc": 2},
        "ci_gate_rollup_signature": "deadbeef12345678",
    }

    session = PersistedSession.model_validate(v8_shape)

    # 090 fields defaulted to safe empty values
    assert session.inheritance_repair_counter == {}
    assert session.repair_audit == []

    # Pre-090 fields preserved
    assert session.bounce_counter == {"sha-abc": 1}
    assert session.local_fix_counter == {"sha-abc": 2}
    assert session.ci_gate_rollup_signature == "deadbeef12345678"


def test_empty_counter_means_zero_attempts_not_unlimited() -> None:
    """An absent/empty ``inheritance_repair_counter`` means zero attempts taken for
    any head — NOT 'unlimited'.  The configured ``max_repair_attempts_per_head``
    budget still applies (FR-026, SC-009)."""
    session = PersistedSession.model_validate({"card_id": "PVTI_X"})

    assert session.inheritance_repair_counter == {}
    # The read path is ``.get(head_sha, 0)`` — a never-attempted head reads 0.
    assert session.inheritance_repair_counter.get("any-head-sha", 0) == 0


def test_repair_decision_record_round_trips() -> None:
    """A RepairDecisionRecord of each kind round-trips through JSON serialisation."""
    record = RepairDecisionRecord(
        head_sha="sha-abc",
        attempt=1,
        kind="static_guard",
        is_safe=False,
        flagged_patterns=["removed assertion", "added @pytest.mark.skip"],
        detail="diff weakens tests",
        decided_at="2026-06-14T12:00:00+00:00",
    )
    re_loaded = RepairDecisionRecord.model_validate(record.model_dump(mode="json"))
    assert re_loaded == record


def test_v9_fields_round_trip_through_serialisation() -> None:
    """A populated v9 session — including a repair_audit with the full decision
    sequence (dispatch → static_guard + reviewer → acceptance/rejection →
    escalation) — dumped and re-loaded MUST preserve every field byte-identically."""
    session = PersistedSession(
        card_id="PVTI_X",
        performer_stage="implementing",
        phase="monitoring_performer",
        inheritance_repair_counter={"sha-abc": 1, "sha-def": 2},
        repair_audit=[
            RepairDecisionRecord(
                head_sha="sha-abc",
                attempt=1,
                kind="dispatch",
                decided_at="2026-06-14T12:00:00+00:00",
            ),
            RepairDecisionRecord(
                head_sha="sha-abc",
                attempt=1,
                kind="static_guard",
                is_safe=True,
                decided_at="2026-06-14T12:01:00+00:00",
            ),
            RepairDecisionRecord(
                head_sha="sha-abc",
                attempt=1,
                kind="reviewer",
                is_safe=True,
                decided_at="2026-06-14T12:02:00+00:00",
            ),
            RepairDecisionRecord(
                head_sha="sha-abc",
                attempt=1,
                kind="acceptance",
                detail="both guards cleared; landed as candidate",
                decided_at="2026-06-14T12:03:00+00:00",
            ),
            RepairDecisionRecord(
                head_sha="sha-def",
                attempt=2,
                kind="rejection",
                is_safe=False,
                flagged_patterns=["loosened comparison operator"],
                detail="static guard veto",
                decided_at="2026-06-14T12:10:00+00:00",
            ),
            RepairDecisionRecord(
                head_sha="sha-def",
                attempt=2,
                kind="escalation",
                detail="budget exhausted",
                decided_at="2026-06-14T12:11:00+00:00",
            ),
        ],
    )

    blob = session.model_dump(mode="json")
    re_loaded = PersistedSession.model_validate(blob)

    assert re_loaded.inheritance_repair_counter == session.inheritance_repair_counter
    assert re_loaded.repair_audit == session.repair_audit
    # Pre-090 fields untouched by the round-trip
    assert re_loaded.card_id == session.card_id
    assert re_loaded.performer_stage == session.performer_stage
