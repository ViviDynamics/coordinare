"""Advocate actions (spec 173): what the run does to an issue, and in what order.

The label is applied before the comment, deliberately and preserved from the
service this replaces. If the comment succeeds and the label fails, the next
run sees an unlabelled issue and answers it again; labelling first means the
worst case is a labelled issue with no reply, which a human notices once rather
than a stranger receiving the same automated answer every cycle.
"""
from __future__ import annotations

import structlog

from performer.workflows.advocate.models import (
    Action,
    Classification,
    IssueCandidate,
    IssueOutcome,
)
from performer.workflows.advocate.settings import AdvocateSettings

log = structlog.get_logger(__name__)


class Poster:
    """The GitHub side of the run, injectable so tests never touch a network.

    Kept as one object rather than three callables because every method needs
    the same owner, repo and token, and threading those through each call site
    is how one of them ends up pointed at the wrong repository.
    """

    def __init__(self, owner: str, repo: str, token: str) -> None:
        self._owner, self._repo, self._token = owner, repo, token

    async def label(self, issue_id: str, label: str) -> None:
        from performer.github import add_labels

        await add_labels(self._owner, self._repo, issue_id, [label], self._token)

    async def comment(self, issue_number: int, body: str) -> None:
        from performer.github import post_issue_comment

        await post_issue_comment(self._owner, self._repo, issue_number, body, self._token)


def reply_body(classification: Classification, settings: AdvocateSettings) -> str:
    """An answer plus the automation disclosure."""
    return f"{classification.answer}\n\n{settings.disclosure_template}"


def redirect_body(settings: AdvocateSettings) -> str:
    """The off-topic redirect, with the support URL substituted.

    A template referring to a placeholder the settings do not fill is rendered
    literally rather than raising, because a KeyError here would abort a run
    over a comment's wording.
    """
    try:
        return settings.redirect_template.format(
            support_channel_url=settings.support_channel_url,
        )
    except (KeyError, IndexError, ValueError):
        return settings.redirect_template


async def apply_outcome(
    poster: Poster,
    issue: IssueCandidate,
    *,
    action: Action,
    label: str,
    body: str | None,
    settings: AdvocateSettings,
    classification: Classification | None = None,
    escalation_reason: str | None = None,
) -> IssueOutcome:
    """Label, then comment. Returns what actually happened.

    A failure on either call is recorded on the outcome rather than raised: one
    unreachable issue must not abandon the rest of the run.
    """
    outcome = IssueOutcome(
        issue_id=issue.issue_id,
        number=issue.number,
        action=action,
        escalation_reason=escalation_reason,
        classification=classification.classification if classification else None,
        cited_documents=list(classification.cited_documents) if classification else [],
    )
    try:
        await poster.label(issue.issue_id, label)
        outcome.label_applied = label
    except Exception as exc:  # noqa: BLE001
        log.warning("advocate.label_failed", issue=issue.issue_id, error=str(exc))

    if body:
        try:
            await poster.comment(issue.number, body)
            outcome.comment_posted = True
        except Exception as exc:  # noqa: BLE001
            log.warning("advocate.comment_failed", issue=issue.issue_id, error=str(exc))
    return outcome
