from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


async def assess_card(state: CoordinareState) -> CoordinareState:
    card = state.get("current_card")
    github = state.get("github_service")
    claude = state.get("claude_service")
    if not isinstance(card, dict) or github is None or claude is None:
        state["phase"] = "idle"
        return state

    try:
        details = await github.get_issue_details(str(card.get("issue_id", "")))
        assessment = await claude.assess_card_sufficiency(details)
    except Exception as exc:
        logger.error(
            "assess_card.service_failure",
            card_id=str(card.get("id", "")),
            error=str(exc),
        )
        state["phase"] = "blocked"
        state["open_questions"] = [f"Assessment failed: {exc}"]
        return state

    if assessment.get("sufficient", True):
        state["phase"] = "dispatching"
        state["open_questions"] = []
    else:
        state["phase"] = "blocked"
        questions = assessment.get("questions")
        state["open_questions"] = [str(item) for item in questions] if isinstance(questions, list) else []
    return state
