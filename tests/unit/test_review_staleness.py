"""128: unit tests for the pure staleness evaluator (FR-001/002/007)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from coordinare.models.review import (
    Review,
    ReviewerType,
    ReviewState,
    ReviewThread,
    StalenessClass,
)
from coordinare.services.review_staleness import (
    StalenessConfig,
    classify_review_staleness,
)


def _cr(commit: str = "old", rid: str = "R1", submitted: datetime | None = None) -> Review:
    return Review(
        id=rid,
        author_login="jason",
        author_type=ReviewerType.HUMAN,
        state=ReviewState.CHANGES_REQUESTED,
        commit_oid=commit,
        submitted_at=submitted or datetime.now(UTC),
    )


def test_fresh_when_review_on_head() -> None:
    r = _cr(commit="HEAD")
    res = classify_review_staleness(r, head_oid="HEAD", commits_behind=0)
    assert res.classification is StalenessClass.FRESH


def test_fresh_when_within_commit_threshold() -> None:
    # commit differs but 0 commits behind counted → not stale
    r = _cr(commit="old")
    res = classify_review_staleness(r, head_oid="HEAD", commits_behind=0)
    assert res.classification is StalenessClass.FRESH


def test_fresh_when_commit_unknown() -> None:
    r = _cr(commit="")
    res = classify_review_staleness(r, head_oid="HEAD", commits_behind=9)
    assert res.classification is StalenessClass.FRESH


def test_fresh_when_not_human_change_request() -> None:
    r = Review(
        id="R1", author_login="bot", author_type=ReviewerType.TRUSTED_BOT,
        state=ReviewState.CHANGES_REQUESTED, commit_oid="old",
    )
    assert classify_review_staleness(r, "HEAD", 9).classification is StalenessClass.FRESH
    r2 = Review(
        id="R2", author_login="jason", author_type=ReviewerType.HUMAN,
        state=ReviewState.APPROVED, commit_oid="old",
    )
    assert classify_review_staleness(r2, "HEAD", 9).classification is StalenessClass.FRESH


def test_stale_addressed_all_threads_resolved() -> None:
    r = _cr(commit="old", rid="R1")
    threads = [
        ReviewThread(id="T1", is_resolved=True, review_id="R1"),
        ReviewThread(id="T2", is_resolved=True, review_id="R1"),
    ]
    res = classify_review_staleness(r, "HEAD", commits_behind=12, threads=threads)
    assert res.classification is StalenessClass.STALE_ADDRESSED


def test_stale_unaddressed_some_threads_open() -> None:
    r = _cr(commit="old", rid="R1")
    threads = [
        ReviewThread(id="T1", is_resolved=True, review_id="R1"),
        ReviewThread(id="T2", is_resolved=False, review_id="R1"),
    ]
    res = classify_review_staleness(r, "HEAD", commits_behind=12, threads=threads)
    assert res.classification is StalenessClass.STALE_UNADDRESSED


def test_stale_addressed_body_only_new_commits() -> None:
    # #111 shape: behind head, no threads, new commits since → addressed.
    r = _cr(commit="old", rid="R1")
    res = classify_review_staleness(r, "HEAD", commits_behind=30, threads=[])
    assert res.classification is StalenessClass.STALE_ADDRESSED


def test_body_only_ignores_other_reviews_threads() -> None:
    # Threads exist but none attributed to THIS review → treat as body-only.
    r = _cr(commit="old", rid="R1")
    other = [ReviewThread(id="T9", is_resolved=False, review_id="R2")]
    res = classify_review_staleness(r, "HEAD", commits_behind=5, threads=other)
    assert res.classification is StalenessClass.STALE_ADDRESSED


def test_unattributed_threads_are_conservative() -> None:
    # No review_id attribution anywhere + an open thread → keep UNADDRESSED.
    r = _cr(commit="old", rid="R1")
    threads = [ReviewThread(id="T1", is_resolved=False, review_id=None)]
    res = classify_review_staleness(r, "HEAD", commits_behind=5, threads=threads)
    assert res.classification is StalenessClass.STALE_UNADDRESSED


def test_commit_threshold_boundary() -> None:
    r = _cr(commit="old")
    cfg = StalenessConfig(min_commits_behind=3)
    assert (
        classify_review_staleness(r, "HEAD", commits_behind=2, threads=[], config=cfg).classification
        is StalenessClass.FRESH
    )
    assert (
        classify_review_staleness(r, "HEAD", commits_behind=3, threads=[], config=cfg).classification
        is StalenessClass.STALE_ADDRESSED
    )


def test_hours_behind_guard() -> None:
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    recent = _cr(commit="old", submitted=now - timedelta(hours=1))
    cfg = StalenessConfig(min_commits_behind=1, min_hours_behind=24.0)
    # commit-stale but too recent → FRESH
    assert (
        classify_review_staleness(recent, "HEAD", 5, threads=[], config=cfg, now=now).classification
        is StalenessClass.FRESH
    )
    old = _cr(commit="old", submitted=now - timedelta(hours=48))
    assert (
        classify_review_staleness(old, "HEAD", 5, threads=[], config=cfg, now=now).classification
        is StalenessClass.STALE_ADDRESSED
    )
