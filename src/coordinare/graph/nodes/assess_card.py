from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from coordinare.services.persona_service import get_effective_instructions, load_personas_hot

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
        # Work with a mutable copy before attaching additional metadata.
        details = dict(details)

        # Attach any accumulated Q&A history so the assessment backend can
        # incorporate it when generating follow-up questions or deciding
        # whether there is now enough information to proceed.
        clarifications = state.get("card_clarifications") or []
        if clarifications:
            details["clarifications"] = clarifications

        # Inject assessor persona instructions with hot-reload (018-performer-personas).
        personas = load_personas_hot(state.get("config_path"), state.get("config"))
        details["persona_instructions"] = get_effective_instructions("assessor", personas)

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

    # If the model returned insufficient with no new questions, the assessor
    # had nothing concrete to ask — treat as sufficient rather than blocking
    # indefinitely. This covers both: (a) first assessment where the assessor
    # found no issues, and (b) follow-up rounds where all questions are answered.
    if not sufficient and not questions:
        logger.info(
            "assess_card.no_questions_treating_as_sufficient",
            card_id=str(card.get("id", "")),
            answered_rounds=len(answered_rounds),
            msg="Assessor returned insufficient but asked no questions — proceeding to dispatch",
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
