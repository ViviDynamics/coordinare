from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

# Only stages explicitly permitted to write after approval belong here.
# Reviewing, QA, closing review and implementing cannot inherit an approval.
_POST_APPROVAL_PUSH_MARKERS = {"documenting": frozenset({"docs_committed"})}


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


async def carried_approval_ids(
    state: CoordinareState, github: Any, reviews: list[dict[str, object]],
    *, pr_url: str, head_sha: str,
) -> set[int]:
    """#99: carry approval only over proven daemon commits from permitted stages.

    The compare service proves ancestry and completeness. This policy then
    checks every author's GitHub login and stage provenance. Any uncertainty
    leaves the approval stale so the closer continues waiting.
    """
    if not reviews or not pr_url or not head_sha:
        return set()
    carried: set[int] = set()
    checked: dict[str, bool] = {}
    for review in reviews:
        approved_sha = str(review.get("commit_oid") or "").strip()
        if not approved_sha or approved_sha == head_sha:
            continue
        if approved_sha not in checked:
            checked[approved_sha] = False
            try:
                logins = {login.strip().lower() for login in await github.self_logins() if login.strip()}
                if not logins:
                    continue
                entries = await github.compare_commits(pr_url, approved_sha, head_sha)
                if not isinstance(entries, list) or not entries:
                    continue
                commits = {c["sha"]: c for c in entries}
                if head_sha not in commits or len(commits) != len(entries):
                    continue
                own_authors = all(
                    isinstance(c.get("author"), dict)
                    and str(c["author"].get("login") or "").strip().lower() in logins
                    for c in entries
                )
                checked[approved_sha] = own_authors and set(commits) <= _recorded_commits(state, commits, approved_sha)
            except Exception as exc:
                logger.warning("monitor_pr.approval_carryover_unverified", error_type=type(exc).__name__)
        if checked[approved_sha]:
            carried.add(id(review))
    return carried
