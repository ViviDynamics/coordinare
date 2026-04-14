from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.graph.nodes.check_board import _sort_by_priority, check_board
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


@pytest.mark.asyncio
async def test_check_board_clears_commit_summary_on_new_card() -> None:
    """042 regression: after one card merges successfully, commit_summary
    stays populated in state. When check_board picks up the NEXT card,
    it must clear commit_summary — otherwise the notify node's success
    detection (``if commit_summary or card.status=='DONE'``) fires
    card_merged for every stage dispatch of the new card.

    Observed: after card #87 merged, card #89's very first dispatch
    notification fired as ``card_merged`` instead of ``card_dispatched``."""
    state = initial_state()
    state["github_service"] = _GitHub()
    # Simulate the residue of a previous successful merge
    state["commit_summary"] = "abc1234 Previous card merged"
    state["current_card"] = None  # no prior card tracked

    result = await check_board(state)

    # New card picked up — commit_summary must be cleared
    assert result["current_card"]["id"] == "ITEM_1"
    assert result.get("commit_summary") is None


@pytest.mark.asyncio
async def test_check_board_preserves_commit_summary_when_same_card() -> None:
    """Defensive: clearing commit_summary must only happen on a genuine
    card transition, not every cycle.  (The transition check uses card id.)"""
    state = initial_state()
    state["github_service"] = _GitHub()
    state["commit_summary"] = "abc1234 Same card"
    state["current_card"] = {"id": "ITEM_1", "title": "Card"}  # same id as _GitHub returns

    result = await check_board(state)

    # Same card — commit_summary NOT cleared
    assert result.get("commit_summary") == "abc1234 Same card"


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


# ---------------------------------------------------------------------------
# 042 — bot's own comments must NOT be treated as user answers
# ---------------------------------------------------------------------------


class _GitHubBlockedOnlyBotComments:
    """Card in BLOCKED column with only bot-authored comments on the issue.
    GitHub records createdAt at second precision; our local timestamps are
    sub-second — so the bot's own reminder can appear "newer" than the
    cutoff.  The author filter stops the false-answer detection."""

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
                    {
                        "body": "**🔍 Assessor** — Needs input:\n- Cannot merge PR",
                        "createdAt": "2026-04-13T19:58:50Z",
                        "author": {"login": "vivi-coordinare[bot]"},
                    },
                    {
                        "body": "**✅ Closer** — Needs input:\n- GitHub rejected merge",
                        "createdAt": "2026-04-13T20:01:36Z",
                        "author": {"login": "vivi-coordinare[bot]"},
                    },
                ]
            }
        }

    async def move_card(self, item_id: str, status: str) -> None:
        self.moved_to = status


@pytest.mark.asyncio
async def test_check_board_ignores_bot_comments_on_blocked_card() -> None:
    """042 regression: the coordinare bot's own reminder comments on a
    blocked issue must NOT be treated as user answers — otherwise every
    cycle re-dispatches the card, forming an infinite blocked → dispatch
    loop.  Surfaced on PR #88 after the merge_pr permanent-error fix
    made cards actually land in blocked."""
    from datetime import UTC, datetime, timedelta

    state = initial_state()
    github = _GitHubBlockedOnlyBotComments()
    state["github_service"] = github
    # Stale cutoff — all bot comments are "newer" by timestamp but must
    # still be ignored because of the author filter.
    state["last_blocked_notified_at"] = datetime.now(UTC) - timedelta(hours=1)
    state["blocked_reminder_hours"] = 24
    state["open_questions"] = ["Cannot merge PR"]

    result = await check_board(state)

    # Must NOT have transitioned to dispatching — bot comments are not answers
    assert result["phase"] != "dispatching"
    assert github.moved_to != "IN_PROGRESS"
    # No clarification captured from bot comments
    assert result.get("card_clarifications") == []
    # open_questions preserved (not cleared as if answered)
    assert result["open_questions"] == ["Cannot merge PR"]


class _GitHubBlockedMixedAuthors:
    """Mix of bot and human comments on the blocked issue — only the
    human comment should trigger the re-dispatch."""

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
                    {
                        "body": "Reminder from bot",
                        "createdAt": "2026-04-13T19:58:50Z",
                        "author": {"login": "vivi-coordinare[bot]"},
                    },
                    {
                        "body": "Here's the answer from a real user",
                        "createdAt": "2026-04-13T20:05:00Z",
                        "author": {"login": "alice"},
                    },
                ]
            }
        }

    async def move_card(self, item_id: str, status: str) -> None:
        self.moved_to = status


@pytest.mark.asyncio
async def test_check_board_human_answer_still_triggers_redispatch() -> None:
    """042: The bot-comment filter must not suppress genuine user answers
    — this is the primary purpose of the blocked-card detection.  A
    human comment newer than the cutoff still triggers re-dispatch."""
    from datetime import UTC, datetime

    state = initial_state()
    github = _GitHubBlockedMixedAuthors()
    state["github_service"] = github
    state["last_blocked_notified_at"] = datetime(2026, 4, 13, 19, 30, tzinfo=UTC)
    state["blocked_reminder_hours"] = 24
    state["open_questions"] = ["Question to answer"]

    result = await check_board(state)

    # Human comment should trigger dispatch
    assert result["phase"] == "dispatching"
    assert github.moved_to == "IN_PROGRESS"
    clarifications = result.get("card_clarifications", [])
    assert len(clarifications) == 1
    assert "real user" in clarifications[0]["answer"]


class _GitHubBlockedOnlyOldHumanComment:
    """Blocked issue with only old human comments (from prior blocked
    rounds before our latest cutoff).  Those must not re-trigger."""

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
                    {
                        "body": "Old answer",
                        "createdAt": "2026-03-01T12:00:00Z",
                        "author": {"login": "alice"},
                    }
                ]
            }
        }

    async def move_card(self, item_id: str, status: str) -> None:
        self.moved_to = status


@pytest.mark.asyncio
async def test_check_board_ignores_human_comments_older_than_cutoff() -> None:
    """042: Timestamp filter still applies alongside the author filter —
    old human comments from prior blocked rounds don't re-trigger."""
    from datetime import UTC, datetime

    state = initial_state()
    github = _GitHubBlockedOnlyOldHumanComment()
    state["github_service"] = github
    state["last_blocked_notified_at"] = datetime(2026, 4, 13, 19, 30, tzinfo=UTC)
    state["blocked_reminder_hours"] = 24
    state["open_questions"] = ["Question"]

    result = await check_board(state)

    assert result["phase"] != "dispatching"
    assert github.moved_to != "IN_PROGRESS"


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
async def test_check_board_readopts_in_progress_card_after_restart() -> None:
    """When current_card is None (fresh restart) and a card is IN_PROGRESS, re-adopt it."""
    state = initial_state()
    state["github_service"] = _GitHubInProgress()

    result = await check_board(state)

    assert result["phase"] == "dispatching"
    assert result["current_card"] is not None
    assert result["current_card"]["id"] == "ITEM_P"


@pytest.mark.asyncio
async def test_check_board_routes_to_monitoring_agent_for_in_progress_with_card() -> None:
    """When current_card is set and card is IN_PROGRESS, monitor it."""
    state = initial_state()
    state["github_service"] = _GitHubInProgress()
    state["current_card"] = {"id": "ITEM_P", "status": "IN_PROGRESS"}

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


# ---------------------------------------------------------------------------
# 025 — Card Prioritization tests
# ---------------------------------------------------------------------------


class TestSortByPriority:
    """Tests for _sort_by_priority helper."""

    def test_sorts_by_priority_value_lexicographic(self) -> None:
        items = ["C", "A", "B"]
        fields = {"A": {"Priority": "P1"}, "B": {"Priority": "P0"}, "C": {"Priority": "P2"}}
        result = _sort_by_priority(items, fields, "Priority", [])
        assert result == ["B", "A", "C"]  # P0 < P1 < P2

    def test_null_priority_sorts_last(self) -> None:
        items = ["A", "B", "C"]
        fields = {"A": {}, "B": {"Priority": "P0"}, "C": {"Priority": "P1"}}
        result = _sort_by_priority(items, fields, "Priority", [])
        assert result == ["B", "C", "A"]  # B(P0), C(P1), A(no value)

    def test_tied_priority_preserves_board_order(self) -> None:
        items = ["A", "B", "C"]
        fields = {"A": {"Priority": "P1"}, "B": {"Priority": "P1"}, "C": {"Priority": "P0"}}
        result = _sort_by_priority(items, fields, "Priority", [])
        assert result == ["C", "A", "B"]  # C(P0), then A,B(P1) in original order

    def test_custom_priority_order(self) -> None:
        items = ["A", "B", "C"]
        fields = {"A": {"Urgency": "Low"}, "B": {"Urgency": "Critical"}, "C": {"Urgency": "High"}}
        result = _sort_by_priority(items, fields, "Urgency", ["Critical", "High", "Medium", "Low"])
        assert result == ["B", "C", "A"]  # Critical < High < Low

    def test_unlisted_value_sorts_after_listed(self) -> None:
        items = ["A", "B"]
        fields = {"A": {"Priority": "Unknown"}, "B": {"Priority": "P0"}}
        result = _sort_by_priority(items, fields, "Priority", ["P0", "P1"])
        assert result == ["B", "A"]  # P0 listed, Unknown not listed → sorts after

    def test_no_field_values_returns_original_order(self) -> None:
        items = ["A", "B", "C"]
        result = _sort_by_priority(items, {}, "Priority", [])
        assert result == ["A", "B", "C"]  # all null → original order preserved

    def test_all_same_priority_preserves_order(self) -> None:
        items = ["X", "Y", "Z"]
        fields = {"X": {"P": "P1"}, "Y": {"P": "P1"}, "Z": {"P": "P1"}}
        result = _sort_by_priority(items, fields, "P", [])
        assert result == ["X", "Y", "Z"]


@pytest.mark.asyncio
async def test_check_board_selects_highest_priority_card() -> None:
    """Integration: check_board uses priority sorting when configured."""
    from unittest.mock import MagicMock

    class _GitHubWithPriority:
        async def poll_board(self):
            return {
                "snapshot": {"TODO": ["LOW", "HIGH", "MED"], "IN_PROGRESS": [], "IN_REVIEW": [], "BLOCKED": []},
                "titles": {"LOW": "Low", "HIGH": "High", "MED": "Med"},
                "descriptions": {"LOW": "", "HIGH": "", "MED": ""},
                "issue_numbers": {"LOW": 1, "HIGH": 2, "MED": 3},
                "issue_urls": {},
                "content_node_ids": {},
                "item_field_values": {
                    "LOW": {"Priority": "P2"},
                    "HIGH": {"Priority": "P0"},
                    "MED": {"Priority": "P1"},
                },
            }
        async def get_issue_details(self, issue_id): return {}
        async def move_card(self, item_id, status): pass

    from coordinare.config import PriorityConfig

    config = MagicMock()
    config.priority = PriorityConfig(field_name="Priority", priority_order=[])

    state = initial_state()
    state["github_service"] = _GitHubWithPriority()
    state["config"] = config

    result = await check_board(state)

    assert result["current_card"]["id"] == "HIGH"  # P0 is highest priority
