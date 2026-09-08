"""Curator candidate selection (spec 173): rules, before any model call.

An issue already on the board is not a candidate, and the durable marker for
that is the board itself plus the curator's own label, never coordinare memory.
"""
from __future__ import annotations

from performer.workflows.curator.models import IssueCandidate


def select_candidates(
    issues: list[dict],
    *,
    curator_label: str,
    on_board_ids: set[str] | None = None,
    max_per_run: int = 5,
) -> list[IssueCandidate]:
    """Open issues that are not on the board and not already proposed."""
    seen_on_board = on_board_ids or set()
    out: list[IssueCandidate] = []
    for issue in issues:
        issue_id = str(issue.get("id") or "")
        if not issue_id or issue_id in seen_on_board:
            continue
        labels = [str(x) for x in (issue.get("labels") or [])]
        if curator_label and curator_label in labels:
            continue
        out.append(IssueCandidate(
            issue_id=issue_id,
            number=int(issue.get("number") or 0),
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or ""),
            url=str(issue.get("url") or ""),
            labels=labels,
        ))
        if len(out) >= max(1, max_per_run):
            break
    return out
