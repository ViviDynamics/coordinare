"""Schema v9 → v10 migration contract test (spec 095 T010).

Asserts (data-model.md; FR-006):
- ``CURRENT_SCHEMA_VERSION == 10`` and ``MIN_SUPPORTED_SCHEMA_VERSION`` unchanged.
- A synthetic v9 snapshot (no ``env_blocked``) loads cleanly and migrates to v10
  with ``env_blocked is None`` — so the first ENV_BLOCKED hold after an upgrade
  notifies once and repopulates the dedup state.
- A fresh v10 session with a populated ``env_blocked`` round-trips through
  ``model_dump(mode="json")`` → ``model_validate`` byte-identically, so a still-
  active infra block does NOT re-notify the operator after a daemon restart.
- ``env_blocked`` carries only infra/check identifiers — never secret values.
"""
from __future__ import annotations

from coordinare.state_store import (
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    PersistedSession,
)


def test_current_schema_version_is_10() -> None:
    """Spec 095 bumps the snapshot schema to v10 (per-card env_blocked)."""
    assert CURRENT_SCHEMA_VERSION == 10


def test_min_supported_unchanged() -> None:
    """v1 snapshots must still load — we never regress legacy compatibility."""
    assert MIN_SUPPORTED_SCHEMA_VERSION == 1


def test_v9_shaped_snapshot_loads_into_v10_with_env_blocked_none() -> None:
    """A snapshot written before 095 (no ``env_blocked``) MUST load cleanly and
    default ``env_blocked`` to None (re-notify once after upgrade)."""
    v9_shape = {
        "card_id": "PVTI_X",
        "performer_stage": "implementing",
        "phase": "monitoring_performer",
        "bounce_counter": {"sha-abc": 1},
        "inheritance_repair_counter": {"sha-abc": 1},
        "ci_gate_rollup_signature": "deadbeef12345678",
    }

    session = PersistedSession.model_validate(v9_shape)

    assert session.env_blocked is None
    # Pre-095 fields preserved
    assert session.bounce_counter == {"sha-abc": 1}
    assert session.inheritance_repair_counter == {"sha-abc": 1}


def test_env_blocked_round_trips_through_serialisation() -> None:
    """A populated ``env_blocked`` (head_sha + pattern_id + cause + action) dumped
    and re-loaded MUST preserve every field byte-identically, so a still-active
    block survives a restart without re-notifying (FR-006)."""
    session = PersistedSession(
        card_id="PVTI_X",
        performer_stage="implementing",
        phase="monitoring_performer",
        env_blocked={
            "head_sha": "a" * 40,
            "pattern_id": "artifact_storage_quota",
            "cause": "CI artifact-storage quota exhausted",
            "action": "Raise the Actions storage budget or clear old artifacts",
        },
    )

    blob = session.model_dump(mode="json")
    re_loaded = PersistedSession.model_validate(blob)

    assert re_loaded.env_blocked == session.env_blocked
    # Carries only identifiers — no secret-shaped keys
    assert set(re_loaded.env_blocked) == {"head_sha", "pattern_id", "cause", "action"}
