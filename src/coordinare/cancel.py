"""Shared card cancellation helper (026-card-cancellation)."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

_CANCEL_TIMEOUT_SECONDS = 10


async def cancel_active_card(
    state: CoordinareState,
    *,
    move_to_todo: bool = True,
    cancel_timeout_seconds: int = _CANCEL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Cancel the currently active card, stop the performer, and clean up.

    Returns a dict with ``status`` ("cancelled" or "no_active_card").
    """
    phase = state.get("phase", "idle")
    card = state.get("current_card")

    if phase == "idle" or not isinstance(card, dict):
        return {"status": "no_active_card"}

    card_id = str(card.get("id", ""))
    card_title = str(card.get("title", ""))

    logger.info("cancel_active_card.starting", card_id=card_id, phase=phase)

    # Stop the performer session (best-effort with timeout)
    agent_service = state.get("agent_service")
    session_id = state.get("agent_dispatch", {}).get("session_id", "")
    if agent_service is not None and session_id:
        try:
            # Try graceful cancel via relay_feedback with cancel signal
            await asyncio.wait_for(
                agent_service.relay_feedback({"action": "cancel", "session_id": session_id}),
                timeout=cancel_timeout_seconds,
            )
        except TimeoutError:
            logger.warning("cancel_active_card.performer_stop_timeout", card_id=card_id)
        except Exception as exc:
            logger.warning("cancel_active_card.performer_stop_failed", card_id=card_id, error=str(exc))

    # Clean up workspace
    workspace_manager = state.get("workspace_manager")
    workspace_path = state.get("workspace_path")
    if workspace_manager is not None and workspace_path is not None:
        try:
            await workspace_manager.teardown(workspace_path)
        except Exception as exc:
            logger.warning("cancel_active_card.workspace_cleanup_failed", error=str(exc))

    # Move card back to TODO on the board (if requested)
    github = state.get("github_service")
    if move_to_todo and github is not None and card_id:
        try:
            await github.move_card(card_id, "TODO")
        except Exception as exc:
            logger.warning("cancel_active_card.move_to_todo_failed", card_id=card_id, error=str(exc))

    # Reset state — clear all card-specific and lifecycle fields so the next
    # card starts fresh without stale performer_stage or relay_feedback.
    lifecycle_seq = state.get("lifecycle_sequence") or ["implementing"]
    state["phase"] = "idle"
    state["current_card"] = None
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    state["workspace_path"] = None
    state["workspace_branch"] = None
    state["performer_events"] = []
    state["performer_metrics"] = None
    state["open_questions"] = []
    state["system_error_count"] = 0
    state["system_error_reason"] = None
    state["system_error_notified"] = False
    state["system_error_last_at"] = None
    state["performer_stage"] = lifecycle_seq[0] if lifecycle_seq else "implementing"
    state["relay_feedback"] = []
    state["pending_reviews"] = []
    state["pending_override"] = None  # 031: clear any queued override
    state["card_tokens_total"] = 0  # 034: clear token counters
    state["card_cost_estimate"] = 0.0
    state["card_budget_alert_sent"] = False
    from coordinare.metrics import METRICS
    METRICS.card_cost_estimate_dollars.set(0)
    state["card_clarifications"] = []

    # Emit notification
    notification_service = state.get("notification_service")
    if notification_service is not None:
        from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity

        try:
            await notification_service.dispatch(
                NotificationEvent(
                    event_type=EventType.card_cancelled,
                    severity=NotificationSeverity.warning,
                    payload={
                        "event_type": "card_cancelled",
                        "severity": "warning",
                        "source": "cancel",
                        "card_id": card_id,
                        "card_title": card_title,
                        "previous_phase": phase,
                        "summary": f"Card '{card_title}' cancelled from {phase}",
                    },
                    source="cancel",
                    dedup_key=f"card_cancelled:{card_id}",
                )
            )
        except Exception as exc:
            logger.warning("cancel_active_card.notification_failed", error=str(exc))

    logger.info("cancel_active_card.complete", card_id=card_id)
    return {"status": "cancelled", "card_id": card_id}
