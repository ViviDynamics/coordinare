"""128: pure evaluator that classifies an outstanding human CHANGES_REQUESTED
review as FRESH / STALE_ADDRESSED / STALE_UNADDRESSED.

No I/O — takes already-fetched review + thread data + the current PR head and
returns a classification. Encodes FR-001 (staleness), FR-002 (addressed) and
FR-007 (fresh unchanged) from specs/128-stale-review-handling.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from coordinare.models.review import (
    Review,
    ReviewerType,
    ReviewState,
    ReviewThread,
    StalenessClass,
)


@dataclass(frozen=True)
class StalenessConfig:
    """Threshold for treating a change-request as stale. The default is the
    safe one (research Decision 6): stale only when the review's commit differs
    from head AND at least one later commit exists — never a review on head."""

    min_commits_behind: int = 1
    min_hours_behind: float | None = None  # optional extra guard; None = disabled


@dataclass(frozen=True)
class StalenessResult:
    classification: StalenessClass
    reason: str


def _threads_for_review(review_id: str, threads: list[ReviewThread]) -> list[ReviewThread]:
    """Threads attributed to this review. When GitHub did not attribute any
    thread (all ``review_id`` None), fall back to the full set — conservative:
    an unresolved thread anywhere keeps the review UNADDRESSED."""
    attributed = [t for t in threads if t.review_id == review_id]
    if attributed:
        return attributed
    if any(t.review_id for t in threads):
        # Attribution exists but none point at this review → treat as body-only.
        return []
    return list(threads)


def classify_review_staleness(
    review: Review,
    head_oid: str,
    commits_behind: int,
    threads: list[ReviewThread] | None = None,
    config: StalenessConfig | None = None,
    now: datetime | None = None,
) -> StalenessResult:
    """Classify a single outstanding review.

    Only a HUMAN ``CHANGES_REQUESTED`` review can be stale-handled; anything
    else returns FRESH (the caller's existing path applies). ``commits_behind``
    is the number of commits between the review's commit and the current head.
    """
    cfg = config or StalenessConfig()
    threads = threads or []

    if review.author_type != ReviewerType.HUMAN or review.state != ReviewState.CHANGES_REQUESTED:
        return StalenessResult(StalenessClass.FRESH, "not a human change-request")

    # Unknown provenance → never auto-advance (FR-010 safe default).
    if not review.commit_oid or not head_oid:
        return StalenessResult(StalenessClass.FRESH, "review commit or head unknown")

    if review.commit_oid == head_oid:
        return StalenessResult(StalenessClass.FRESH, "review is on the current head")

    if commits_behind < max(1, cfg.min_commits_behind):
        return StalenessResult(
            StalenessClass.FRESH,
            f"only {commits_behind} commit(s) behind (< {cfg.min_commits_behind})",
        )

    if cfg.min_hours_behind is not None:
        submitted = review.submitted_at
        ref = now or datetime.now(UTC)
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=UTC)
        hours = (ref - submitted).total_seconds() / 3600.0
        if hours < cfg.min_hours_behind:
            return StalenessResult(
                StalenessClass.FRESH,
                f"only {hours:.1f}h old (< {cfg.min_hours_behind}h)",
            )

    # Stale by commit. Now decide addressed vs unaddressed.
    own_threads = _threads_for_review(review.id, threads)
    if own_threads:
        if all(t.is_resolved for t in own_threads):
            return StalenessResult(
                StalenessClass.STALE_ADDRESSED,
                f"{commits_behind} commit(s) behind; all {len(own_threads)} thread(s) resolved",
            )
        unresolved = sum(1 for t in own_threads if not t.is_resolved)
        return StalenessResult(
            StalenessClass.STALE_UNADDRESSED,
            f"{commits_behind} commit(s) behind; {unresolved} thread(s) still open",
        )

    # Body-only review (no attributed threads) + new commits landed → addressed.
    return StalenessResult(
        StalenessClass.STALE_ADDRESSED,
        f"body-only change-request {commits_behind} commit(s) behind; new work landed since",
    )
