from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
from coordinare.models.card import CardStatus
from coordinare.models.notification import Notification

logger = structlog.get_logger(__name__)


def _coerce_status(raw: object, *, fallback: CardStatus) -> CardStatus:
    if isinstance(raw, str):
        try:
            return CardStatus(raw)
        except ValueError:
            return fallback
    return fallback


async def notify(state: CoordinareState) -> CoordinareState:
    email_service = state.get("email_service")
    slack_service = state.get("slack_service")
    card = state.get("current_card")
    if not isinstance(card, dict) or email_service is None or slack_service is None:
        return state

    open_questions_raw = state.get("open_questions")
    open_questions = [str(item) for item in open_questions_raw] if isinstance(open_questions_raw, list) else []
    commit_summary_raw = state.get("commit_summary")
    commit_summary = str(commit_summary_raw) if isinstance(commit_summary_raw, str) else None

    notification = Notification(
        card_title=str(card.get("title", "")),
        card_status=_coerce_status(card.get("status"), fallback=CardStatus.TODO),
        previous_status=_coerce_status(card.get("previous_status"), fallback=CardStatus.TODO),
        task_description=str(card.get("description", "")),
        open_questions=open_questions,
        commit_summary=commit_summary,
        pr_url=str(card.get("pr_url")) if card.get("pr_url") else None,
    )

    for coro, channel in [
        (email_service.send_notification(state.get("notification_email", "coordinare@vividynamics.com"), notification), "email"),
        (slack_service.send_notification(notification), "slack"),
    ]:
        try:
            await coro
        except Exception as exc:
            logger.warning("notification.delivery_failed", channel=channel, error=str(exc))

    return state
