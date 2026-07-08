"""Schema v14 → v15 migration contract test (spec 126 T002).

NOTE: renumbered to v15 on rebase — spec 124 took v13 and spec 125 took v14,
so the 126 terminal-success-floor fields (feedback ledger + origin SHA + no-op
retry counter) are v15.

Asserts (contracts/state-schema-v14.md — the 126 contract doc, numbering
predates the rebase):
- ``CURRENT_SCHEMA_VERSION == 15``; ``MIN_SUPPORTED_SCHEMA_VERSION`` unchanged.
- v14 sessions load with ``feedback_ledger == []``, ``feedback_origin_sha is
  None``, ``noop_success_retries == 0``.
- ``FeedbackItemRecord`` rejects extra keys and out-of-vocabulary
  disposition/round_status values; malformed ledger entries drop on load.
- All ledger fields round-trip losslessly.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.state_store import (
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    FeedbackItemRecord,
    PersistedSession,
)

_ITEM = {
    "id": "fb-1",
    "raiser": "reviewing",
    "origin_sha": "abc123",
    "body_digest": "fix the margin",
    "disposition": "open",
    "dispute_reason": "",
    "re_raised": False,
    "round_status": "current",
}


def test_current_schema_version_is_16() -> None:
    """Spec 128 bumps the snapshot schema to v16 (top-level + per-card
    surfaced_stale_reviews stale-review dedup marker), superseding the v15 pin
    (spec 126, terminal-success floors — still present, just no longer current)."""
    assert CURRENT_SCHEMA_VERSION == 16


def test_old_session_loads_with_surfaced_stale_reviews_default() -> None:
    """128: a pre-v16 session loads with an empty surfaced_stale_reviews."""
    sess = PersistedSession.model_validate({"card_id": "PVTI_X"})
    assert sess.surfaced_stale_reviews == {}


def test_min_supported_unchanged() -> None:
    assert MIN_SUPPORTED_SCHEMA_VERSION == 1


def test_old_session_loads_with_v15_defaults() -> None:
    sess = PersistedSession.model_validate({"card_id": "PVTI_X"})
    assert sess.feedback_ledger == []
    assert sess.feedback_origin_sha is None
    assert sess.noop_success_retries == 0


def test_feedback_item_record_rejects_bad_shapes() -> None:
    with pytest.raises(ValidationError):
        FeedbackItemRecord(**{**_ITEM, "bogus": 1})
    with pytest.raises(ValidationError):
        FeedbackItemRecord(**{**_ITEM, "disposition": "maybe"})
    with pytest.raises(ValidationError):
        FeedbackItemRecord(**{**_ITEM, "round_status": "ancient"})
    with pytest.raises(ValidationError):
        FeedbackItemRecord(**{**_ITEM, "id": ""})


def test_malformed_ledger_entries_drop_not_fatal() -> None:
    sess = PersistedSession.model_validate(
        {
            "card_id": "PVTI_X",
            "feedback_ledger": [
                dict(_ITEM),
                {**_ITEM, "id": "fb-2", "disposition": "nonsense"},
                "not-a-dict",
            ],
        }
    )
    assert [r.id for r in sess.feedback_ledger] == ["fb-1"]


def test_feedback_ledger_round_trips() -> None:
    session = PersistedSession(
        card_id="PVTI_X",
        feedback_ledger=[
            FeedbackItemRecord(**_ITEM),
            FeedbackItemRecord(
                **{
                    **_ITEM,
                    "id": "fb-2",
                    "raiser": "ci",
                    "disposition": "disputed",
                    "dispute_reason": "check is flaky",
                    "re_raised": True,
                    "round_status": "previous",
                }
            ),
        ],
        feedback_origin_sha="abc123",
        noop_success_retries=1,
    )
    re_loaded = PersistedSession.model_validate(session.model_dump(mode="json"))
    assert [r.id for r in re_loaded.feedback_ledger] == ["fb-1", "fb-2"]
    assert re_loaded.feedback_ledger[1].dispute_reason == "check is flaky"
    assert re_loaded.feedback_ledger[1].re_raised is True
    assert re_loaded.feedback_origin_sha == "abc123"
    assert re_loaded.noop_success_retries == 1
