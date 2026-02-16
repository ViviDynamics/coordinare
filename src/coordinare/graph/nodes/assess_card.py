from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState


async def assess_card(state: CoordinareState) -> CoordinareState:
    card = state.get("current_card")
    github = state.get("github_service")
    claude = state.get("claude_service")
    if not isinstance(card, dict) or github is None or claude is None:
        state["phase"] = "idle"
        return state

    details = await github.get_issue_details(str(card.get("issue_id", "")))
    assessment = await claude.assess_card_sufficiency(details)
    if assessment.get("sufficient", True):
        state["phase"] = "dispatching"
        state["open_questions"] = []
    else:
        state["phase"] = "blocked"
        questions = assessment.get("questions")
        state["open_questions"] = [str(item) for item in questions] if isinstance(questions, list) else []
    return state
