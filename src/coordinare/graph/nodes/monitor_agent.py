from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from coordinare.services.github import PermanentGitHubError
from coordinare.transport.base import TransportError

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


async def _teardown_workspace(state: CoordinareState) -> None:
    """Tear down the workspace if one was prepared for this session.

    Clears workspace_path and workspace_branch from state regardless of
    teardown outcome so stale paths never accumulate.
    """
    workspace_manager = state.get("workspace_manager")
    workspace_path = state.get("workspace_path")
    if workspace_manager is not None and workspace_path is not None:
        await workspace_manager.teardown(workspace_path)
    state["workspace_path"] = None
    state["workspace_branch"] = None


async def monitor_agent(state: CoordinareState) -> CoordinareState:
    agent = state.get("agent_service")
    card = state.get("current_card")
    github = state.get("github_service")
    if agent is None or not isinstance(card, dict):
        state["phase"] = "idle"
        return state

    session_id = state.get("agent_dispatch", {}).get("session_id", "")
    card_id = str(card.get("id", ""))

    # Assume terminal by default; cleared only when the agent is still working.
    # The finally block guarantees teardown even on unexpected exceptions.
    _teardown_on_exit = True
    try:
        try:
            status = await agent.check_status(str(session_id))
        except TransportError as exc:
            # Network / transport failure — transient, route through retry logic.
            logger.warning(
                "monitor_agent.transport_error",
                card_id=card_id,
                exc_type=type(exc).__name__,
            )
            # If system_error_notified is True we're inheriting stale state from
            # a previous card's exhausted retry cycle (that card was BLOCKED and
            # can no longer appear in monitor_agent).  Reset so this card gets its
            # full retry budget and operator notification fires if needed.
            if state.get("system_error_notified"):
                state["system_error_count"] = 0
                state["system_error_notified"] = False
            state["system_error_count"] = state.get("system_error_count", 0) + 1
            state["system_error_last_at"] = datetime.now(UTC)
            state["system_error_reason"] = (
                f"Transport failure during status check: {type(exc).__name__}"
            )
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            state["phase"] = "system_error"
            return state
        except PermanentGitHubError as exc:
            logger.error(
                "permanent_service_failure.card_blocked",
                card_id=card_id,
                error=str(exc),
            )
            if github is not None:
                try:
                    await github.move_card(card_id, "BLOCKED")
                except Exception:
                    logger.warning("move_card_to_blocked_failed", card_id=card_id)
            state["phase"] = "blocked"
            state["open_questions"] = [f"Permanent service failure: {exc}"]
            return state

        # Accumulate backend events (capped at 100 entries)
        new_events = status.get("events")
        if isinstance(new_events, list) and new_events:
            existing = list(state.get("performer_events") or [])
            state["performer_events"] = (existing + new_events)[-100:]

        # Store latest performer metrics for dashboard visibility
        new_metrics = status.get("metrics")
        if isinstance(new_metrics, dict):
            state["performer_metrics"] = new_metrics

        marker = status.get("status", "working")
        if marker == "pr_opened":
            card["pr_url"] = status.get("pr_url")
            card["pr_node_id"] = status.get("pr_node_id")
            card["previous_status"] = card.get("status", "IN_PROGRESS")
            card["status"] = "IN_REVIEW"
            state["current_card"] = card
            state["phase"] = "monitoring_pr"
            state["system_error_count"] = 0
            state["system_error_last_at"] = None
            state["system_error_notified"] = False
            state["system_error_reason"] = None
        elif marker == "session_expired":
            # Transient system failure — auto-requeue to TODO without human intervention.
            reason = str(status.get("reason", ""))
            logger.warning(
                "monitor_agent.session_expired_requeue",
                card_id=card_id,
                reason=reason,
                msg="Session expired — moving card back to TODO for re-dispatch",
            )
            if github is not None:
                try:
                    await github.move_card(card_id, "TODO")
                except Exception:
                    logger.warning("move_card_to_todo_failed", card_id=card_id)
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            state["open_questions"] = []
            state["phase"] = "idle"
        elif marker == "error":
            reason = str(status.get("reason", ""))
            state["system_error_count"] = state.get("system_error_count", 0) + 1
            state["system_error_last_at"] = datetime.now(UTC)
            state["system_error_reason"] = (
                f"Performer returned an error: {reason}" if reason
                else "Performer encountered an error."
            )
            state["phase"] = "system_error"
            state["agent_dispatch"] = {}
        elif marker == "blocked":
            state["phase"] = "blocked"
            questions = status.get("questions")
            if isinstance(questions, list) and questions:
                state["open_questions"] = [str(item) for item in questions]
            else:
                # blocked with no questions — assessment backend will generate them
                state["open_questions"] = []
        else:
            _teardown_on_exit = False  # still working — workspace stays active
            state["phase"] = "monitoring_agent"
        return state
    finally:
        if _teardown_on_exit:
            await _teardown_workspace(state)
