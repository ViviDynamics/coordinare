"""Route new issue comments to the appropriate handler (055).

Runs every cycle before check_board. For the active card's linked issue,
fetches any new comments, classifies them, and updates session state:
  - scope_change  → requirements_changed = True + card_clarifications entry
  - clarification → card_clarifications entry
  - blocker_update → logged (operator visibility; no automated action)
  - approval      → logged only
  - noise         → silently recorded
"""
from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

import structlog

from coordinare.services.board_provider import board_of
from coordinare.services.issue_comment_service import (
    CommentClassification,
    classify_issue_comment,
    classify_issue_comment_ai,
    fetch_new_issue_comments,
)

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


# Bound restart catch-up: one comment-heavy card must not hold every monitor tick.
_COMMENTS_PER_TICK = 5
_COMMENT_BUDGET_SECONDS = 20.0
_CLASSIFIER_TIMEOUT_SECONDS = 15.0


async def route_issue_comments(state: CoordinareState) -> CoordinareState:
    board = board_of(state)
    card = state.get("current_card")
    if board is None or not isinstance(card, dict):
        return state

    card_id = str(card.get("id", ""))
    # 153: gated on the card id, not on a GitHub issue number. The old gate meant a
    # board whose cards have no issue number -- every board that is not GitHub --
    # returned here every cycle and routed nothing, however correct the provider
    # beneath it was.
    if not card_id:
        return state

    # Metadata only: it rides along on the events for the logs and the dashboard.
    # Nothing fetches with it.
    issue_number = int(card.get("issue_number") or 0)
    since_id: int | None = state.get("last_issue_comment_id")
    processed: set[int] = set(state.get("processed_issue_comment_ids") or ())

    events = await fetch_new_issue_comments(card_id, since_id, board, issue_number)
    if not events:
        return state

    # Process an ordered prefix so the watermark never skips deferred comments.
    events = sorted(events, key=lambda event: event.comment_id)
    new_max_id = since_id or 0
    deadline = monotonic() + _COMMENT_BUDGET_SECONDS
    handled = 0

    conducting_backend = state.get("conducting_backend")
    clarifications: list[dict] = list(state.get("card_clarifications") or [])
    requirements_changed: bool = bool(state.get("requirements_changed"))

    for event in events:
        if event.comment_id in processed:
            new_max_id = max(new_max_id, event.comment_id)
            continue
        remaining = deadline - monotonic()
        if handled >= _COMMENTS_PER_TICK or remaining <= 0:
            logger.info("route_issue_comments.deferred", card_id=card_id)
            break
        try:
            async with asyncio.timeout(min(_CLASSIFIER_TIMEOUT_SECONDS, remaining)):
                label = await classify_issue_comment_ai(
                    event.body, event.author, conducting_backend
                )
        except TimeoutError:
            # The existing keyword fallback preserves the original comment body
            # for scope changes/clarifications when the model is unavailable.
            label = None
            logger.info("route_issue_comments.classifier_timeout", card_id=card_id)
        handled += 1
        new_max_id = max(new_max_id, event.comment_id)
        classifier = "ai"
        if label is None:
            label = classify_issue_comment(event.body)
            classifier = "keyword"
        classification = CommentClassification(
            source="issue",
            comment_id=event.comment_id,
            author=event.author,
            classification=label,  # type: ignore[arg-type]
            card_id=card_id,
        )
        processed.add(event.comment_id)

        logger.info(
            "route_issue_comments.classified",
            comment_id=classification.comment_id,
            author=classification.author,
            classification=classification.classification,
            source=classification.source,
            classifier=classifier,
            card_id=card_id,
            issue_number=issue_number,
        )

        if classification.classification in ("scope_change", "clarification"):
            if classification.classification == "scope_change":
                requirements_changed = True
            clarifications.append({
                "source": classification.source,
                "comment_id": classification.comment_id,
                "author": classification.author,
                "classification": classification.classification,
                "body": event.body,
                "created_at": event.created_at,
            })
        elif classification.classification == "blocker_update":
            logger.info(
                "route_issue_comments.blocker_update",
                comment_id=classification.comment_id,
                body_preview=event.body[:120],
                card_id=card_id,
            )
        # approval and noise: already logged above

    state["card_clarifications"] = clarifications
    state["requirements_changed"] = requirements_changed
    state["last_issue_comment_id"] = new_max_id if new_max_id else since_id
    state["processed_issue_comment_ids"] = processed

    return state
