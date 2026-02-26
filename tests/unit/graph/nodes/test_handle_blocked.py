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

    async def move_card(self, item_id: str, status: str) -> None:
        pass

    async def add_comment(self, subject_id: str, body: str):
        self.comment_body = body
        return {"id": "C1"}


@pytest.mark.asyncio
async def test_handle_blocked_uses_fallback_when_no_open_questions() -> None:
    github = _GitHubFallback()
    state = initial_state()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = []

    result = await handle_blocked(state)

    assert result["phase"] == "blocked"
    assert "additional implementation details" in github.comment_body
