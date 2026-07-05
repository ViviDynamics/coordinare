"""Schema v13 → v14 migration contract test (spec 125 T002).

NOTE: renumbered to v14 on rebase — spec 124 (OpenWiki) landed v13 first, so
the 125 stage-verdict/comment-watermark fields are v14.

Asserts (contracts/state-schema-v13.md; FR-001/FR-010/FR-011/FR-012):
- ``CURRENT_SCHEMA_VERSION == 14`` and ``MIN_SUPPORTED_SCHEMA_VERSION`` unchanged.
- A synthetic v12 snapshot loads cleanly with ``stage_verdicts == {}`` per
  session, ``processed_issue_comment_ids == []`` and
  ``last_issue_comment_id is None``.
- ``StageVerdict`` rejects extra keys and empty ``head_sha``/``verdict``.
- A corrupted ``stage_verdicts`` entry is dropped on session load (bad slot ==
  no slot: dispatch), never a crashed daemon.
- All v14 fields round-trip losslessly through ``model_dump(mode="json")`` →
  ``model_validate``.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coordinare.state_store import (
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    PersistedSession,
    StageVerdict,
    WorkflowSnapshot,
)


def test_current_schema_version_is_14() -> None:
    """Spec 125 bumps the snapshot schema to v14 (stage verdict memory +
    persisted issue-comment watermark)."""
    assert CURRENT_SCHEMA_VERSION == 14


def test_min_supported_unchanged() -> None:
    assert MIN_SUPPORTED_SCHEMA_VERSION == 1


def test_v13_snapshot_loads_into_v14_with_defaults() -> None:
    # A genuine v13 snapshot (spec 124's schema, with an OpenWiki env_cache
    # entry present) must load in a v14 daemon with the 125 fields defaulted —
    # this is the direct v13 → v14 upgrade path.
    v13_shape = {
        "schema_version": 13,
        "snapshot_at": datetime.now(UTC).isoformat(),
        "phase": "idle",
        "env_cache": {
            "sym": {
                "symphony_name": "sym",
                "sanitised_name": "sym",
                "cache_dir": "/tmp/c",
                "wiki_initialized": True,
            }
        },
    }
    snap = WorkflowSnapshot.model_validate(v13_shape)
    assert snap is not None

    sess = PersistedSession.model_validate({"card_id": "PVTI_X"})
    assert sess.stage_verdicts == {}
    assert sess.processed_issue_comment_ids == []
    assert sess.last_issue_comment_id is None


def test_stage_verdict_rejects_extra_keys_and_empty_fields() -> None:
    with pytest.raises(ValidationError):
        StageVerdict(head_sha="abc", verdict="approved", recorded_at="t", bogus=1)
    with pytest.raises(ValidationError):
        StageVerdict(head_sha="", verdict="approved", recorded_at="t")
    with pytest.raises(ValidationError):
        StageVerdict(head_sha="abc", verdict="", recorded_at="t")


def test_corrupt_stage_verdict_entry_dropped_not_fatal() -> None:
    """A malformed slot loads as no slot (dispatch), never a crashed load."""
    sess = PersistedSession.model_validate(
        {
            "card_id": "PVTI_X",
            "stage_verdicts": {
                "reviewing": {
                    "head_sha": "abc123",
                    "verdict": "approved",
                    "recorded_at": "2026-07-04T10:00:00+00:00",
                },
                "qa": {"head_sha": "", "verdict": "qa_passed", "recorded_at": "t"},
                "security": "not-a-dict",
            },
        }
    )
    assert set(sess.stage_verdicts.keys()) == {"reviewing"}
    assert sess.stage_verdicts["reviewing"].head_sha == "abc123"


def test_v14_fields_round_trip() -> None:
    session = PersistedSession(
        card_id="PVTI_X",
        stage_verdicts={
            "reviewing": StageVerdict(
                head_sha="abc123",
                verdict="approved",
                recorded_at="2026-07-04T10:00:00+00:00",
            ),
            "documenting": StageVerdict(
                head_sha="def456",
                verdict="docs_committed",
                recorded_at="2026-07-04T11:00:00+00:00",
            ),
        },
    )
    re_loaded = PersistedSession.model_validate(session.model_dump(mode="json"))
    assert re_loaded.stage_verdicts["reviewing"].head_sha == "abc123"
    assert re_loaded.stage_verdicts["reviewing"].verdict == "approved"
    assert re_loaded.stage_verdicts["documenting"].head_sha == "def456"


def test_comment_watermark_round_trips_per_card() -> None:
    """US4: the comment router is per-card (it reads the active card's linked
    issue), so the dedup watermark persists on PersistedSession."""
    session = PersistedSession(
        card_id="PVTI_X",
        processed_issue_comment_ids=[3, 1, 2],
        last_issue_comment_id=7,
    )
    re_loaded = PersistedSession.model_validate(session.model_dump(mode="json"))
    assert re_loaded.processed_issue_comment_ids == [3, 1, 2]
    assert re_loaded.last_issue_comment_id == 7
