from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState


async def check_board(state: CoordinareState) -> CoordinareState:
    github = state.get("github_service")
    if github is None:
        state["phase"] = "idle"
        return state

    board = await github.poll_board()
    snapshot = board.get("snapshot")
    state["board_snapshot"] = snapshot if isinstance(snapshot, dict) else {}
    state["last_poll_at"] = datetime.now(UTC)

    in_progress = state["board_snapshot"].get("IN_PROGRESS", [])
    in_review = state["board_snapshot"].get("IN_REVIEW", [])
    blocked = state["board_snapshot"].get("BLOCKED", [])
    todo = state["board_snapshot"].get("TODO", [])

    if in_review:
        state["phase"] = "monitoring_pr"
        return state
    if in_progress:
        state["phase"] = "monitoring_agent"
        return state
    if blocked:
        state["phase"] = "blocked"
        return state
    if todo:
        item = todo[0]
        titles = board.get("titles", {})
        descriptions = board.get("descriptions", {})
        issue_numbers = board.get("issue_numbers", {})
        state["current_card"] = {
            "id": item,
            "issue_id": item,
            "issue_number": int(issue_numbers.get(item, 0)),
            "title": str(titles.get(item, "")),
            "description": str(descriptions.get(item, "")),
            "status": "TODO",
            "previous_status": "TODO",
        }
        state["phase"] = "dispatching"
        return state

    state["phase"] = "idle"
    return state
