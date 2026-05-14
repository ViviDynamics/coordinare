from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from coordinare.graph.nodes.github_retry import (
    clear_deferred_github_operation,
    defer_github_operation,
    get_deferred_github_operation,
    github_operation_ready,
    is_transient_github_outage_error,
)

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

        backend = state.get("conducting_backend")
        if backend is not None:
            try:
                from coordinare.services.persona_service import (
                    get_effective_instructions,
                    load_personas_hot,
                )
                personas = load_personas_hot(state.get("config_path"), state.get("config"))
                card_data: dict = {
                    "title": card.get("title", ""),
                    "body": card.get("description", ""),
                    "clarifications": clarifications,
                    "persona_instructions": get_effective_instructions("assessor", personas),
                }
                assessment = await backend.assess(card_data)
                generated = assessment.get("questions") or []
                questions = [str(q) for q in generated if str(q).strip()]
            except Exception as exc:
                logger.warning("handle_blocked_assessment_failed", card_id=card_id, error=str(exc))
        if not questions:
            # No questions from assessment — the card is sufficiently specified.
            # Re-queue for dispatch instead of posting generic "clarify" questions.
            logger.info(
                "handle_blocked_requeue_for_dispatch",
                card_id=card_id,
                answered_rounds=len(answered_rounds),
                msg="No questions generated — re-queuing card for dispatch",
            )
            try:
                await github.move_card(card_id, "TODO")
            except Exception as exc:
                logger.warning("handle_blocked.move_card_todo_failed", card_id=card_id, error=str(exc))
            state["phase"] = "idle"
            state["last_blocked_notified_at"] = None
            return state

    move_ready, move_retry_in = github_operation_ready(state, "handle_blocked_move")
    if move_ready:
        try:
            await github.move_card(card_id, "BLOCKED")
            clear_deferred_github_operation(state, "handle_blocked_move")
        except Exception as exc:
            if is_transient_github_outage_error(exc):
                deferred = defer_github_operation(
                    state,
                    operation="handle_blocked_move",
                    error=exc,
                    base_delay_seconds=30.0,
                    max_delay_seconds=600.0,
                )
                logger.warning(
                    "handle_blocked.move_card_blocked_deferred",
                    card_id=card_id,
                    error=str(exc),
                    attempt=deferred.get("attempt"),
                    retry_at=deferred.get("retry_at"),
                )
            else:
                logger.warning("handle_blocked.move_card_blocked_failed", card_id=card_id, error=str(exc))
    else:
        logger.info(
            "handle_blocked.move_card_blocked_wait",
            card_id=card_id,
            retry_in_seconds=round(move_retry_in, 1),
        )
    question_lines = "\n".join(f"- {q}" for q in questions)

    # Identify which role is blocking so humans know who's talking
    role_labels: dict[str, str] = {
        "assessing": "🔍 Assessor",
        "architecting": "📐 Architect",
        "implementing": "💻 Implementer",
        "reviewing": "👀 Reviewer",
        "security": "🔒 Security",
        "qa": "🧪 QA",
        "documenting": "📝 Tech Writer",
        "closing_review": "✅ Closer",
    }
    stage = state.get("performer_stage", "assessing")
    role_label = role_labels.get(stage, f"🤖 {stage}")

    raw_hours = state.get("blocked_reminder_hours", 24)
    hours = raw_hours if isinstance(raw_hours, int) else 24
    now = datetime.now(UTC)
    last = state.get("last_blocked_notified_at")

    # 042: ``last_blocked_notified_at`` does double duty as (1) the cutoff
    # check_board uses to detect new user answers ("any comment newer than
    # this is an answer") and (2) the gate for re-posting the reminder
    # comment every 24h.  We must update (1) on EVERY pass through this
    # node so check_board doesn't mistake old comments — including the
    # bot's own previous reminders — for fresh user answers and trigger
    # a dispatch loop.  Comment posting (2) is independently gated so we
    # don't spam the issue.
    comment_deferred = get_deferred_github_operation(state, "handle_blocked_comment")
    comment_ready, comment_retry_in = github_operation_ready(state, "handle_blocked_comment")
    if comment_deferred is not None:
        should_repost_reminder = comment_ready
    else:
        should_repost_reminder = last is None or (
            isinstance(last, datetime) and now - last >= timedelta(hours=hours)
        )

    if should_repost_reminder:
        if issue_id:
            try:
                await github.add_comment(
                    issue_id, f"**{role_label}** — Needs input:\n{question_lines}",
                )
                clear_deferred_github_operation(state, "handle_blocked_comment")
            except Exception as exc:
                if is_transient_github_outage_error(exc):
                    deferred = defer_github_operation(
                        state,
                        operation="handle_blocked_comment",
                        error=exc,
                        base_delay_seconds=60.0,
                        max_delay_seconds=1800.0,
                    )
                    logger.warning(
                        "handle_blocked.add_comment_deferred",
                        card_id=card_id,
                        error=str(exc),
                        attempt=deferred.get("attempt"),
                        retry_at=deferred.get("retry_at"),
                    )
                else:
                    logger.warning(
                        "handle_blocked.add_comment_failed",
                        card_id=card_id, error=str(exc),
                    )
        else:
            logger.warning(
                "handle_blocked.no_issue_id",
                card_id=card_id,
                msg="Card has no linked issue (draft item?) — skipping comment",
            )
    elif comment_deferred is not None:
        logger.info(
            "handle_blocked.add_comment_wait",
            card_id=card_id,
            retry_in_seconds=round(comment_retry_in, 1),
        )

    # Always advance the cutoff timestamp — see comment above.  Stale
    # cutoffs from a previous blocked round (or from a saved snapshot
    # restored after restart) cause check_board to re-dispatch the card
    # on the bot's own comments, producing an infinite blocked → dispatch
    # → blocked loop.
    state["last_blocked_notified_at"] = now

    state["phase"] = "blocked"
    return state
