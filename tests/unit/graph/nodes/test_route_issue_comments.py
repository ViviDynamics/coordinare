from __future__ import annotations

import pytest

from coordinare.graph.nodes.route_issue_comments import route_issue_comments
from coordinare.graph.state import initial_state
from coordinare.services.issue_comment_service import (
    CommentClassification,
    classify_issue_comment,
)

# ---------------------------------------------------------------------------
# Stub GitHub service
# ---------------------------------------------------------------------------


class _GitHub:
    def __init__(self, comments: list[dict] | None = None):
        self._comments = comments or []

    async def get_issue_comments(self, issue_number: int, since_id: int | None = None):
        return [c for c in self._comments if since_id is None or c["id"] > since_id]


# ---------------------------------------------------------------------------
# classify_issue_comment unit tests
# ---------------------------------------------------------------------------


def test_classify_scope_change():
    assert classify_issue_comment("also add an export to CSV button") == "scope_change"


def test_classify_blocker_update():
    assert classify_issue_comment("the dependency is unblocked now") == "blocker_update"


def test_classify_approval():
    assert classify_issue_comment("LGTM, ship it!") == "approval"


def test_classify_clarification_default():
    assert classify_issue_comment("what should happen when the user is logged out?") == "clarification"


def test_classify_noise_empty():
    assert classify_issue_comment("") == "noise"


# ---------------------------------------------------------------------------
# route_issue_comments node tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_card_returns_state_unchanged():
    state = initial_state()
    state["github_service"] = _GitHub()
    result = await route_issue_comments(state)
    assert result is state


@pytest.mark.asyncio
async def test_no_issue_number_returns_unchanged():
    state = initial_state()
    state["github_service"] = _GitHub()
    state["current_card"] = {"id": "ITEM_1", "issue_number": 0}
    result = await route_issue_comments(state)
    assert result.get("card_clarifications") in (None, [])


@pytest.mark.asyncio
async def test_no_new_comments_returns_unchanged():
    state = initial_state()
    state["github_service"] = _GitHub(comments=[])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}
    result = await route_issue_comments(state)
    assert result.get("card_clarifications") in (None, [])


@pytest.mark.asyncio
async def test_clarification_comment_appended():
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 1001, "author": "alice", "body": "what happens when the user is offline?", "created_at": "2026-04-27T10:00:00Z"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}

    result = await route_issue_comments(state)

    assert len(result["card_clarifications"]) == 1
    c = result["card_clarifications"][0]
    assert c["classification"] == "clarification"
    assert c["comment_id"] == 1001
    assert c["source"] == "issue"
    assert result["requirements_changed"] is False


@pytest.mark.asyncio
async def test_scope_change_sets_requirements_changed():
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 2001, "author": "bob", "body": "please also add dark mode support", "created_at": "2026-04-27T10:00:00Z"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}

    result = await route_issue_comments(state)

    assert result["requirements_changed"] is True
    assert result["card_clarifications"][0]["classification"] == "scope_change"


@pytest.mark.asyncio
async def test_idempotent_second_call():
    comments = [
        {"id": 1001, "author": "alice", "body": "what happens offline?", "created_at": "2026-04-27T10:00:00Z"},
    ]
    state = initial_state()
    state["github_service"] = _GitHub(comments=comments)
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}

    result = await route_issue_comments(state)
    assert len(result["card_clarifications"]) == 1
    assert result["last_issue_comment_id"] == 1001

    # Second call — since_id=1001 means no new comments returned
    result2 = await route_issue_comments(result)
    assert len(result2["card_clarifications"]) == 1  # not duplicated


@pytest.mark.asyncio
async def test_processed_ids_guard_prevents_reprocessing():
    """Simulate restart scenario: comment_id already in processed set."""
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 1001, "author": "alice", "body": "what happens offline?", "created_at": "2026-04-27T10:00:00Z"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}
    state["processed_issue_comment_ids"] = {1001}

    result = await route_issue_comments(state)
    assert result.get("card_clarifications") in (None, [])


@pytest.mark.asyncio
async def test_multiple_comments_mixed_classifications():
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 1, "author": "a", "body": "LGTM", "created_at": "2026-04-27T10:00:00Z"},
        {"id": 2, "author": "b", "body": "also add CSV export", "created_at": "2026-04-27T10:01:00Z"},
        {"id": 3, "author": "c", "body": "what does the error state look like?", "created_at": "2026-04-27T10:02:00Z"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}

    result = await route_issue_comments(state)

    # approval is not appended to clarifications
    clarification_ids = [c["comment_id"] for c in result["card_clarifications"]]
    assert 1 not in clarification_ids  # approval — no entry
    assert 2 in clarification_ids       # scope_change → clarifications
    assert 3 in clarification_ids       # clarification

    assert result["requirements_changed"] is True
    assert result["last_issue_comment_id"] == 3


# ---------------------------------------------------------------------------
# Phase 2: CommentClassification source-agnosticism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body,expected", [
    ("also add dark mode", "scope_change"),
    ("blocked by the auth service", "blocker_update"),
    ("LGTM ship it", "approval"),
    ("what should happen when offline?", "clarification"),
    ("", "noise"),
])
def test_classification_labels_match_taxonomy(body: str, expected: str):
    assert classify_issue_comment(body) == expected


def test_comment_classification_dataclass_fields():
    cc = CommentClassification(
        source="pr",
        comment_id=999,
        author="copilot",
        classification="clarification",
        card_id="ITEM_X",
    )
    assert cc.source == "pr"
    assert cc.comment_id == 999
    assert cc.classification == "clarification"


def test_comment_classification_issue_source():
    cc = CommentClassification(
        source="issue",
        comment_id=42,
        author="alice",
        classification="scope_change",
        card_id="ITEM_Y",
    )
    assert cc.source == "issue"


@pytest.mark.asyncio
async def test_clarification_entry_has_issue_source():
    """route_issue_comments stamps source='issue' on all stored clarifications."""
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 5000, "author": "alice", "body": "what is the expected error format?", "created_at": "2026-04-27T12:00:00Z"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}

    result = await route_issue_comments(state)

    assert result["card_clarifications"][0]["source"] == "issue"


@pytest.mark.asyncio
async def test_scope_change_entry_has_issue_source():
    """scope_change clarification entries carry source='issue'."""
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 6000, "author": "bob", "body": "please also add CSV export", "created_at": "2026-04-27T12:00:00Z"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}

    result = await route_issue_comments(state)

    entry = result["card_clarifications"][0]
    assert entry["source"] == "issue"
    assert entry["classification"] == "scope_change"
