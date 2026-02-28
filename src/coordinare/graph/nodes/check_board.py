from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from coordinare.lib.acceptance_criteria import parse_acceptance_criteria

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


async def check_board(state: CoordinareState) -> CoordinareState:
    github = state.get("github_service")
    if github is None:
        state["phase"] = "idle"
        return state

    try:
        board = await github.poll_board()
    except Exception as exc:
        logger.error("check_board.poll_failed", error=str(exc))
        state["phase"] = "idle"
        return state
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
        item = blocked[0]
        titles = board.get("titles", {})
        descriptions = board.get("descriptions", {})
        issue_numbers = board.get("issue_numbers", {})
        description = str(descriptions.get(item, ""))
        state["current_card"] = {
            "id": item,
            "issue_id": item,
            "issue_number": int(issue_numbers.get(item, 0)),
            "title": str(titles.get(item, "")),
            "description": description,
            "acceptance_criteria": parse_acceptance_criteria(description),
            "status": "BLOCKED",
            "previous_status": "BLOCKED",
        }

        last_notified = state.get("last_blocked_notified_at")
        if last_notified is not None and isinstance(last_notified, datetime):
            details = await github.get_issue_details(item)
            comments_node = details.get("comments")
            comments = (
                comments_node.get("nodes", [])
                if isinstance(comments_node, dict)
                else []
            )
            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                created_raw = comment.get("createdAt", "")
                if not isinstance(created_raw, str) or not created_raw:
                    continue
                try:
                    created_at = datetime.fromisoformat(
                        created_raw.replace("Z", "+00:00")
                    )
                    if created_at > last_notified:
                        await github.move_card(item, "IN_PROGRESS")
                        state["current_card"]["previous_status"] = "BLOCKED"
                        state["current_card"]["status"] = "IN_PROGRESS"
                        state["phase"] = "monitoring_agent"
                        state["last_blocked_notified_at"] = None
                        return state
                except (ValueError, TypeError):
                    continue

        raw_hours = state.get("blocked_reminder_hours", 24)
        hours = raw_hours if isinstance(raw_hours, int) else 24
        now = datetime.now(UTC)
        if last_notified is None or (
            isinstance(last_notified, datetime)
            and now - last_notified >= timedelta(hours=hours)
        ):
            state["phase"] = "blocked"
            return state

        state["phase"] = "idle"
        return state
    if todo:
        # Filter out items carrying advocate labels (FR-001a)
        advocate_labels = set()
        handled = state.get("advocate_handled_label", "")
        escalation = state.get("advocate_escalation_label", "")
        if handled:
            advocate_labels.add(str(handled))
        if escalation:
            advocate_labels.add(str(escalation))

        item_labels = board.get("item_labels", {})
        eligible_todo = [
            item_id for item_id in todo
            if not (set(item_labels.get(item_id, [])) & advocate_labels)
        ]

        if eligible_todo:
            item = eligible_todo[0]
            titles = board.get("titles", {})
            descriptions = board.get("descriptions", {})
            issue_numbers = board.get("issue_numbers", {})
            description = str(descriptions.get(item, ""))
            state["current_card"] = {
                "id": item,
                "issue_id": item,
                "issue_number": int(issue_numbers.get(item, 0)),
                "title": str(titles.get(item, "")),
                "description": description,
                "acceptance_criteria": parse_acceptance_criteria(description),
                "status": "TODO",
                "previous_status": "TODO",
            }
            state["phase"] = "dispatching"
            return state

        # All TODO items are filtered by advocate labels — clear any stale current_card
        # so persisted snapshots don't carry forward a card that's no longer eligible.
        state["current_card"] = None

    state["phase"] = "idle"
    return state
