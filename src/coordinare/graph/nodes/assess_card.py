from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


async def assess_card(state: CoordinareState) -> CoordinareState:
    card = state.get("current_card")
    github = state.get("github_service")
    backend = state.get("assessment_backend")
    if not isinstance(card, dict) or github is None or backend is None:
        state["phase"] = "idle"
        return state

    try:
        details = await github.get_issue_details(str(card.get("issue_id", "")))
        # Attach any accumulated Q&A history so the assessment backend can
        # incorporate it when generating follow-up questions or deciding
        # whether there is now enough information to proceed.
        clarifications = state.get("card_clarifications") or []
        if clarifications:
            details = dict(details)
            details["clarifications"] = clarifications
        assessment = await backend.assess(details)
    except Exception as exc:
        logger.error(
            "assess_card.service_failure",
            card_id=str(card.get("id", "")),
            error=str(exc),
        )
        state["phase"] = "blocked"
        state["open_questions"] = [f"Assessment failed: {exc}"]
        return state

    clarifications = state.get("card_clarifications") or []
    answered_rounds = [c for c in clarifications if isinstance(c, dict) and c.get("answer", "").strip()]

    questions = assessment.get("questions") or []
    sufficient = assessment.get("sufficient", True)

    # If the model returned insufficient with no new questions but the user
    # has already answered at least one round, the LLM has run out of things
    # to ask — treat this as sufficient so we don't loop forever.
    if not sufficient and not questions and answered_rounds:
        logger.info(
            "assess_card.no_new_questions_after_answers",
            card_id=str(card.get("id", "")),
            answered_rounds=len(answered_rounds),
            msg="No follow-up questions after answered rounds — proceeding to dispatch",
        )
        sufficient = True

    if sufficient:
        # Embed accumulated clarifications into the card so dispatch_card
        # can forward the full Q&A context to the performer.
        if clarifications and isinstance(card, dict):
            card = dict(card)
            card["clarifications"] = clarifications
            state["current_card"] = card
        state["phase"] = "dispatching"
        state["open_questions"] = []
    else:
        state["phase"] = "blocked"
        state["open_questions"] = [str(item) for item in questions] if isinstance(questions, list) else []
    return state
