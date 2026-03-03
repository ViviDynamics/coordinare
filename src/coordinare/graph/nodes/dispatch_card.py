from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from coordinare.services.github import PermanentGitHubError
from coordinare.transport.base import TransportError
from coordinare.workspace import WorkspaceSetupError

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


async def dispatch_card(state: CoordinareState) -> CoordinareState:
    card = state.get("current_card")
    github = state.get("github_service")
    agent = state.get("agent_service")
    if not isinstance(card, dict) or github is None or agent is None:
        state["phase"] = "idle"
        return state

    try:
        health = await agent.check_health()
        health_status = str(health.get("status", "unknown"))
    except Exception:
        health_status = "unreachable"

    state["agent_health_status"] = health_status
    if health_status in {"error", "unknown", "unreachable"}:
        state["phase"] = "blocked"
        state["open_questions"] = [
            f"Agent health check failed (status: {health_status}). "
            "Cannot dispatch work until the agent is reachable."
        ]
        return state

    # --- Workspace setup (011) ---
    workspace_manager = state.get("workspace_manager")
    workspace_info = None
    if workspace_manager is not None:
        try:
            workspace_info = await workspace_manager.prepare(card)
            state["workspace_path"] = workspace_info.path
            state["workspace_branch"] = workspace_info.branch
        except WorkspaceSetupError as exc:
            logger.error(
                "workspace_setup_failed.card_blocked",
                card_id=str(card.get("id", "")),
                error=str(exc),
            )
            try:
                await github.move_card(str(card.get("id", "")), "BLOCKED")
            except Exception:
                logger.warning(
                    "move_card_to_blocked_failed",
                    card_id=str(card.get("id", "")),
                )
            state["phase"] = "blocked"
            state["open_questions"] = [str(exc)]
            return state

    card_id = str(card.get("id", ""))
    try:
        await github.move_card(card_id, "IN_PROGRESS")
        result = await agent.dispatch_card(card, workspace_info=workspace_info)
    except (TransportError, PermanentGitHubError) as exc:
        logger.error(
            "permanent_service_failure.card_blocked",
            card_id=card_id,
            error=str(exc),
        )
        try:
            await github.move_card(card_id, "BLOCKED")
        except Exception:
            logger.warning("move_card_to_blocked_failed", card_id=card_id)
        state["phase"] = "blocked"
        state["open_questions"] = [f"Permanent service failure: {exc}"]
        return state

    state["agent_dispatch"] = result
    card["previous_status"] = card.get("status", "TODO")
    card["status"] = "IN_PROGRESS"
    state["current_card"] = card
    state["phase"] = "monitoring_agent"
    return state
