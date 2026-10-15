from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity
from coordinare.services.documentation_paths import is_documentation_path

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

CARRYOVER_CACHE_SECONDS = 900.0
DISMISSED_DOCS_REASON = "approval_dismissed_by_daemon_docs_push"
DISMISSED_DOCS_MESSAGE = (
    "The documenter's docs-only commit dismissed the approval; re-approve to merge, "
    "or disable 'dismiss stale reviews' for this repo"
)

# Only the documenter can write after approval. Other stages cannot inherit it.
_POST_APPROVAL_PUSH_MARKERS = {"documenting": frozenset({"docs_committed"})}


def review_rules_known(rules: dict[str, bool]) -> bool:
    """Carry-over needs a positive reading of both push-related review rules."""
    return all(isinstance(rules.get(key), bool) for key in (
        "dismiss_stale_reviews_on_push", "require_extra_approval_for_unattributed_changes",
    ))


def _recorded_commits(
    state: CoordinareState, commits: dict[str, dict[str, Any]], approved_sha: str,
) -> set[str]:
    """Match commits to successful permitted writers, never to the current stage.

    Old records identify only their exact settled SHA. A reported starting SHA
    also establishes the run's linear range. Merge commits or incomplete ranges
    cannot attribute any additional commits to that writer.
    """
    records = [
        record
        for stage, record in (state.get("stage_verdicts") or {}).items()
        if stage in _POST_APPROVAL_PUSH_MARKERS
        and record.get("verdict") in _POST_APPROVAL_PUSH_MARKERS[stage]
    ]
    side = state.get("documenting_side")
    if isinstance(side, dict) and side.get("status") == "done":
        records.append(side)
    covered: set[str] = set()
    for record in records:
        end = record.get("head_sha")
        if not isinstance(end, str) or end not in commits:
            continue
        before = record.get("head_before")
        if before == end:
            continue
        covered.add(end)
        if not isinstance(before, str) or not before:
            continue
        cursor = end
        segment: set[str] = set()
        while cursor in commits and cursor not in segment and cursor != before:
            segment.add(cursor)
            parents = commits[cursor].get("parents")
            if not isinstance(parents, list) or len(parents) != 1 or not isinstance(parents[0], dict):
                break
            parent = parents[0].get("sha")
            if not isinstance(parent, str):
                break
            cursor = parent
        if cursor == before and (before == approved_sha or before in commits):
            covered.update(segment)
    return covered


async def _verify_documenter_range(
    state: CoordinareState, github: Any, *, pr_url: str, approved_sha: str, head_sha: str,
) -> bool:
    logins = {login.strip().lower() for login in await github.self_logins() if login.strip()}
    if not logins:
        return False
    entries = await github.compare_commits(pr_url, approved_sha, head_sha)
    if not isinstance(entries, list) or not entries:
        return False
    commits = {c["sha"]: c for c in entries}
    if head_sha not in commits or len(commits) != len(entries):
        return False
    own_authors = all(
        isinstance(c.get("author"), dict)
        and str(c["author"].get("login") or "").strip().lower() in logins
        for c in entries
    )
    if not own_authors or not set(commits) <= _recorded_commits(state, commits, approved_sha):
        return False
    files = await github.compare_changed_files(pr_url, approved_sha, head_sha)
    return isinstance(files, list) and bool(files) and all(is_documentation_path(path) for path in files)


async def carried_approval_ids(
    state: CoordinareState, github: Any, reviews: list[dict[str, object]],
    *, pr_url: str, head_sha: str,
) -> set[int]:
    """Prove daemon authorship, documenter provenance and a complete docs-only range.

    Results are cached per approval/head pair on the card, including unreadable
    results. They expire so a transient API failure can recover. Only the current
    head and candidate approvals are retained. A cached result never overrides a
    withdrawn review or the target branch's review rules.
    """
    if not reviews or not pr_url or not head_sha or not review_rules_known(state.get("merge_review_rules") or {}):
        return set()
    approved_shas = {str(review.get("commit_oid") or "").strip() for review in reviews}
    pairs = {f"{approved}|{head_sha}" for approved in approved_shas}
    cache = state.get("approval_carryover_cache") or {}
    cache = {pair: entry for pair, entry in cache.items() if pair in pairs}
    state["approval_carryover_cache"] = cache
    carried: set[int] = set()
    for review in reviews:
        approved_sha = str(review.get("commit_oid") or "").strip()
        if not approved_sha or approved_sha == head_sha:
            continue
        pair = f"{approved_sha}|{head_sha}"
        now = time.time()
        cached = cache.get(pair)
        if cached is None or not 0 <= now - cached[0] < CARRYOVER_CACHE_SECONDS:
            verified = False
            try:
                verified = await _verify_documenter_range(
                    state, github, pr_url=pr_url, approved_sha=approved_sha, head_sha=head_sha,
                )
            except Exception as exc:
                if state.get("approval_carryover_unverified_head") != head_sha:
                    logger.warning(
                        "monitor_pr.approval_carryover_unverified", head=head_sha,
                        error_type=type(exc).__name__,
                    )
                    state["approval_carryover_unverified_head"] = head_sha
            cache[pair] = (now, verified)
        if cache[pair][1]:
            carried.add(id(review))
    return carried


async def surface_docs_approval_wait(
    state: CoordinareState, *, card_id: str, head_sha: str,
) -> None:
    """Explain the branch-rule hold on the card and once per head in the feed and notifications."""
    state["review_wait_detail"] = DISMISSED_DOCS_MESSAGE
    if state.get("approval_docs_notified_head") == head_sha:
        return
    state["approval_docs_notified_head"] = head_sha
    activity = state.get("activity_log")
    if activity is not None:
        activity.record(
            activity_type=DISMISSED_DOCS_REASON, card_id=card_id, stage="reviewing", text=DISMISSED_DOCS_MESSAGE,
        )
    notifier = state.get("notification_service")
    if notifier is not None:
        try:
            await notifier.dispatch(NotificationEvent(
                event_type=EventType.merge_review_required,
                severity=NotificationSeverity.warning,
                source="monitor_pr",
                payload={"card_id": card_id, "head": head_sha, "reason": DISMISSED_DOCS_REASON,
                         "message": DISMISSED_DOCS_MESSAGE},
                dedup_key=f"{DISMISSED_DOCS_REASON}:{card_id}:{head_sha}",
            ))
        except Exception as exc:
            logger.warning("monitor_pr.docs_approval_notify_failed", card_id=card_id, error_type=type(exc).__name__)
