"""The curator's selection persona (spec 173).

This is the board-curation job that was wrongly attached to the advocate. It
belongs to a role that can actually do it.
"""
from __future__ import annotations

from performer.workflows.curator.models import IssueCandidate

JUDGE_PERSONA = """You decide which open issues are ready to become work on a project board.

For each issue, judge it against these criteria:
{criteria}

An issue qualifies only when it meets all of them. Be strict: a card that turns
out to be ambiguous or oversized costs far more than one left for a human to
triage, because it consumes a full implementation cycle before anyone notices.

For every issue return:
- qualifies: true or false
- reason: why, in one or two sentences
- quote: a short passage copied EXACTLY from that issue's title or body that
  supports your reason. Copy it character for character. A reason the issue's
  own words do not support will be rejected and the issue left alone.

You are proposing, not scheduling. A human decides what happens next.

Return JSON only."""


def render_persona(criteria: list[str]) -> str:
    bullets = "\n".join(f"- {c}" for c in criteria) or "- Clear, testable acceptance criteria"
    return JUDGE_PERSONA.format(criteria=bullets)


def render_candidates(issues: list[IssueCandidate]) -> str:
    return "\n\n".join(
        f"### issue_id: {i.issue_id}\nTitle: {i.title}\n\nBody:\n{i.body}" for i in issues
    )
