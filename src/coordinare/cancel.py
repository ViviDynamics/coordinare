"""Shared card cancellation helper (026-card-cancellation)."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.graph.state import _retire_active_session
from coordinare.services.board_provider import board_of, move_card_or_warn

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

_CANCEL_TIMEOUT_SECONDS = 10


def resolve_active_card(state: CoordinareState) -> tuple[str, str]:
    """The card an operator override should act on: (card_id, title).

    Returns ("", "") when there is genuinely nothing to act on.

    377: this used to read only ``state["current_card"]``, which is a *derived
    mirror* of ``active_sessions[active_card_id]["current_card"]``. Each
    session's card payload is not persisted -- daemon.py rebuilds it "from the
    live board by check_board's re-adopt path" -- so after a daemon restart the
    mirror is None while ``active_card_id`` and ``active_sessions`` are
    populated and a performer is genuinely running.

    In that window both operator override levers were inert. Observed live with
    a performer running and ``monitor_performer`` polling it every 37 seconds:

        POST /api/cancel          -> {"status": "no_active_card"}
        POST /api/restart-from/qa -> {"error": "No active card to override"}

    Cancel is the emergency stop. It was unavailable in precisely the state an
    operator would reach for it, and the workaround -- stopping the container by
    hand -- is worse than a no-op, because coordinare reads the dead container as
    a system error and spends one of the card's three retries.

    So resolve from the session model and fall back to the mirror, rather than
    the reverse: ``active_card_id`` is persisted and survives a restart, and the
    mirror does not. A stale ``active_card_id`` with no matching session is not
    a card -- returning it would make the endpoints claim success on nothing,
    which is a worse lie than the refusal this replaces.
    """
    sessions = state.get("active_sessions") or {}
    active_id = state.get("active_card_id")

    if active_id and isinstance(sessions, dict) and active_id in sessions:
        session = sessions.get(active_id) or {}
        card = session.get("current_card")
        title = ""
        if isinstance(card, dict):
            title = str(card.get("title", ""))
        return str(active_id), title or str(state.get("active_card_title") or "")

    # No session model (a cold single-card flow, or a test): the mirror is all
    # there is, and it is still authoritative when populated.
    card = state.get("current_card")
    if isinstance(card, dict) and card.get("id"):
        return str(card.get("id", "")), str(card.get("title", ""))
    return "", ""


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
    card_id, card_title = resolve_active_card(state)

    if phase == "idle" or not card_id:
        return {"status": "no_active_card"}

    logger.info("cancel_active_card.starting", card_id=card_id, phase=phase)

    # Stop the performer session (best-effort with timeout).
    #
    # 377 review: resolving the CARD correctly is not enough, because the
    # performer is stopped through a session id that comes from somewhere else
    # and can be empty or stale.
    #
    # `agent_dispatch` is deliberately not persisted (state_store: "Transient
    # fields ... are intentionally omitted"), and check_board says plainly that
    # "a freshly-readopted session has no agent_dispatch.session_id". So in the
    # exact case this fix exists for -- a card readopted after a restart -- there
    # is no id to stop with. Reporting "cancelled" there would be a confident
    # lie: the card is reset and moved while the performer keeps running.
    #
    # It can also be STALE rather than empty: a session retiring and check_board
    # promoting another card moves active_card_id without touching
    # agent_dispatch, so the id can belong to a different card than the one
    # resolved above. Stopping that one would kill an unrelated performer.
    agent_service = state.get("agent_service")
    dispatch = state.get("agent_dispatch") or {}
    session_id = str(dispatch.get("session_id") or "")
    dispatch_card = str(dispatch.get("card_id") or "")
    if session_id and dispatch_card and dispatch_card != card_id:
        logger.warning(
            "cancel_active_card.dispatch_belongs_to_another_card",
            card_id=card_id, dispatch_card=dispatch_card,
        )
        session_id = ""

    performer_stopped = False
    if agent_service is not None and session_id:
        try:
            # Try graceful cancel via relay_feedback with cancel signal
            await asyncio.wait_for(
                agent_service.relay_feedback({"action": "cancel", "session_id": session_id}),
                timeout=cancel_timeout_seconds,
            )
            performer_stopped = True
        except TimeoutError:
            logger.warning("cancel_active_card.performer_stop_timeout", card_id=card_id)
        except Exception as exc:
            logger.warning("cancel_active_card.performer_stop_failed", card_id=card_id, error=str(exc))
    else:
        logger.warning(
            "cancel_active_card.no_session_to_stop",
            card_id=card_id,
            reason="agent_dispatch carries no session_id for this card",
        )

    # Clean up workspace
    workspace_manager = state.get("workspace_manager")
    workspace_path = state.get("workspace_path")
    if workspace_manager is not None and workspace_path is not None:
        try:
            await workspace_manager.teardown(workspace_path)
        except Exception as exc:
            logger.warning("cancel_active_card.workspace_cleanup_failed", error=str(exc))

    # Move card back to TODO on the board (if requested).
    #
    # 377 review: NOT when the performer could not be stopped. Returning a card
    # to Todo makes it eligible for pickup, so if the old performer is still
    # alive the card gets a second one -- two performers on one branch, pushing
    # over each other. Leaving it where it is keeps the card visible and
    # inactive, which is recoverable; a double dispatch is not.
    # "Not stopped" is two different situations and only one of them is
    # dangerous. A pre-existing test caught this: with no agent_service at all
    # there is no performer subsystem, so nothing can be running and refusing to
    # return the card just strands it. The risk is specifically that a performer
    # EXISTS and we could not reach it.
    performer_may_be_running = agent_service is not None and not performer_stopped
    board_provider = board_of(state)
    if move_to_todo and performer_may_be_running:
        logger.warning(
            "cancel_active_card.not_returned_to_todo",
            card_id=card_id,
            reason="the performer was not confirmed stopped; returning the card could double-dispatch it",
        )
    if move_to_todo and not performer_may_be_running and board_provider is not None and card_id:
        try:
            await move_card_or_warn(board_provider, card_id, "TODO")
        except Exception as exc:
            logger.warning("cancel_active_card.move_to_todo_failed", card_id=card_id, error=str(exc))

    # Reset state — clear all card-specific and lifecycle fields so the next
    # card starts fresh without stale performer_stage or relay_feedback.
    lifecycle_seq = state.get("lifecycle_sequence") or ["implementing"]
    state["phase"] = "idle"
    _retire_active_session(state, trigger="card_cancelled")
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
    # 377 review: the caller is an operator pressing an emergency stop. Saying
    # "cancelled" when the performer was never reached is the worst outcome of
    # this whole issue -- worse than the original refusal, because it is a
    # confident lie with the card reset underneath a live performer.
    return {
        "status": "cancelled",
        "card_id": card_id,
        "performer_stopped": performer_stopped,
        "returned_to_todo": bool(move_to_todo and not performer_may_be_running),
        **({} if not performer_may_be_running else {
            "warning": (
                "coordinare could not reach the performer to stop it (no session id for this "
                "card); its container may still be running, and the card was left in place "
                "rather than returned to Todo to avoid a second performer on the same branch"
            )
        }),
    }
