"""Issue comment polling service (055).

Fetches new comments on GitHub issues linked to active cards and
classifies them for dispatch by route_issue_comments.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class CommentClassification:
    """Assessor output for a single comment from any source.

    source distinguishes PR comments from issue comments; classification
    logic is identical regardless of source.
    """

    source: Literal["pr", "issue"]
    comment_id: int
    author: str
    classification: Literal["clarification", "scope_change", "blocker_update", "approval", "noise"]
    card_id: str

# ---------------------------------------------------------------------------
# Classification taxonomy
# ---------------------------------------------------------------------------

_SCOPE_CHANGE_KEYWORDS = [
    "add", "also add", "also need", "also want", "should also", "please add",
    "include", "extend", "new feature", "scope", "requirement",
    "in addition", "additionally", "expand",
]

_BLOCKER_KEYWORDS = [
    "blocked", "blocker", "blocking", "waiting on", "depends on",
    "can't proceed", "cannot proceed", "unblocked", "resolved now",
]

_APPROVAL_KEYWORDS = [
    "lgtm", "looks good", "approved", "ship it", "good to go", "👍", "✅",
    "great work", "well done", "perfect",
]


def _compile_keyword_pattern(keywords: list[str]) -> re.Pattern[str]:
    parts = [re.escape(kw) for kw in keywords]
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


_SCOPE_CHANGE_RE = _compile_keyword_pattern(_SCOPE_CHANGE_KEYWORDS)
_BLOCKER_RE = _compile_keyword_pattern(_BLOCKER_KEYWORDS)
_APPROVAL_RE = _compile_keyword_pattern(_APPROVAL_KEYWORDS)


@dataclass
class IssueCommentEvent:
    issue_number: int
    comment_id: int
    author: str
    body: str
    created_at: str
    card_id: str


def classify_issue_comment(body: str) -> str:
    """Classify a comment body as one of: scope_change, blocker_update, approval, noise, clarification.

    Uses keyword matching. Falls back to clarification if no other label matches.
    """
    if not body.strip():
        return "noise"
    if _SCOPE_CHANGE_RE.search(body):
        return "scope_change"
    if _BLOCKER_RE.search(body):
        return "blocker_update"
    if _APPROVAL_RE.search(body):
        return "approval"
    # Default: treat as clarification (question, feedback, context)
    return "clarification"


async def fetch_new_issue_comments(
    issue_number: int,
    since_id: int | None,
    github_service: Any,
    card_id: str,
) -> list[IssueCommentEvent]:
    """Fetch comments on issue_number posted after since_id.

    Returns [] if issue_number is 0/None or on any error.
    """
    if not issue_number:
        return []
    try:
        raw = await github_service.get_issue_comments(issue_number, since_id=since_id)
    except Exception:
        logger.exception(
            "fetch_new_issue_comments.failed",
            issue_number=issue_number,
            since_id=since_id,
            card_id=card_id,
        )
        return []
    events: list[IssueCommentEvent] = []
    for c in raw:
        events.append(
            IssueCommentEvent(
                issue_number=issue_number,
                comment_id=int(c["id"]),
                author=str(c.get("author", "")),
                body=str(c.get("body", "")),
                created_at=str(c.get("created_at", "")),
                card_id=card_id,
            )
        )
    return events
