from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coordinare.graph.nodes.handle_blocked import _questions_from_card, handle_blocked
from coordinare.graph.state import initial_state
from coordinare.services.github import PermanentGitHubError, TransientGitHubError


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
    state["conducting_backend"] = _FailingBackend()

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
    class _ConductingBackend:
        async def assess(self, card):
            return {"sufficient": False, "questions": ["Which routes?", "What data model?"], "rationale": "missing info"}

    github = _GitHubFallback()
    state = initial_state()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1", "title": "My Feature"}
    state["open_questions"] = []
    state["conducting_backend"] = _ConductingBackend()

    result = await handle_blocked(state)

    assert result["phase"] == "blocked"
    assert "Which routes?" in github.comment_body
    assert "What data model?" in github.comment_body


@pytest.mark.asyncio
async def test_handle_blocked_does_not_repost_comment_if_recent() -> None:
    """042: If last_blocked_notified_at is recent (within reminder window),
    we MUST NOT re-post the reminder comment (avoid spam) — but the
    cutoff timestamp itself is still advanced to ``now`` so check_board
    won't mistake old comments for fresh user answers and trigger an
    infinite blocked → dispatch loop.

    This used to assert the timestamp was NOT updated; that turned out to
    be the source of the dispatch-loop bug surfaced on PR #88's blocked
    state.  See handle_blocked.py docstring for the full reasoning.
    """

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
    # Reminder comment NOT re-posted (within 24h window)
    assert github.comment_body is None
    # But cutoff timestamp DID advance — this is what breaks the loop
    assert result["last_blocked_notified_at"] is not None
    assert result["last_blocked_notified_at"] > recent


@pytest.mark.asyncio
async def test_handle_blocked_advances_cutoff_even_on_stale_state() -> None:
    """042 regression: a stale ``last_blocked_notified_at`` from a saved
    snapshot (e.g., daemon restarted hours after blocking) must still
    advance to ``now`` so check_board doesn't treat the bot's own old
    reminder comments as fresh user answers and dispatch the card.

    Direct reproduction of the PR #88 dispatch loop:
      - Saved state had last_blocked_notified_at = 12:52
      - merge_pr permanent error re-entered blocked at 14:16
      - handle_blocked posted a new reminder at 14:16
      - Without advancing the cutoff, check_board saw the 14:16 bot
        comment as "newer than 12:52" → dispatched
    """

    stale = datetime.now(UTC) - timedelta(hours=2)  # > reminder window? still
    github = _GitHubFallback()
    state = initial_state()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = ["Cannot merge — ruleset blocks the App"]
    state["last_blocked_notified_at"] = stale
    state["blocked_reminder_hours"] = 24  # 2h < 24h, so no re-post

    result = await handle_blocked(state)

    # Cutoff advanced → check_board won't mis-detect old bot comments as answers
    assert result["last_blocked_notified_at"] > stale
    # And no spam comment posted
    assert github.comment_body is None


@pytest.mark.asyncio
async def test_handle_blocked_reposts_comment_after_reminder_window() -> None:
    """042: After the reminder window elapses, the comment IS re-posted
    (so users get a nudge if they've forgotten about the blocked card)."""

    very_old = datetime.now(UTC) - timedelta(hours=48)  # > 24h
    github = _GitHubFallback()
    state = initial_state()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = ["Clarify scope"]
    state["last_blocked_notified_at"] = very_old
    state["blocked_reminder_hours"] = 24

    result = await handle_blocked(state)

    assert github.comment_body is not None
    assert "Needs input" in github.comment_body
    assert result["last_blocked_notified_at"] > very_old


@pytest.mark.asyncio
async def test_handle_blocked_transient_comment_failure_is_deferred() -> None:
    class _GitHubTransientComment:
        def __init__(self) -> None:
            self.comment_attempts = 0
            self.moved_to: list[str] = []
            self.comment_body: str | None = None

        async def move_card(self, item_id: str, status: str) -> None:
            self.moved_to.append(status)

        async def add_comment(self, subject_id: str, body: str):
            self.comment_attempts += 1
            if self.comment_attempts == 1:
                raise TransientGitHubError("temporary failure in name resolution")
            self.comment_body = body
            return {"id": "C1"}


    github = _GitHubTransientComment()
    state = initial_state()
    state["github_service"] = github
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = ["Clarify scope"]
    state["blocked_reminder_hours"] = 24

    first = await handle_blocked(state)
    assert first["phase"] == "blocked"
    queue = first.get("github_retry_queue") or []
    comment_entry = next(
        (entry for entry in queue if isinstance(entry, dict) and entry.get("operation") == "handle_blocked_comment"),
        None,
    )
    assert comment_entry is not None

    # Force deferred retry to become due now; reminder window should not block
    # this retry because it is explicitly queued.
    comment_entry["retry_at"] = datetime.now(UTC) - timedelta(seconds=1)
    second = await handle_blocked(first)
    assert second["phase"] == "blocked"
    assert github.comment_attempts == 2
    assert github.comment_body is not None
    queue_after = second.get("github_retry_queue") or []
    assert not any(
        isinstance(entry, dict) and entry.get("operation") == "handle_blocked_comment"
        for entry in queue_after
    )


# ---------------------------------------------------------------------------
# Fallback questions with empty/None title
# ---------------------------------------------------------------------------


def test_fallback_questions_with_empty_title() -> None:
    """_questions_from_card with empty title uses 'this feature' label."""
    questions = _questions_from_card("", "")
    assert all(isinstance(q, str) and len(q) > 0 for q in questions)
    assert "this feature" in questions[0]


def test_fallback_questions_with_none_title() -> None:
    """_questions_from_card with None title uses 'this feature' label."""
    questions = _questions_from_card(None or "", "")
    assert all(isinstance(q, str) and len(q) > 0 for q in questions)
    assert "this feature" in questions[0]


# ---------------------------------------------------------------------------
# Exception handling in no-questions path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_blocked_move_todo_exception() -> None:
    """When no questions and move_card raises, handle gracefully."""
    class _GitHubMoveError:
        async def move_card(self, item_id: str, status: str) -> None:
            raise Exception("move failed")

    state = initial_state()
    state["github_service"] = _GitHubMoveError()
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = []

    result = await handle_blocked(state)
    assert result["phase"] == "idle"


# ---------------------------------------------------------------------------
# Transient error handling for move_card(BLOCKED)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_blocked_move_card_blocked_transient_error_deferred() -> None:
    """Transient move_card error is deferred with retry queue entry."""
    class _GitHubTransient:
        async def move_card(self, item_id: str, status: str) -> None:
            raise TransientGitHubError("rate limited")

        async def add_comment(self, subject_id: str, body: str):
            return {"id": "C1"}

    state = initial_state()
    state["github_service"] = _GitHubTransient()
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = ["Clarify scope"]

    result = await handle_blocked(state)
    assert result["phase"] == "blocked"
    queue = result.get("github_retry_queue") or []
    move_entry = next(
        (entry for entry in queue if isinstance(entry, dict) and entry.get("operation") == "handle_blocked_move"),
        None,
    )
    assert move_entry is not None
    assert move_entry.get("attempt", 0) >= 1


# ---------------------------------------------------------------------------
# Non-transient error in add_comment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_blocked_add_comment_permanent_error() -> None:
    """Non-transient add_comment error is logged, not deferred."""
    class _GitHubPermanent:
        async def move_card(self, item_id: str, status: str) -> None:
            pass

        async def add_comment(self, subject_id: str, body: str):
            raise PermanentGitHubError("forbidden")

    state = initial_state()
    state["github_service"] = _GitHubPermanent()
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = ["Clarify scope"]

    result = await handle_blocked(state)
    assert result["phase"] == "blocked"
    # Non-transient errors are not queued; the call just logs and continues
    queue = result.get("github_retry_queue") or []
    comment_entries = [entry for entry in queue if isinstance(entry, dict) and entry.get("operation") == "handle_blocked_comment"]
    assert not comment_entries


# ---------------------------------------------------------------------------
# Deferred comment retry polling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_blocked_deferred_comment_wait_log() -> None:
    """When deferred comment is not yet due, log the wait."""
    from coordinare.services.github import TransientGitHubError

    class _GitHubRetry:
        async def move_card(self, item_id: str, status: str) -> None:
            pass

        async def add_comment(self, subject_id: str, body: str):
            raise TransientGitHubError("temporary failure")

    state = initial_state()
    state["github_service"] = _GitHubRetry()
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = ["Clarify scope"]

    # First call: exception, creates deferred entry
    first_state = await handle_blocked(state)

    # Second call: deferred entry exists but retry_at is in the future
    # The code should log the wait and not retry yet
    second_state = await handle_blocked(first_state)

    # The deferred entry should still be there
    queue = second_state.get("github_retry_queue") or []
    comment_entries = [entry for entry in queue if isinstance(entry, dict) and entry.get("operation") == "handle_blocked_comment"]
    assert len(comment_entries) >= 1


@pytest.mark.asyncio
async def test_handle_blocked_non_transient_move_blocked_error_logs_warning() -> None:
    """Non-transient error from move_card('BLOCKED') is logged but does not defer."""
    class _GitHubMoveError:
        async def move_card(self, item_id: str, status: str) -> None:
            if status == "BLOCKED":
                raise PermanentGitHubError("permission denied")

        async def add_comment(self, subject_id: str, body: str):
            return {"id": "C1"}

    state = initial_state()
    state["github_service"] = _GitHubMoveError()
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = ["Clarify scope"]

    result = await handle_blocked(state)
    # Despite the move error, the node should still complete and mark blocked
    assert result["phase"] == "blocked"
    # No deferred entry for move (non-transient errors are not retried)
    queue = result.get("github_retry_queue") or []
    assert not any(
        isinstance(e, dict) and e.get("operation") == "handle_blocked_move"
        for e in queue
    )


@pytest.mark.asyncio
async def test_handle_blocked_deferred_move_waits() -> None:
    """When move_card is deferred and not yet due, the move step is skipped."""

    class _GitHubNeverMove:
        async def move_card(self, item_id: str, status: str) -> None:
            raise AssertionError("move_card must not be called when deferred")

        async def add_comment(self, subject_id: str, body: str):
            return {"id": "C1"}

    state = initial_state()
    state["github_service"] = _GitHubNeverMove()
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = ["Clarify scope"]
    # Pre-load a deferred move entry that is not yet due
    state["github_retry_queue"] = [{
        "operation": "handle_blocked_move",
        "attempt": 1,
        "retry_at": datetime.now(UTC) + timedelta(seconds=300),
        "error": "connection refused",
        "deferred_at": datetime.now(UTC),
    }]

    result = await handle_blocked(state)
    assert result["phase"] == "blocked"
