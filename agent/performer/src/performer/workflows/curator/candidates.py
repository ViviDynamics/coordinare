"""Curator candidate selection (spec 173): rules, before any model call.

An issue already on the board is not a candidate, and the durable marker for
that is the board itself plus the curator's own labels, never coordinare memory.

416: exclusion is not only the curator's proposal label. The scan also
advances past the issues it previously declined (``skipped_label``) and the
ones triage escalated (``escalation_label``), and the remaining candidates are
taken oldest first so a perpetual stream of new issues cannot starve the old
ones -- the listing is newest-first, which is exactly backwards for a scan.
"""
from __future__ import annotations

from performer.workflows.curator.models import IssueCandidate


def select_candidates(
    issues: list[dict],
    *,
    curator_label: str,
    on_board_ids: set[str] | None = None,
    max_per_run: int = 5,
    excluded_labels: tuple[str, ...] | list[str] = (),
) -> list[IssueCandidate]:
    """Open issues that are not on the board, not proposed, not declined."""
    seen_on_board = on_board_ids or set()
    excluded = {label for label in (curator_label, *excluded_labels) if label}
    candidates: list[IssueCandidate] = []
    for issue in issues:
        issue_id = str(issue.get("id") or "")
        if not issue_id or issue_id in seen_on_board:
            continue
        labels = [str(x) for x in (issue.get("labels") or [])]
        if excluded & set(labels):
            continue
        candidates.append(IssueCandidate(
            issue_id=issue_id,
            number=int(issue.get("number") or 0),
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or ""),
            url=str(issue.get("url") or ""),
            created_at=str(issue.get("created_at") or ""),
            labels=labels,
        ))
    candidates.sort(key=lambda c: c.created_at or "9999")
    return candidates[:max(1, max_per_run)]
