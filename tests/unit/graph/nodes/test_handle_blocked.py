from __future__ import annotations

import pytest

from coordinare.graph.nodes.handle_blocked import handle_blocked
from coordinare.graph.state import initial_state


class _GitHub:
    async def move_card(self, item_id: str, status: str) -> None:
        assert item_id == "ITEM_1"
        assert status == "BLOCKED"

    async def add_comment(self, subject_id: str, body: str):
        assert subject_id == "ISSUE_1"
        assert "Needs input" in body
        return {"id": "C1"}


@pytest.mark.asyncio
async def test_handle_blocked_moves_card_and_comments() -> None:
    state = initial_state()
    state["github_service"] = _GitHub()
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = ["Clarify AC"]

    result = await handle_blocked(state)

    assert result["phase"] == "blocked"
    assert result.get("last_blocked_notified_at") is not None


@pytest.mark.asyncio
async def test_handle_blocked_returns_blocked_when_no_github() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1"}

    result = await handle_blocked(state)

    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_handle_blocked_returns_blocked_when_no_card() -> None:
    state = initial_state()
    state["github_service"] = _GitHub()

    result = await handle_blocked(state)

    assert result["phase"] == "blocked"


class _GitHubFallback:
    def __init__(self) -> None:
        self.comment_body: str | None = None
        self.moved_to: str | None = None

    async def move_card(self, item_id: str, status: str) -> None:
        self.moved_to = status

    async def add_comment(self, subject_id: str, body: str):
        self.comment_body = body
        return {"id": "C1"}


@pytest.mark.asyncio
async def test_handle_blocked_requeues_when_no_open_questions() -> None:
    github = _GitHubFallback()
    state = initial_state()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = []

    result = await handle_blocked(state)

    # When no questions are generated, re-queue the card for dispatch
    # instead of posting generic "clarify" questions.
    assert result["phase"] == "idle"
    assert github.moved_to == "TODO"


class _GitHubRequeue:
    def __init__(self) -> None:
        self.moved_to: list[str] = []

    async def move_card(self, item_id: str, status: str) -> None:
        self.moved_to.append(status)

    async def add_comment(self, subject_id: str, body: str):
        return {"id": "C1"}


@pytest.mark.asyncio
async def test_handle_blocked_requeues_when_no_questions_and_answered_rounds() -> None:
    """No questions + prior answered rounds → re-queue card to TODO for dispatch."""
    github = _GitHubRequeue()
    state = initial_state()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1", "title": "Add feature"}
    state["open_questions"] = []
    state["card_clarifications"] = [{"questions": ["What routes?"], "answer": "All of them"}]

    result = await handle_blocked(state)

    assert result["phase"] == "idle"
    assert "TODO" in github.moved_to
    assert result["last_blocked_notified_at"] is None


@pytest.mark.asyncio
async def test_handle_blocked_assessment_failure_requeues() -> None:
    """Assessment backend raising an exception → no questions → re-queue to TODO."""
    class _FailingBackend:
        async def assess(self, card):
            raise RuntimeError("Anthropic API down")

    github = _GitHubFallback()
    state = initial_state()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1", "title": "My Feature"}
    state["open_questions"] = []
    state["assessment_backend"] = _FailingBackend()

    result = await handle_blocked(state)

    # Assessment failure with no questions → re-queue for dispatch
    assert result["phase"] == "idle"
    assert github.moved_to == "TODO"


@pytest.mark.asyncio
async def test_handle_blocked_skips_comment_when_no_issue_id() -> None:
    """Card with no issue_id → move card to BLOCKED but skip adding a GitHub comment."""
    class _GitHubTracked:
        def __init__(self) -> None:
            self.moved_to: list[str] = []
            self.comments_added: list[str] = []

        async def move_card(self, item_id: str, status: str) -> None:
            self.moved_to.append(status)

        async def add_comment(self, subject_id: str, body: str):
            self.comments_added.append(body)
            return {"id": "C1"}

    github = _GitHubTracked()
    state = initial_state()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_id": ""}  # no issue_id
    state["open_questions"] = ["What routes should this affect?"]

    result = await handle_blocked(state)

    assert result["phase"] == "blocked"
    assert "BLOCKED" in github.moved_to
    assert github.comments_added == []  # no comment was posted


@pytest.mark.asyncio
async def test_handle_blocked_uses_assessment_questions_when_provided() -> None:
    """Assessment backend returns questions → uses them directly (skips fallback)."""
    class _AssessmentBackend:
        async def assess(self, card):
            return {"sufficient": False, "questions": ["Which routes?", "What data model?"], "rationale": "missing info"}

    github = _GitHubFallback()
    state = initial_state()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1", "title": "My Feature"}
    state["open_questions"] = []
    state["assessment_backend"] = _AssessmentBackend()

    result = await handle_blocked(state)

    assert result["phase"] == "blocked"
    assert "Which routes?" in github.comment_body
    assert "What data model?" in github.comment_body


@pytest.mark.asyncio
async def test_handle_blocked_does_not_update_notification_time_if_recent() -> None:
    """If last_blocked_notified_at is recent, it is NOT updated again."""
    from datetime import UTC, datetime, timedelta

    recent = datetime.now(UTC) - timedelta(hours=1)
    github = _GitHubFallback()
    state = initial_state()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = ["Clarify scope"]
    state["last_blocked_notified_at"] = recent
    state["blocked_reminder_hours"] = 24

    result = await handle_blocked(state)

    assert result["phase"] == "blocked"
    # Notification time should NOT have been advanced (it was recent)
    assert result["last_blocked_notified_at"] == recent
