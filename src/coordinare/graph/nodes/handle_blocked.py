from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from coordinare.graph.attribution import coordinare_attribution
from coordinare.graph.nodes.github_retry import (
    clear_deferred_github_operation,
    defer_github_operation,
    get_deferred_github_operation,
    github_operation_ready,
    is_transient_github_outage_error,
)
from coordinare.services.board_provider import board_of, move_card_or_warn

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


def _parse_clarification_ts(raw: object) -> datetime | None:
    """Best-effort parse of a clarification ``created_at`` ISO timestamp."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _is_bot_author(author: str) -> bool:
    return author.endswith("[bot]") or author == "vivi-coordinare"


def _reask_loop_detected(clarifications: list) -> bool:
    """Detect an assessor re-ask loop on already-answered questions.

    The forgetfulness failure mode (live card #153): the human answers a
    clarification round, but the assessor regenerates the same questions
    (often rephrased, so exact-text matching is unreliable) and the bot
    re-posts them. We detect the loop *structurally / temporally* instead of
    by text: a human has answered at least one round, AND the bot has posted
    a clarification *after* that latest human answer with no newer human
    reply. That means we are re-asking questions the human already addressed
    — re-blocking again would loop forever.
    """
    latest_human: datetime | None = None
    bot_times: list[datetime] = []
    for c in clarifications:
        if not isinstance(c, dict):
            continue
        author = str(c.get("author", ""))
        ts = _parse_clarification_ts(c.get("created_at"))
        if _is_bot_author(author):
            if ts is not None:
                bot_times.append(ts)
        elif author and ts is not None and (latest_human is None or ts > latest_human):
            # human-authored clarification == an answer
            latest_human = ts
    if latest_human is None:
        return False
    return any(t > latest_human for t in bot_times)


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
    board_provider = board_of(state)
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

    # Forgetfulness guard: if the assessor is re-asking questions the human
    # already answered (human answered, then the bot re-posted the same
    # clarification with no newer human reply), stop re-blocking and re-queue
    # for dispatch. Re-blocking again would loop indefinitely on questions
    # that have effectively been answered. See _reask_loop_detected.
    if questions and _reask_loop_detected(clarifications):
        logger.info(
            "handle_blocked_clarification_loop_broken",
            card_id=card_id,
            open_questions=len(questions),
            answered_rounds=len(answered_rounds),
            msg="Assessor re-asking already-answered questions — re-queuing for dispatch",
        )
        try:
            await move_card_or_warn(board_provider, card_id, "TODO")
        except Exception as exc:
            logger.warning("handle_blocked.move_card_todo_failed", card_id=card_id, error=str(exc))
        state["open_questions"] = []
        state["phase"] = "idle"
        state["last_blocked_notified_at"] = None
        return state

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
                state["open_questions"] = questions
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
                await move_card_or_warn(board_provider, card_id, "TODO")
            except Exception as exc:
                logger.warning("handle_blocked.move_card_todo_failed", card_id=card_id, error=str(exc))
            state["phase"] = "idle"
            state["last_blocked_notified_at"] = None
            return state

    move_ready, move_retry_in = github_operation_ready(state, "handle_blocked_move")
    if move_ready:
        try:
            await move_card_or_warn(board_provider, card_id, "BLOCKED")
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
    # 069: prefer the per-card session watermark over the top-level mirror.
    # The session dict survives rehydration via PersistedSession, so a restart
    # within the reminder window won't lose the dedup gate and re-post.
    active_sessions = state.setdefault("active_sessions", {}) if card_id else {}
    sess = active_sessions.get(card_id) if card_id else None
    if not isinstance(sess, dict):
        sess = {}
        if card_id:
            active_sessions[card_id] = sess
    sess_last = sess.get("last_blocked_notified_at")
    last = sess_last if sess_last is not None else state.get("last_blocked_notified_at")

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
                header = coordinare_attribution(state.get("config"), stage)
                await board_provider.add_card_comment(
                    issue_id,
                    f"{header}\n\n**{role_label}** — Needs input:\n{question_lines}",
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
    # 069: dual-write to session dict so the watermark survives rehydration
    # via PersistedSession.last_blocked_notified_at.
    if card_id:
        sess["last_blocked_notified_at"] = now
        # 069 FR-005: keep the per-card session-phase mirror aligned with the
        # top-level phase.  monitor_performer flips state["phase"]="blocked"
        # without touching active_sessions[card_id]["phase"], so notify's
        # FR-005 guard saw a stale "monitoring_performer" on the session and
        # suppressed legitimate card_blocked posts (2026-05-23 incident).
        sess["phase"] = "blocked"

    from coordinare.metrics import METRICS
    symphony = state.get("symphony_name", "__default__")
    METRICS.cards_outcome_total.labels(symphony=symphony, outcome="blocked").inc()

    state["phase"] = "blocked"
    return state
