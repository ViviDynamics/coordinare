"""handle_system_error — retry performer errors before escalating to operator."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

_RETRY_INTERVAL = 90.0   # seconds between retry attempts
_MAX_RETRIES = 3          # maximum attempts before notifying operator


async def handle_system_error(state: CoordinareState) -> CoordinareState:
    """Retry failed performer dispatches up to _MAX_RETRIES times.

    On each invocation:
    - If attempts < _MAX_RETRIES and 90s have elapsed: clear dispatch, re-queue for dispatch.
    - If attempts < _MAX_RETRIES but 90s have not elapsed: set idle (wait for next cycle).
    - If attempts >= _MAX_RETRIES: notify operator via notification service, move card to
      BLOCKED on GitHub (without commenting), set system_error_notified=True, go idle.
    """
    count = state.get("system_error_count", 0)
    last_at = state.get("system_error_last_at")
    now = datetime.now(UTC)
    card = state.get("current_card") or {}
    card_id = str(card.get("id", ""))

    if count < _MAX_RETRIES:
        elapsed = (now - last_at).total_seconds() if last_at else _RETRY_INTERVAL
        if elapsed < _RETRY_INTERVAL:
            logger.info(
                "handle_system_error.waiting",
                card_id=card_id,
                attempt=count,
                elapsed=f"{elapsed:.0f}s",
                retry_in=f"{_RETRY_INTERVAL - elapsed:.0f}s",
            )
            state["phase"] = "idle"
            return state

        logger.info(
            "handle_system_error.retrying",
            card_id=card_id,
            attempt=count,
            max_retries=_MAX_RETRIES,
        )
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        state["phase"] = "dispatching"
        return state

    # Max retries exhausted — notify operator once, then move card to BLOCKED
    if not state.get("system_error_notified"):
        reason = state.get("system_error_reason") or "Unknown performer error"
        logger.error(
            "handle_system_error.max_retries_exceeded",
            card_id=card_id,
            reason=reason,
            attempts=count,
        )
        notification_service = state.get("notification_service")
        if notification_service is not None:
            event = NotificationEvent(
                event_type=EventType.performer_error,
                severity=NotificationSeverity.critical,
                payload={
                    "card_title": str(card.get("title", "")),
                    "card_id": card_id,
                    "error": reason[:500],
                    "attempts": str(count),
                },
                source="handle_system_error",
                dedup_key=f"system_error:{card_id}",
            )
            try:
                await notification_service.dispatch(event)
            except Exception as exc:
                logger.warning("handle_system_error.notification_failed", error=str(exc))

        github = state.get("github_service")
        if github is not None and card_id:
            try:
                await github.move_card(card_id, "BLOCKED")
            except Exception as exc:
                logger.warning("handle_system_error.move_card_failed", error=str(exc))

        state["system_error_notified"] = True

    state["phase"] = "idle"
    return state
