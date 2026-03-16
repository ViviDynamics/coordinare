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

    state["open_questions"] = ["What routes need breadcrumbs?"]
    result = await check_board(state)

    # User comment should trigger re-assessment (dispatching) not agent monitoring
    assert result["phase"] == "dispatching"
    assert result["current_card"]["status"] == "IN_PROGRESS"
    assert github.moved_to == "IN_PROGRESS"
    assert result["last_blocked_notified_at"] is None
    # Clarification should be captured with the prior questions and comment body
    assert len(result["card_clarifications"]) == 1
    assert result["card_clarifications"][0]["answer"] == "Here is the answer"
    assert result["card_clarifications"][0]["questions"] == ["What routes need breadcrumbs?"]
    assert result["open_questions"] == []
    assert result["agent_dispatch"] == {}


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


# --- Early-return priority tests (in_review > in_progress > blocked > todo) ---


class _GitHubInReview:
    async def poll_board(self):
        return {
            "snapshot": {"IN_REVIEW": ["ITEM_R"], "TODO": [], "IN_PROGRESS": [], "BLOCKED": []},
            "titles": {"ITEM_R": "PR Card"},
            "descriptions": {"ITEM_R": ""},
            "issue_numbers": {"ITEM_R": 3},
        }


@pytest.mark.asyncio
async def test_check_board_routes_to_monitoring_pr_for_in_review() -> None:
    state = initial_state()
    state["github_service"] = _GitHubInReview()

    result = await check_board(state)

    assert result["phase"] == "monitoring_pr"


class _GitHubInProgress:
    async def poll_board(self):
        return {
            "snapshot": {"IN_PROGRESS": ["ITEM_P"], "TODO": [], "IN_REVIEW": [], "BLOCKED": []},
            "titles": {"ITEM_P": "Active Card"},
            "descriptions": {"ITEM_P": ""},
            "issue_numbers": {"ITEM_P": 4},
        }


@pytest.mark.asyncio
async def test_check_board_routes_to_monitoring_agent_for_in_progress() -> None:
    state = initial_state()
    state["github_service"] = _GitHubInProgress()

    result = await check_board(state)

    assert result["phase"] == "monitoring_agent"


class _GitHubEmpty:
    async def poll_board(self):
        return {
            "snapshot": {"TODO": [], "IN_PROGRESS": [], "IN_REVIEW": [], "BLOCKED": []},
            "titles": {},
            "descriptions": {},
            "issue_numbers": {},
        }


@pytest.mark.asyncio
async def test_check_board_idle_when_board_empty() -> None:
    state = initial_state()
    state["github_service"] = _GitHubEmpty()

    result = await check_board(state)

    assert result["phase"] == "idle"


# --- Blocked card edge cases: bad comment data ---


class _GitHubBlockedBadComments:
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
                    "not a dict",
                    {"createdAt": ""},
                    {"createdAt": "invalid-date"},
                ]
            }
        }

    async def move_card(self, item_id: str, status: str) -> None:
        pass


@pytest.mark.asyncio
async def test_check_board_blocked_with_bad_comment_data_sends_reminder() -> None:
    """Non-dict comments, empty dates, invalid dates should be skipped."""
    state = initial_state()
    state["github_service"] = _GitHubBlockedBadComments()
    state["last_blocked_notified_at"] = datetime(2026, 2, 20, 10, 0, tzinfo=UTC)
    state["blocked_reminder_hours"] = 24

    result = await check_board(state)

    assert result["phase"] == "blocked"


# --- New system_error routing tests ---


class _GitHubPollFails:
    async def poll_board(self):
        raise RuntimeError("GitHub API is down")


@pytest.mark.asyncio
async def test_check_board_idle_when_poll_board_raises() -> None:
    """poll_board() exception → phase='idle' (don't crash the loop)."""
    state = initial_state()
    state["github_service"] = _GitHubPollFails()

    result = await check_board(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_check_board_system_error_when_in_progress_with_error_count() -> None:
    """in_progress card + system_error_count > 0 → phase='system_error' (retry path)."""
    state = initial_state()
    state["github_service"] = _GitHubInProgress()
    state["system_error_count"] = 1

    result = await check_board(state)

    assert result["phase"] == "system_error"


@pytest.mark.asyncio
async def test_check_board_idle_when_blocked_and_system_error_notified() -> None:
    """Blocked card where operator has already been notified → phase='idle' (no re-notify)."""
    state = initial_state()
    state["github_service"] = _GitHubBlockedNoNewComment()
    state["system_error_notified"] = True

    result = await check_board(state)

    assert result["phase"] == "idle"


class _GitHubBlockedOldComment:
    """Blocked card with a comment that predates last_blocked_notified_at."""

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
                    # Comment is OLDER than last_blocked_notified_at — should not trigger requeue
                    {"body": "old comment", "createdAt": "2026-02-20T08:00:00Z"},
                ]
            }
        }

    async def move_card(self, item_id: str, status: str) -> None:
        pass


@pytest.mark.asyncio
async def test_check_board_blocked_old_comment_does_not_requeue() -> None:
    """A comment that predates last_blocked_notified_at should not trigger re-dispatch."""
    state = initial_state()
    state["github_service"] = _GitHubBlockedOldComment()
    # Notified AFTER the comment — so the comment is "old"
    state["last_blocked_notified_at"] = datetime(2026, 2, 20, 10, 0, tzinfo=UTC)
    state["blocked_reminder_hours"] = 24 * 365  # reminder not due yet

    result = await check_board(state)

    # Comment was old → no requeue; reminder not due → idle
    assert result["phase"] == "idle"


# ---------------------------------------------------------------------------
# Line 158->160: same card id — clarifications NOT cleared
# ---------------------------------------------------------------------------


class _GitHubSameCard:
    async def poll_board(self):
        return {
            "snapshot": {"TODO": ["ITEM_1"], "IN_PROGRESS": [], "IN_REVIEW": []},
            "titles": {"ITEM_1": "Card"},
            "descriptions": {"ITEM_1": "Desc"},
            "issue_numbers": {"ITEM_1": 1},
        }


@pytest.mark.asyncio
async def test_check_board_preserves_clarifications_for_same_card() -> None:
    """Line 158->160: when the board picks the same card as current_card, clarifications are NOT cleared."""
    state = initial_state()
    state["github_service"] = _GitHubSameCard()
    state["current_card"] = {"id": "ITEM_1", "title": "Card", "status": "TODO"}
    state["card_clarifications"] = [{"question": "Q?", "answer": "A"}]

    result = await check_board(state)

    # Clarifications must be preserved (same card returned from re-queue)
    assert result.get("card_clarifications") == [{"question": "Q?", "answer": "A"}]
