from __future__ import annotations

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
        except (TransportError, PermanentGitHubError) as exc:
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

        marker = status.get("status", "working")
        if marker == "pr_opened":
            card["pr_url"] = status.get("pr_url")
            card["pr_node_id"] = status.get("pr_node_id")
            card["previous_status"] = card.get("status", "IN_PROGRESS")
            card["status"] = "IN_REVIEW"
            state["current_card"] = card
            state["phase"] = "monitoring_pr"
        elif marker in {"blocked", "error", "session_expired"}:
            state["phase"] = "blocked"
            questions = status.get("questions")
            state["open_questions"] = [str(item) for item in questions] if isinstance(questions, list) else []
        else:
            _teardown_on_exit = False  # still working — workspace stays active
            state["phase"] = "monitoring_agent"
        return state
    finally:
        if _teardown_on_exit:
            await _teardown_workspace(state)
