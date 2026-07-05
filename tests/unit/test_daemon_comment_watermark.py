"""Spec 125 US4 — issue-comment dedup + stage verdicts survive restart (W1-W3).

The comment router is per-card (it reads the active card's linked issue), so
the watermark persists on PersistedSession and round-trips through
_persist_active_sessions → snapshot → session restore.

Contract: specs/125-stage-verdict-memory/contracts/skip-decision.md (W rows)
"""
from __future__ import annotations

from coordinare.daemon import _persist_active_sessions


def test_comment_watermark_persists_per_card() -> None:
    live = {
        "card-1": {
            "processed_issue_comment_ids": {3001, 3002, 3005},
            "last_issue_comment_id": 3005,
        },
    }

    out = _persist_active_sessions(live)

    assert out["card-1"].processed_issue_comment_ids == [3001, 3002, 3005]
    assert out["card-1"].last_issue_comment_id == 3005


def test_comment_ids_bounded_to_largest_2000() -> None:
    live = {
        "card-1": {
            "processed_issue_comment_ids": set(range(1, 2501)),
            "last_issue_comment_id": 2500,
        },
    }

    out = _persist_active_sessions(live)

    ids = out["card-1"].processed_issue_comment_ids
    assert len(ids) == 2000
    assert min(ids) == 501  # oldest 500 evicted; numerically largest retained
    assert max(ids) == 2500


def test_comment_ids_reject_bools_and_junk() -> None:
    live = {
        "card-1": {
            # 99.5 is a float with no int twin: it must be rejected, NOT
            # truncated to 99 (int(99.5) would corrupt the watermark).
            "processed_issue_comment_ids": [True, "junk", None, 42, 99.5, 7],
            "last_issue_comment_id": True,  # bool must not coerce to 1
        },
    }

    out = _persist_active_sessions(live)

    assert out["card-1"].processed_issue_comment_ids == [7, 42]
    assert 99 not in out["card-1"].processed_issue_comment_ids
    assert out["card-1"].last_issue_comment_id is None


def test_missing_watermark_defaults_empty() -> None:
    out = _persist_active_sessions({"card-1": {}})
    assert out["card-1"].processed_issue_comment_ids == []
    assert out["card-1"].last_issue_comment_id is None


def test_stage_verdicts_persist_and_corrupt_slots_drop() -> None:
    """Persist-side mirror of the contract-test load tolerance: good slots
    persist, malformed ones drop (bad slot == no slot == dispatch)."""
    live = {
        "card-1": {
            "stage_verdicts": {
                "reviewing": {
                    "head_sha": "abc123",
                    "verdict": "approved",
                    "recorded_at": "2026-07-04T10:00:00+00:00",
                },
                "qa": {"head_sha": "", "verdict": "qa_passed", "recorded_at": "t"},
                "security": "not-a-dict",
            },
        },
    }

    out = _persist_active_sessions(live)

    verdicts = out["card-1"].stage_verdicts
    assert set(verdicts.keys()) == {"reviewing"}
    assert verdicts["reviewing"].head_sha == "abc123"
    assert verdicts["reviewing"].verdict == "approved"
