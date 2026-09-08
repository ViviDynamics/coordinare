"""Advocate intake (spec 173): which open issues this run will consider.

"Already handled" is read from the issue's own labels and nowhere else. The
service this replaces kept a set of processed ids in memory, absent from every
snapshot, so a restart re-scanned every open issue and only the GitHub label
stopped a duplicate reply. Reading the label directly makes the durable thing
the only thing.
"""
from __future__ import annotations

from performer.workflows.advocate.models import IssueCandidate


def is_handled(labels: list[str], handled_label: str, escalation_label: str) -> bool:
    """True when this issue already carries one of the advocate's own labels.

    An escalated issue counts as handled: a human owns it, and answering it
    again would talk over them.
    """
    marks = {label for label in (handled_label, escalation_label) if label}
    return bool(marks & set(labels))


def select_candidates(
    issues: list[dict],
    *,
    handled_label: str,
    escalation_label: str,
) -> list[IssueCandidate]:
    """The unhandled open issues, in the order the listing returned them."""
    candidates: list[IssueCandidate] = []
    for issue in issues:
        labels = [str(x) for x in (issue.get("labels") or [])]
        if is_handled(labels, handled_label, escalation_label):
            continue
        issue_id = str(issue.get("id") or "")
        if not issue_id:
            # An issue with no node id cannot be labelled or commented on, so
            # acting on it would produce a reply we could never mark as sent.
            continue
        candidates.append(IssueCandidate(
            issue_id=issue_id,
            number=int(issue.get("number") or 0),
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or ""),
            url=str(issue.get("url") or ""),
            labels=labels,
        ))
    return candidates
