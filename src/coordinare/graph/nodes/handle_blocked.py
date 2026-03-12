from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


def _questions_from_card(title: str, description: str) -> list[str]:
    """Generate targeted clarification questions without an LLM.

    Used as the last resort when the assessment backend is unavailable or
    produces no output.  Questions reference the actual feature title and
    cover the five categories most commonly missing from empty-body cards.
    """
    label = f'"{title}"' if title else "this feature"
    return [
        f"Which specific pages, routes, or parts of the application should {label} affect?",
        "Describe the expected user experience in detail — what does it look like and how does a user interact with it?",
        "What are the acceptance criteria? How will you verify this feature is complete and working correctly?",
        "Are there any edge cases to handle (e.g. empty states, loading states, error states, or permission checks)?",
        "What data or backend support does this feature require — is anything missing from the current API?",
    ]


async def handle_blocked(state: CoordinareState) -> CoordinareState:
    github = state.get("github_service")
    card = state.get("current_card")
    if github is None or not isinstance(card, dict):
        state["phase"] = "blocked"
        return state

    card_id = str(card.get("id", ""))
    issue_id = str(card.get("issue_id", ""))

    raw_questions = state.get("open_questions")
    questions = [str(item) for item in raw_questions] if isinstance(raw_questions, list) else []
    clarifications = state.get("card_clarifications") or []
    answered_rounds = [c for c in clarifications if isinstance(c, dict) and c.get("answer", "").strip()]

    if not questions:
        logger.info("handle_blocked_no_open_questions", card_id=card_id,
                    msg="No questions from performer/assessor — generating from card content")

        backend = state.get("assessment_backend")
        if backend is not None:
            try:
                card_data: dict = {
                    "title": card.get("title", ""),
                    "body": card.get("description", ""),
                    "clarifications": clarifications,
                }
                assessment = await backend.assess(card_data)
                generated = assessment.get("questions") or []
                questions = [str(q) for q in generated if str(q).strip()]
            except Exception as exc:
                logger.warning("handle_blocked_assessment_failed", card_id=card_id, error=str(exc))
        if not questions:
            if answered_rounds:
                # The user has answered all clarification questions and the assessment
                # has no further questions — the card is sufficiently specified.
                # Re-queue for dispatch by moving the card back to TODO so the next
                # check_board cycle picks it up through assess_card → dispatch_card.
                logger.info(
                    "handle_blocked_requeue_for_dispatch",
                    card_id=card_id,
                    answered_rounds=len(answered_rounds),
                    msg="Sufficient Q&A history — re-queuing card for dispatch",
                )
                await github.move_card(card_id, "TODO")
                state["phase"] = "idle"
                state["last_blocked_notified_at"] = None
                return state
            else:
                logger.warning("handle_blocked_fallback", card_id=card_id,
                               msg="Assessment generated no questions — deriving from card title")
                questions = _questions_from_card(
                    title=card.get("title", ""),
                    description=card.get("description", ""),
                )

    await github.move_card(card_id, "BLOCKED")
    question_lines = "\n".join(f"- {q}" for q in questions)
    if issue_id:
        await github.add_comment(issue_id, f"Needs input:\n{question_lines}")
    else:
        logger.warning(
            "handle_blocked.no_issue_id",
            card_id=card_id,
            msg="Card has no linked issue (draft item?) — skipping comment",
        )

    raw_hours = state.get("blocked_reminder_hours", 24)
    hours = raw_hours if isinstance(raw_hours, int) else 24
    now = datetime.now(UTC)
    last = state.get("last_blocked_notified_at")
    if last is None or (isinstance(last, datetime) and now - last >= timedelta(hours=hours)):
        state["last_blocked_notified_at"] = now
    state["phase"] = "blocked"
    return state
