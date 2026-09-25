"""Advocate actions (spec 173): what the run does to an issue, and in what order.

The label is applied before the comment.  416 sharpened the other half of the
promise: when the label does not land, the comment is withheld entirely, so
the worst case is an issue nobody spoke on twice, not a stranger receiving the
same automated answer every cycle.  The Poster creates a missing label rather
than letting the repo 404 forever, and the refusal is the backstop for when
even that fails.
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
        from performer.github import add_labels, ensure_label

        # 416: a repository without the labels pre-created is not a 404 on
        # every cycle -- create the label once, then retry the apply.  If even
        # the retry fails, the exception propagates and the comment is
        # withheld, which is the durable-refusal contract.
        try:
            await add_labels(self._owner, self._repo, issue_id, [label], self._token)
        except Exception:
            await ensure_label(self._owner, self._repo, label, self._token)
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
    """Label, then comment — and never comment when the label did not land.

    A failure on either call is recorded on the outcome rather than raised: one
    unreachable issue must not abandon the rest of the run.  416: a failed
    label now withholds the comment as well.  Posting the comment anyway is
    what made a repository without the labels answer the same issue every
    cycle, because the label is the only durable "already handled" mark.
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
        log.warning(
            "advocate.comment_withheld", issue=issue.issue_id,
            reason="label_failed", error=str(exc),
        )
        return outcome

    if body:
        try:
            await poster.comment(issue.number, body)
            outcome.comment_posted = True
        except Exception as exc:  # noqa: BLE001
            log.warning("advocate.comment_failed", issue=issue.issue_id, error=str(exc))
    return outcome
