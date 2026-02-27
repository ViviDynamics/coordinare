from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity

logger = structlog.get_logger(__name__)


def _event_type_for_phase(phase: str) -> EventType:
    mapping: dict[str, EventType] = {
        "dispatching": EventType.card_dispatched,
        "blocked": EventType.card_blocked,
        "merging": EventType.card_merged,
        "relay_feedback": EventType.card_transition,
        "monitoring_agent": EventType.card_transition,
        "monitoring_pr": EventType.card_transition,
    }
    return mapping.get(phase, EventType.card_transition)


async def notify(state: CoordinareState) -> CoordinareState:
    notification_service = state.get("notification_service")
    card = state.get("current_card")
    if not isinstance(card, dict) or notification_service is None:
        return state

    phase = state.get("phase", "idle")
    event_type = _event_type_for_phase(phase)

    open_questions_raw = state.get("open_questions")
    open_questions = [str(item) for item in open_questions_raw] if isinstance(open_questions_raw, list) else []
    commit_summary_raw = state.get("commit_summary")
    commit_summary = str(commit_summary_raw) if isinstance(commit_summary_raw, str) else None

    payload: dict[str, str] = {
        "event_type": event_type.value,
        "severity": NotificationSeverity.info.value,
        "source": "board",
        "summary": f"{card.get('title', '')} [{card.get('status', '')}]",
        "card_title": str(card.get("title", "")),
        "card_status": str(card.get("status", "")),
        "previous_status": str(card.get("previous_status", "")),
    }
    if open_questions:
        payload["open_questions"] = "; ".join(open_questions)
    if commit_summary:
        payload["commit_summary"] = commit_summary
    if card.get("pr_url"):
        payload["pr_url"] = str(card["pr_url"])

    card_id = card.get("id") or card.get("title", "unknown")
    dedup_key = f"{event_type.value}:{card_id}:{card.get('status', '')}"

    event = NotificationEvent(
        event_type=event_type,
        severity=NotificationSeverity.info,
        payload=payload,
        source="board",
        dedup_key=dedup_key,
    )

    try:
        await notification_service.dispatch(event)
    except Exception as exc:
        logger.warning("notification.dispatch_failed", error=str(exc))

    return state
