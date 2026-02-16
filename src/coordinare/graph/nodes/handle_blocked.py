from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState


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
    if not questions:
        questions = ["Please provide additional implementation details."]

    await github.move_card(card_id, "BLOCKED")
    question_lines = "\n".join(f"- {q}" for q in questions)
    await github.add_comment(issue_id, f"Needs input:\n{question_lines}")

    raw_hours = state.get("blocked_reminder_hours", 24)
    hours = raw_hours if isinstance(raw_hours, int) else 24
    now = datetime.now(UTC)
    last = state.get("last_blocked_notified_at")
    if last is None or (isinstance(last, datetime) and now - last >= timedelta(hours=hours)):
        state["last_blocked_notified_at"] = now
    state["phase"] = "blocked"
    return state
