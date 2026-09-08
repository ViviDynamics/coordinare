"""Curator gate (spec 173): a judgement must quote the issue it judges.

Same discipline as the closer's: a reason the issue's own words do not support
is not a reason. It also gives the human who reviews the backlog something to
check the judgement against, which is the point of proposing rather than
promoting.
"""
from __future__ import annotations

import re

from performer.workflows.curator.models import IssueCandidate, SelectionJudgement


def _fold(text: str) -> str:
    """Collapse whitespace so a quote is not rejected over reflowed lines."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def quote_supported(judgement: SelectionJudgement, issue: IssueCandidate) -> bool:
    """True when the quote appears in this issue's own title or body."""
    needle = _fold(judgement.quote)
    if not needle:
        return False
    return needle in _fold(issue.text)


def accept_judgement(
    judgement: SelectionJudgement,
    by_id: dict[str, IssueCandidate],
) -> tuple[bool, str]:
    """Whether this judgement may be acted on, and why not when it may not."""
    issue = by_id.get(judgement.issue_id)
    if issue is None:
        return False, "judgement names an issue this run did not send"
    if not judgement.qualifies:
        return True, ""
    if not quote_supported(judgement, issue):
        return False, "the stated reason quotes text that is not in the issue"
    return True, ""
