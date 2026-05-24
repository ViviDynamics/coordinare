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
    "also add", "also need", "also want", "should also", "please add",
    "please include", "please extend", "new feature",
    "in addition", "additionally", "new requirement", "additional requirement",
    "out of scope", "scope creep",
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


_VALID_LABELS = frozenset({
    "scope_change", "clarification", "blocker_update", "approval", "noise",
})


_AI_CLASSIFY_PROMPT = """You classify a single comment posted on a GitHub issue \
that an AI coding agent is working on. Choose exactly one label from:

- scope_change: the commenter is asking to add, remove, or change \
requirements. They want the deliverable to do something different than \
originally described.
- clarification: the commenter is asking a question, answering a question, \
giving context, or otherwise discussing the work without changing scope.
- blocker_update: the commenter is reporting that work is blocked, waiting \
on something, or that a previously reported blocker is resolved.
- approval: the commenter is signing off, approving, or expressing \
satisfaction with completed work.
- noise: the comment is empty, an automated status post (CI results, bot \
evidence dumps, deployment notifications), or otherwise carries no \
actionable signal for the agent.

Important:
- Automated QA evidence posts, CI summaries, and tool-generated reports are \
noise even if their body mentions words like "add" or "include" in setup \
commands or UI labels.
- A human pasting bash commands while debugging is clarification, not scope_change.
- Only label scope_change when the commenter explicitly requests a change to \
what the work should produce.

Comment author: {author}
Comment body:
---
{body}
---

Respond with ONLY a JSON object: {{"label": "<one of the five labels>", \
"rationale": "<one short sentence>"}}"""


async def classify_issue_comment_ai(
    body: str,
    author: str,
    conducting_backend: Any,
) -> str | None:
    """Classify a comment using the conducting LLM.

    Returns one of the five labels on success, or None when the backend is
    unavailable, errors out, or returns an unparseable / unknown label. The
    caller falls back to :func:`classify_issue_comment` (keyword) on None.
    """
    if conducting_backend is None:
        return None
    if not body.strip():
        return "noise"

    prompt_text = _AI_CLASSIFY_PROMPT.format(author=author or "unknown", body=body)
    try:
        result = await conducting_backend.prompt(prompt_text, response_format="json")
    except Exception as exc:
        logger.warning("classify_issue_comment_ai.backend_error", error=str(exc))
        return None

    if not isinstance(result, dict):
        return None

    parsed = result.get("data")
    if parsed is None:
        import json as _json

        raw = result.get("text", "")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = _json.loads(raw)
            except (ValueError, TypeError):
                logger.warning("classify_issue_comment_ai.parse_failed", raw_preview=raw[:200])
                return None
        else:
            return None

    if not isinstance(parsed, dict):
        return None

    label = parsed.get("label")
    if not isinstance(label, str) or label not in _VALID_LABELS:
        logger.warning("classify_issue_comment_ai.invalid_label", label=label)
        return None

    return label


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
