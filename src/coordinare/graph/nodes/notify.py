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

    # Build human-readable summary
    card_title = str(card.get("title", ""))
    card_number = card.get("issue_number", "")
    card_ref = f"#{card_number}" if card_number else ""
    title_short = card_title[:60] + "…" if len(card_title) > 60 else card_title
    display_title = f"{card_ref} {title_short}".strip()

    status = str(card.get("status", ""))
    prev_status = str(card.get("previous_status", ""))
    pr_url = str(card.get("pr_url", "") or "")
    performer_stage = str(state.get("performer_stage", ""))

    # Human-readable summary per event type
    if event_type == EventType.card_dispatched:
        role = performer_stage or "implementer"
        summary = f"🚀 {display_title} — dispatched to {role}"
    elif event_type == EventType.card_blocked:
        questions_preview = open_questions[0][:80] if open_questions else "needs input"
        summary = f"🚫 {display_title} — blocked: {questions_preview}"
    elif event_type == EventType.card_merged:
        summary = f"✅ {display_title} — merged!"
    elif prev_status and status and prev_status != status:
        summary = f"📋 {display_title} — {prev_status} → {status}"
    else:
        summary = f"📋 {display_title} [{status}]"

    if pr_url:
        summary += f"\n   PR: {pr_url}"

    payload: dict[str, str] = {
        "event_type": event_type.value,
        "severity": NotificationSeverity.info.value,
        "source": "board",
        "summary": summary,
        "card_title": card_title,
        "card_number": str(card_number),
        "card_status": status,
        "previous_status": prev_status,
    }
    if open_questions:
        payload["open_questions"] = "; ".join(open_questions)
    if commit_summary:
        payload["commit_summary"] = commit_summary
    if pr_url:
        payload["pr_url"] = pr_url

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
