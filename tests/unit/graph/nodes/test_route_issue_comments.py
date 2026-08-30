from __future__ import annotations

import pytest

from coordinare.graph.nodes.route_issue_comments import route_issue_comments
from coordinare.graph.state import initial_state
from coordinare.services.card_identity import CardIdentityMap
from coordinare.services.issue_comment_service import (
    CommentClassification,
    classify_issue_comment,
)

# ---------------------------------------------------------------------------
# Stub GitHub service
# ---------------------------------------------------------------------------


class _GitHub(CardIdentityMap):
    """Stands in for the GitHub service, translating card ids as the real one does.

    153: the node now reads comments through the board, and the GitHub board
    provider resolves a card id to an issue number. Inheriting the production
    mixin rather than stubbing the lookup means these tests exercise the real
    translation -- the same reason the bench's fake inherits it.
    """

    def __init__(self, comments: list[dict] | None = None):
        self._comments = comments or []
        self.asked_for: list[int] = []
        # What a board poll would have supplied for the card these tests use.
        self._remember_issue_numbers({"ITEM_1": 42})

    async def get_issue_details(self, card_id: str):
        return {}

    async def get_issue_comments(self, issue_number: int, since_id: int | None = None):
        self.asked_for.append(issue_number)
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
async def test_a_card_with_no_issue_number_is_still_read():
    """153: this used to be a reason to skip, and that was the seam's last leak.

    The node gated on the card's GitHub issue number, so a board whose cards have
    none -- every board that is not GitHub -- routed nothing every cycle, however
    correct the provider beneath it was. The card id is the gate now.
    """
    state = initial_state()
    github = _GitHub()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_number": 0}
    result = await route_issue_comments(state)
    assert github.asked_for == [42], "the card was skipped instead of resolved"
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


class _Backend:
    """Stub conducting backend returning a pre-baked classifier response."""

    def __init__(self, label: str = "clarification", fail: bool = False):
        self.label = label
        self.fail = fail
        self.call_count = 0

    async def prompt(self, text, response_format=None):
        self.call_count += 1
        if self.fail:
            raise RuntimeError("backend down")
        return {"data": {"label": self.label, "rationale": "stub"}}


@pytest.mark.asyncio
async def test_ai_classifier_used_when_backend_present():
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 8001, "author": "alice", "body": "Click + Add Time Entry to log hours", "created_at": "2026-04-27T10:00:00Z"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}
    # AI says noise; keyword would say "clarification" (trimmed) — verify AI wins.
    state["conducting_backend"] = _Backend(label="noise")

    result = await route_issue_comments(state)

    # noise → not appended to clarifications
    assert (result.get("card_clarifications") or []) == []
    assert 8001 in result["processed_issue_comment_ids"]
    assert result["requirements_changed"] is False


@pytest.mark.asyncio
async def test_keyword_fallback_when_ai_unavailable():
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 8100, "author": "bob", "body": "please also add CSV export", "created_at": "2026-04-27T10:00:00Z"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}
    state["conducting_backend"] = _Backend(fail=True)

    result = await route_issue_comments(state)

    assert result["requirements_changed"] is True
    assert result["card_clarifications"][0]["classification"] == "scope_change"


@pytest.mark.asyncio
async def test_keyword_fallback_when_no_backend_in_state():
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 8200, "author": "carol", "body": "please also add dark mode", "created_at": "2026-04-27T10:00:00Z"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}
    # No conducting_backend key — keyword path.

    result = await route_issue_comments(state)

    assert result["requirements_changed"] is True


@pytest.mark.asyncio
async def test_blocker_update_not_appended_to_clarifications():
    """blocker_update comments are logged but not added to clarifications."""
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 7000, "author": "alice", "body": "the dependency is unblocked now", "created_at": "2026-04-27T13:00:00Z"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}

    result = await route_issue_comments(state)

    # blocker_update comments should not appear in clarifications
    clarifications = result.get("card_clarifications") or []
    assert len(clarifications) == 0
    # But the comment should still be marked as processed
    assert 7000 in result["processed_issue_comment_ids"]


# ---------------------------------------------------------------------------
# 123 US6 (T018): dedup comments BEFORE the AI classifier runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_us6_dedup_before_classify_only_new_comments_classified():
    """123 FR-015: 3 already-processed + 2 new → AI classifier called exactly
    twice (only the unprocessed comments)."""
    backend = _Backend(label="clarification")
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 101, "author": "a", "body": "q1?", "created_at": "t"},
        {"id": 102, "author": "a", "body": "q2?", "created_at": "t"},
        {"id": 103, "author": "a", "body": "q3?", "created_at": "t"},
        {"id": 104, "author": "a", "body": "q4?", "created_at": "t"},
        {"id": 105, "author": "a", "body": "q5?", "created_at": "t"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}
    state["conducting_backend"] = backend
    state["processed_issue_comment_ids"] = {101, 102, 103}

    await route_issue_comments(state)

    assert backend.call_count == 2  # only 104 + 105 classified


@pytest.mark.asyncio
async def test_us6_all_processed_skips_classification_entirely():
    """123 FR-016: all comments already processed → AI classifier called 0
    times and the watermark still advances."""
    backend = _Backend(label="clarification")
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 101, "author": "a", "body": "q1?", "created_at": "t"},
        {"id": 102, "author": "a", "body": "q2?", "created_at": "t"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}
    state["conducting_backend"] = backend
    state["processed_issue_comment_ids"] = {101, 102}

    result = await route_issue_comments(state)

    assert backend.call_count == 0
    assert (result.get("card_clarifications") or []) == []
    assert result["last_issue_comment_id"] == 102  # watermark advanced


@pytest.mark.asyncio
async def test_us6_all_new_comments_classified_no_regression():
    """123 FR-015: a fresh set of comments (none processed) is fully classified
    — no regression on new comments."""
    backend = _Backend(label="clarification")
    state = initial_state()
    state["github_service"] = _GitHub(comments=[
        {"id": 201, "author": "a", "body": "q1?", "created_at": "t"},
        {"id": 202, "author": "a", "body": "q2?", "created_at": "t"},
        {"id": 203, "author": "a", "body": "q3?", "created_at": "t"},
    ])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}
    state["conducting_backend"] = backend

    await route_issue_comments(state)

    assert backend.call_count == 3
