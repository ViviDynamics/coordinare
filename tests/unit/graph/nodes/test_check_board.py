from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.state import initial_state


class _GitHub:
    async def poll_board(self):
        return {
            "snapshot": {"TODO": ["ITEM_1"], "IN_PROGRESS": [], "IN_REVIEW": []},
            "titles": {"ITEM_1": "Card"},
            "descriptions": {"ITEM_1": "Desc"},
            "issue_numbers": {"ITEM_1": 1},
        }


class _GitHubWithAC:
    async def poll_board(self):
        return {
            "snapshot": {"TODO": ["ITEM_1"], "IN_PROGRESS": [], "IN_REVIEW": []},
            "titles": {"ITEM_1": "Card"},
            "descriptions": {"ITEM_1": "Description\n- [ ] Must pass tests\n- [ ] Must have docs"},
            "issue_numbers": {"ITEM_1": 1},
        }


@pytest.mark.asyncio
async def test_check_board_selects_todo_card() -> None:
    state = initial_state()
    state["github_service"] = _GitHub()

    result = await check_board(state)

    assert result["phase"] == "dispatching"
    assert result["current_card"]["id"] == "ITEM_1"


@pytest.mark.asyncio
async def test_check_board_includes_acceptance_criteria() -> None:
    state = initial_state()
    state["github_service"] = _GitHubWithAC()

    result = await check_board(state)

    assert result["current_card"]["acceptance_criteria"] == ["Must pass tests", "Must have docs"]


# --- Blocked card resume and reminder tests ---


class _GitHubBlockedWithNewComment:
    def __init__(self) -> None:
        self.moved_to: str | None = None

    async def poll_board(self):
        return {
            "snapshot": {"BLOCKED": ["ITEM_B"], "TODO": [], "IN_PROGRESS": [], "IN_REVIEW": []},
            "titles": {"ITEM_B": "Blocked Card"},
            "descriptions": {"ITEM_B": "Desc"},
            "issue_numbers": {"ITEM_B": 2},
        }

    async def get_issue_details(self, issue_id: str):
        return {
            "comments": {
                "nodes": [
                    {"body": "Here is the answer", "createdAt": "2026-02-25T12:00:00Z"},
                ]
            }
        }

    async def move_card(self, item_id: str, status: str) -> None:
        self.moved_to = status


class _GitHubBlockedNoNewComment:
    async def poll_board(self):
        return {
            "snapshot": {"BLOCKED": ["ITEM_B"], "TODO": [], "IN_PROGRESS": [], "IN_REVIEW": []},
            "titles": {"ITEM_B": "Blocked Card"},
            "descriptions": {"ITEM_B": "Desc"},
            "issue_numbers": {"ITEM_B": 2},
        }

    async def get_issue_details(self, issue_id: str):
        return {"comments": {"nodes": []}}

    async def move_card(self, item_id: str, status: str) -> None:
        pass


@pytest.mark.asyncio
async def test_check_board_resumes_blocked_card_on_new_comment() -> None:
    state = initial_state()
    github = _GitHubBlockedWithNewComment()
    state["github_service"] = github
    state["last_blocked_notified_at"] = datetime(2026, 2, 25, 10, 0, tzinfo=UTC)

    result = await check_board(state)

    assert result["phase"] == "monitoring_agent"
    assert result["current_card"]["status"] == "IN_PROGRESS"
    assert github.moved_to == "IN_PROGRESS"
    assert result["last_blocked_notified_at"] is None


@pytest.mark.asyncio
async def test_check_board_blocked_card_reminder_due() -> None:
    state = initial_state()
    state["github_service"] = _GitHubBlockedNoNewComment()
    state["last_blocked_notified_at"] = datetime(2026, 2, 23, 10, 0, tzinfo=UTC)
    state["blocked_reminder_hours"] = 24

    result = await check_board(state)

    assert result["phase"] == "blocked"
    assert result["current_card"]["id"] == "ITEM_B"


@pytest.mark.asyncio
async def test_check_board_blocked_card_no_reminder_yet() -> None:
    state = initial_state()
    state["github_service"] = _GitHubBlockedNoNewComment()
    state["last_blocked_notified_at"] = datetime.now(UTC)
    state["blocked_reminder_hours"] = 24

    result = await check_board(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_check_board_blocked_card_first_time() -> None:
    state = initial_state()
    state["github_service"] = _GitHubBlockedNoNewComment()

    result = await check_board(state)

    assert result["phase"] == "blocked"
    assert result["current_card"]["id"] == "ITEM_B"
