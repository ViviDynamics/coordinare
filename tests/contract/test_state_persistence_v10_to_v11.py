"""Schema v10 → v11 migration contract test (spec 096 T002).

Asserts (data-model.md §4; FR-010):
- ``CURRENT_SCHEMA_VERSION == 11`` and ``MIN_SUPPORTED_SCHEMA_VERSION`` unchanged.
- A synthetic v10 snapshot (no top-level ``last_known_main_sha``, no per-card
  ``last_rebase_attempt``) loads cleanly and migrates to v11 with both at their
  safe defaults (``None``) — so the first post-upgrade reconciliation treats live
  main as the comparison point and heals via the proactive trigger.
- A populated ``last_known_main_sha`` (top-level) and per-card
  ``last_rebase_attempt`` round-trip through ``model_dump(mode="json")`` →
  ``model_validate`` byte-identically, so the rebase baseline + anti-thrash marker
  survive a daemon restart.
- Both fields carry only SHAs / branch names / outcome strings — never secrets.
"""
from __future__ import annotations

from datetime import UTC, datetime

from coordinare.state_store import (
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    PersistedSession,
    WorkflowSnapshot,
)


def test_current_schema_version_is_at_least_11() -> None:
    """Spec 096 bumped the snapshot schema to v11 (last_known_main_sha +
    per-card last_rebase_attempt). Later specs bump it further (123 → v12), so
    this only asserts v11's fields are still supported, not that v11 is current.
    """
    assert CURRENT_SCHEMA_VERSION >= 11


def test_min_supported_unchanged() -> None:
    assert MIN_SUPPORTED_SCHEMA_VERSION == 1


def test_v10_snapshot_loads_into_v11_with_defaults() -> None:
    v10_shape = {
        "schema_version": 10,
        "snapshot_at": datetime.now(UTC).isoformat(),
        "phase": "idle",
    }
    snap = WorkflowSnapshot.model_validate(v10_shape)
    assert snap.last_known_main_sha is None

    sess = PersistedSession.model_validate({"card_id": "PVTI_X"})
    assert sess.last_rebase_attempt is None


def test_last_known_main_sha_round_trips() -> None:
    snap = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC),
        phase="monitoring_pr",
        last_known_main_sha="a" * 40,
    )
    re_loaded = WorkflowSnapshot.model_validate(snap.model_dump(mode="json"))
    assert re_loaded.last_known_main_sha == "a" * 40


def test_last_rebase_attempt_round_trips() -> None:
    session = PersistedSession(
        card_id="PVTI_X",
        last_rebase_attempt={
            "main_sha": "a" * 40,
            "head_sha": "b" * 40,
            "outcome": "blocked",
        },
    )
    re_loaded = PersistedSession.model_validate(session.model_dump(mode="json"))
    assert re_loaded.last_rebase_attempt == session.last_rebase_attempt
    assert set(re_loaded.last_rebase_attempt) == {"main_sha", "head_sha", "outcome"}
