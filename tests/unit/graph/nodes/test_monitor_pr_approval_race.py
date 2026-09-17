"""Spec 127 — approval/feedback race.

A human APPROVED review sharing an evaluation batch with unprocessed
actionable reviews (CHANGES_REQUESTED / COMMENTED from a human or trusted
bot) must defer the merge and relay the feedback; the approval is never
consumed and merges on a later evaluation once nothing actionable remains.
Within a batch, each reviewer's latest review supersedes their earlier ones.

Contract: specs/127-approval-feedback-race/contracts/review-routing.md
"""
from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state


def _review(
    review_id: str,
    login: str,
    review_state: str,
    submitted_at: str | None = None,
    body: str = "x",
) -> dict:
    return {
        "id": review_id,
        "author_login": login,
        "state": review_state,
        "body": body,
        "submitted_at": submitted_at,
        "comments": [],
    }


class _GitHubBatch:
    def __init__(self, reviews: list[dict]) -> None:
        self._reviews = reviews

    async def get_pr_reviews(self, pr_id: str) -> list[dict]:
        _ = pr_id
        return [dict(r) for r in self._reviews]


def _state(
    reviews: list[dict],
    processed: set[str] | None = None,
) -> dict:
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1", "id": "CARD_1"}
    state["human_reviewers"] = ["alice", "bob"]
    state["trusted_bot_reviewers"] = ["trusty[bot]"]
    state["github_service"] = _GitHubBatch(reviews)
    if processed is not None:
        state["processed_review_ids"] = set(processed)
    return state


# ---------------------------------------------------------------------------
# US1 — contract row 1: actionable feedback wins; approval deferred
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_with_change_request_defers_merge_and_relays() -> None:
    state = _state(
        [
            _review("R_A", "alice", "APPROVED", "2026-07-04T10:00:00Z"),
            _review("R_B", "bob", "CHANGES_REQUESTED", "2026-07-04T10:01:00Z"),
        ],
    )

    result = await monitor_pr(state)

    assert result["phase"] == "relay_feedback"
    pending_ids = [r["id"] for r in result["pending_reviews"]]
    assert pending_ids == ["R_B"]
    assert "R_A" not in pending_ids  # invariant I3: approval never consumed


@pytest.mark.asyncio
async def test_approval_with_trusted_bot_comment_defers_merge() -> None:
    state = _state(
        [
            _review("R_A", "alice", "APPROVED", "2026-07-04T10:00:00Z"),
            _review("R_T", "trusty[bot]", "COMMENTED", "2026-07-04T10:01:00Z"),
        ],
    )

    result = await monitor_pr(state)

    assert result["phase"] == "relay_feedback"
    assert [r["id"] for r in result["pending_reviews"]] == ["R_T"]


@pytest.mark.asyncio
async def test_deferral_emits_merge_deferred_event() -> None:
    state = _state(
        [
            _review("R_A", "alice", "APPROVED", "2026-07-04T10:00:00Z"),
            _review("R_B", "bob", "CHANGES_REQUESTED", "2026-07-04T10:01:00Z"),
        ],
    )

    with capture_logs() as logs:
        await monitor_pr(state)

    deferred = [e for e in logs if e.get("event") == "monitor_pr.merge_deferred"]
    assert len(deferred) == 1
    assert deferred[0]["approval_review_id"] == "R_A"
    assert deferred[0]["actionable_review_ids"] == ["R_B"]
    assert deferred[0]["card_id"] == "CARD_1"


@pytest.mark.asyncio
async def test_relay_without_approval_emits_no_deferral_event() -> None:
    state = _state([_review("R_B", "bob", "CHANGES_REQUESTED", "2026-07-04T10:01:00Z")])

    with capture_logs() as logs:
        result = await monitor_pr(state)

    assert result["phase"] == "relay_feedback"
    assert not [e for e in logs if e.get("event") == "monitor_pr.merge_deferred"]


# ---------------------------------------------------------------------------
# US1 — contract rows 2-3: unchanged baseline paths (invariants I1/I2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_only_batch_still_merges() -> None:
    state = _state([_review("R_A", "alice", "APPROVED", "2026-07-04T10:00:00Z")])

    result = await monitor_pr(state)

    assert result["phase"] == "merging"


@pytest.mark.asyncio
async def test_actionable_only_batch_still_relays() -> None:
    state = _state([_review("R_B", "bob", "CHANGES_REQUESTED", "2026-07-04T10:01:00Z")])

    result = await monitor_pr(state)

    assert result["phase"] == "relay_feedback"
    assert [r["id"] for r in result["pending_reviews"]] == ["R_B"]


@pytest.mark.asyncio
async def test_fully_processed_batch_keeps_monitoring() -> None:
    state = _state(
        [
            _review("R_A", "alice", "APPROVED", "2026-07-04T10:00:00Z"),
            _review("R_B", "bob", "CHANGES_REQUESTED", "2026-07-04T10:01:00Z"),
        ],
        processed={"R_A", "R_B"},
    )

    result = await monitor_pr(state)

    assert result["phase"] == "monitoring_pr"


# ---------------------------------------------------------------------------
# US1 — FR-003: deferred approval merges once feedback is processed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deferred_approval_merges_after_feedback_processed() -> None:
    reviews = [
        _review("R_A", "alice", "APPROVED", "2026-07-04T10:00:00Z"),
        _review("R_B", "bob", "CHANGES_REQUESTED", "2026-07-04T10:01:00Z"),
    ]

    # Cycle 1: coexisting actionable review defers the merge.
    state = _state(reviews)
    result1 = await monitor_pr(state)
    assert result1["phase"] == "relay_feedback"

    # classify_human_feedback marks the actionable review processed; the
    # approval's ID is untouched (it never rode pending_reviews).
    result1["processed_review_ids"] = {"R_B"}

    # Cycle 2: only the standing approval remains unprocessed -> merge,
    # with no human re-approval.
    result2 = await monitor_pr(result1)
    assert result2["phase"] == "merging"


# ---------------------------------------------------------------------------
# US2 — per-reviewer latest-state supersession (contract S1-S4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_reviewer_later_approval_supersedes_change_request() -> None:
    # S1: CR then later APPROVED from the same reviewer -> approval governs.
    state = _state(
        [
            _review("R_1", "alice", "CHANGES_REQUESTED", "2026-07-04T10:00:00Z"),
            _review("R_2", "alice", "APPROVED", "2026-07-04T10:05:00Z"),
        ],
    )

    result = await monitor_pr(state)

    assert result["phase"] == "merging"


@pytest.mark.asyncio
async def test_same_reviewer_later_change_request_supersedes_approval() -> None:
    # S2: APPROVED then later CR from the same reviewer -> change request
    # governs; no deferral event (no effective approval exists).
    state = _state(
        [
            _review("R_1", "alice", "APPROVED", "2026-07-04T10:00:00Z"),
            _review("R_2", "alice", "CHANGES_REQUESTED", "2026-07-04T10:05:00Z"),
        ],
    )

    with capture_logs() as logs:
        result = await monitor_pr(state)

    assert result["phase"] == "relay_feedback"
    assert [r["id"] for r in result["pending_reviews"]] == ["R_2"]
    assert not [e for e in logs if e.get("event") == "monitor_pr.merge_deferred"]


@pytest.mark.asyncio
async def test_same_reviewer_timestamp_tie_resolves_to_actionable() -> None:
    # S3: identical timestamps -> conservative rule, actionable governs.
    state = _state(
        [
            _review("R_1", "alice", "APPROVED", "2026-07-04T10:00:00Z"),
            _review("R_2", "alice", "CHANGES_REQUESTED", "2026-07-04T10:00:00Z"),
        ],
    )

    result = await monitor_pr(state)

    assert result["phase"] == "relay_feedback"


@pytest.mark.asyncio
async def test_same_reviewer_unparseable_timestamp_resolves_to_actionable() -> None:
    state = _state(
        [
            _review("R_1", "alice", "APPROVED", "not-a-timestamp"),
            _review("R_2", "alice", "CHANGES_REQUESTED", None),
        ],
    )

    result = await monitor_pr(state)

    assert result["phase"] == "relay_feedback"


@pytest.mark.asyncio
async def test_naive_and_aware_timestamps_compared_without_error() -> None:
    """Copilot review fix: a timezone-less ISO-8601 submitted_at parses naive;
    comparing it against a Z-suffixed (aware) timestamp must not raise
    TypeError. Naive is normalised to UTC, so the later review still governs."""
    state = _state(
        [
            # naive (no offset) earlier, aware later — different reviewer so
            # both stay effective and the aware/naive max() also runs.
            _review("R_1", "alice", "APPROVED", "2026-07-04T10:00:00"),
            _review("R_2", "bob", "APPROVED", "2026-07-04T11:00:00Z"),
        ],
    )

    result = await monitor_pr(state)

    # No TypeError; two human approvals, nothing actionable -> merging.
    assert result["phase"] == "merging"


@pytest.mark.asyncio
async def test_same_reviewer_naive_then_aware_later_supersedes() -> None:
    """Same reviewer, naive-earlier CR superseded by aware-later APPROVED —
    the mixed naive/aware comparison must resolve, not raise."""
    state = _state(
        [
            _review("R_1", "alice", "CHANGES_REQUESTED", "2026-07-04T10:00:00"),
            _review("R_2", "alice", "APPROVED", "2026-07-04T11:00:00Z"),
        ],
    )

    result = await monitor_pr(state)

    assert result["phase"] == "merging"


@pytest.mark.asyncio
async def test_same_reviewer_case_whitespace_variants_grouped() -> None:
    """Copilot review fix: grouping normalizes login (strip().lower()) like
    classify_reviewer, so "Alice" and "alice " are the SAME reviewer — the
    later APPROVED supersedes the earlier CHANGES_REQUESTED rather than the
    two escaping supersession as distinct groups (which would leave the CR
    standing and wrongly relay instead of merge)."""
    state = _state(
        [
            _review("R_1", "Alice", "CHANGES_REQUESTED", "2026-07-04T10:00:00Z"),
            _review("R_2", "alice ", "APPROVED", "2026-07-04T11:00:00Z"),
        ],
    )

    result = await monitor_pr(state)

    assert result["phase"] == "merging"


@pytest.mark.asyncio
async def test_null_author_login_reviews_are_never_grouped() -> None:
    """author_login=None (malformed API data) must not become the grouping
    key "None" — each such review stays effective, none silently dropped."""
    from coordinare.graph.nodes.monitor_pr import _latest_reviews_per_author

    effective = _latest_reviews_per_author(
        [
            {"id": "A", "author_login": None, "state": "CHANGES_REQUESTED"},
            {"id": "B", "author_login": None, "state": "COMMENTED"},
            {"id": "C", "author_login": "", "state": "COMMENTED"},
            {"id": "D", "author_login": "alice", "state": "APPROVED"},
        ],
    )

    assert [r["id"] for r in effective] == ["A", "B", "C", "D"]


@pytest.mark.asyncio
async def test_cross_reviewer_states_never_supersede() -> None:
    # S4: supersession applies within one reviewer only.
    state = _state(
        [
            _review("R_1", "alice", "APPROVED", "2026-07-04T10:05:00Z"),
            _review("R_2", "bob", "CHANGES_REQUESTED", "2026-07-04T10:00:00Z"),
        ],
    )

    with capture_logs() as logs:
        result = await monitor_pr(state)

    assert result["phase"] == "relay_feedback"
    assert [r["id"] for r in result["pending_reviews"]] == ["R_2"]
    assert [e for e in logs if e.get("event") == "monitor_pr.merge_deferred"]
