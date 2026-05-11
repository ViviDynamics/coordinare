from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.graph.nodes.check_board import _sort_by_priority, check_board
from coordinare.graph.state import initial_state
from coordinare.services.github import TransientGitHubError


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


# --- 050: Assignee filter tests ---


def _make_assignee_board(assignees_by_item: dict) -> object:
    class _GitHubAssignee:
        async def poll_board(self):
            return {
                "snapshot": {"TODO": list(assignees_by_item.keys()), "IN_PROGRESS": [], "IN_REVIEW": []},
                "titles": {k: f"Card {k}" for k in assignees_by_item},
                "descriptions": {k: "" for k in assignees_by_item},
                "issue_numbers": {k: i + 1 for i, k in enumerate(assignees_by_item)},
                "item_assignees": assignees_by_item,
            }
    return _GitHubAssignee()


class _SimpleConfig:
    assignee_filter: str | None = None

    def __init__(self, assignee_filter=None):
        self.assignee_filter = assignee_filter


@pytest.mark.asyncio
async def test_assignee_filter_dispatches_matching_card() -> None:
    """Filter set + card assigned to filter login → dispatched."""
    state = initial_state()
    state["github_service"] = _make_assignee_board({"ITEM_1": ["coordinare-bot"]})
    state["config"] = _SimpleConfig(assignee_filter="coordinare-bot")

    result = await check_board(state)

    assert result["phase"] == "dispatching"
    assert result["current_card"]["id"] == "ITEM_1"


@pytest.mark.asyncio
async def test_assignee_filter_skips_different_login() -> None:
    """Filter set + card assigned to different login → idle (no dispatch)."""
    state = initial_state()
    state["github_service"] = _make_assignee_board({"ITEM_1": ["human-engineer"]})
    state["config"] = _SimpleConfig(assignee_filter="coordinare-bot")

    result = await check_board(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_assignee_filter_skips_unassigned_card() -> None:
    """Filter set + card with no assignees → idle (no dispatch)."""
    state = initial_state()
    state["github_service"] = _make_assignee_board({"ITEM_1": []})
    state["config"] = _SimpleConfig(assignee_filter="coordinare-bot")

    result = await check_board(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_no_assignee_filter_dispatches_any_card() -> None:
    """No filter set → dispatches any TODO card regardless of assignees."""
    state = initial_state()
    state["github_service"] = _make_assignee_board({"ITEM_1": ["human-engineer"]})
    state["config"] = _SimpleConfig(assignee_filter=None)

    result = await check_board(state)

    assert result["phase"] == "dispatching"
    assert result["current_card"]["id"] == "ITEM_1"


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


class _GitHubInReviewWithTodo:
    async def poll_board(self):
        return {
            "snapshot": {
                "IN_REVIEW": ["ITEM_R"],
                "TODO": ["ITEM_T"],
                "IN_PROGRESS": [],
                "BLOCKED": [],
            },
            "titles": {"ITEM_R": "Awaiting Review", "ITEM_T": "Fresh Work"},
            "descriptions": {"ITEM_R": "", "ITEM_T": ""},
            "issue_numbers": {"ITEM_R": 100, "ITEM_T": 101},
            "issue_urls": {},
            "content_node_ids": {},
        }


@pytest.mark.asyncio
async def test_check_board_multicard_readopts_in_review_and_picks_up_todo() -> None:
    """061: In multi-card mode, an orphaned IN_REVIEW card should be
    re-adopted into active_sessions (phase=monitoring_pr) AND a fresh TODO
    card should be picked up in the same cycle, since passive sessions
    don't consume a concurrency slot.
    """
    from types import SimpleNamespace

    state = initial_state()
    state["github_service"] = _GitHubInReviewWithTodo()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=2,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    state["active_sessions"] = {}

    result = await check_board(state)

    sessions = result.get("active_sessions") or {}
    assert "ITEM_R" in sessions, "IN_REVIEW card should be re-adopted into active_sessions"
    assert "ITEM_T" in sessions, "TODO card should be picked up in same cycle"
    assert sessions["ITEM_R"]["phase"] == "monitoring_pr"
    assert sessions["ITEM_R"]["current_card"]["id"] == "ITEM_R"
    assert sessions["ITEM_R"]["current_card"]["status"] == "IN_REVIEW"
    assert sessions["ITEM_T"]["phase"] == "dispatching"
    assert sessions["ITEM_T"]["current_card"]["id"] == "ITEM_T"


@pytest.mark.asyncio
async def test_check_board_multicard_per_session_in_review_preserves_monitoring_pr() -> None:
    """Bug 16.1 regression: a per-session invocation whose current_card
    is in IN_REVIEW (already tracked in active_sessions) must keep
    phase="monitoring_pr" and must NOT fall through to a path that
    overwrites the phase to "idle", which would cause routing to
    re-dispatch a performer before the human has reviewed.
    """
    from types import SimpleNamespace

    state = initial_state()
    state["github_service"] = _GitHubInReviewWithTodo()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=2,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    # Simulate per-session invocation: ITEM_R already adopted, this call
    # is for that session (current_card = ITEM_R, phase = monitoring_pr).
    state["active_sessions"] = {
        "ITEM_R": {
            "current_card": {
                "id": "ITEM_R",
                "issue_number": 100,
                "title": "Awaiting Review",
                "status": "IN_REVIEW",
            },
            "phase": "monitoring_pr",
        }
    }
    state["current_card"] = {
        "id": "ITEM_R",
        "issue_number": 100,
        "title": "Awaiting Review",
        "status": "IN_REVIEW",
    }
    state["phase"] = "monitoring_pr"

    result = await check_board(state)

    assert result["phase"] == "monitoring_pr", (
        "IN_REVIEW per-session invocation must preserve monitoring_pr; "
        f"got phase={result['phase']!r}"
    )
    # current_card must still be the same IN_REVIEW card (not switched
    # to a TODO pickup card).
    assert result["current_card"]["id"] == "ITEM_R"


@pytest.mark.asyncio
async def test_check_board_singlecard_in_review_still_short_circuits() -> None:
    """061: Single-card mode keeps the original behavior — IN_REVIEW
    routes to monitoring_pr without falling through to TODO pickup.
    """
    state = initial_state()
    state["github_service"] = _GitHubInReviewWithTodo()
    # No config / max_concurrent_cards defaults to 1

    result = await check_board(state)

    assert result["phase"] == "monitoring_pr"
    # No active_sessions populated in single-card mode for IN_REVIEW.
    assert not (result.get("active_sessions") or {})


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


class _GitHubInProgressDirty:
    async def poll_board(self):
        return {
            "snapshot": {"IN_PROGRESS": ["ITEM_DIRTY"], "TODO": ["ITEM_TODO"], "IN_REVIEW": [], "BLOCKED": []},
            "titles": {"ITEM_DIRTY": "Dirty Active Card", "ITEM_TODO": "Fresh TODO"},
            "descriptions": {"ITEM_DIRTY": "Has churn history", "ITEM_TODO": ""},
            "issue_numbers": {"ITEM_DIRTY": 89, "ITEM_TODO": 90},
        }


@pytest.mark.asyncio
async def test_check_board_readopts_dirty_in_progress_card_resets_context() -> None:
    """053: Fresh-start with no snapshot + dirty IN_PROGRESS card should
    re-adopt the active card, resume from implementing, and clear stale
    retry/feedback context before dispatch.
    """
    state = initial_state()
    state["github_service"] = _GitHubInProgressDirty()
    state["lifecycle_sequence"] = ["assessing", "architecting", "implementing", "reviewing"]
    state["performer_stage"] = "closing_review"  # stale residue from a prior run
    state["system_error_count"] = 3
    state["system_error_reason"] = "old failure"
    state["system_error_notified"] = True
    state["relay_feedback"] = [{"body": "stale"}]
    state["open_questions"] = ["stale question"]
    state["feedback_cycle_count"] = 4
    state["blocked_by_dependencies"] = [{"issue_number": 1}]

    result = await check_board(state)

    assert result["phase"] == "dispatching"
    assert result["current_card"]["id"] == "ITEM_DIRTY"
    assert result["performer_stage"] == "implementing"
    assert result["system_error_count"] == 0
    assert result["system_error_reason"] is None
    assert result["system_error_notified"] is False
    assert result["relay_feedback"] == []
    assert result["open_questions"] == []
    assert result["feedback_cycle_count"] == 0
    assert result["blocked_by_dependencies"] == []


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


class _GitHubBlockedAndTodo:
    async def poll_board(self):
        return {
            "snapshot": {"BLOCKED": ["ITEM_B"], "TODO": ["ITEM_T"], "IN_PROGRESS": [], "IN_REVIEW": []},
            "titles": {"ITEM_B": "Dirty Blocked Card", "ITEM_T": "New TODO"},
            "descriptions": {"ITEM_B": "Needs input", "ITEM_T": ""},
            "issue_numbers": {"ITEM_B": 2, "ITEM_T": 3},
        }

    async def get_issue_details(self, issue_id: str):
        return {"comments": {"nodes": []}}

    async def move_card(self, item_id: str, status: str) -> None:
        pass


@pytest.mark.asyncio
async def test_check_board_fresh_start_prioritizes_blocked_over_todo() -> None:
    """053: With no snapshot/current_card, an existing BLOCKED card should
    remain the active focus (not replaced by TODO pickup).
    """
    state = initial_state()
    state["github_service"] = _GitHubBlockedAndTodo()

    result = await check_board(state)

    assert result["phase"] == "blocked"
    assert result["current_card"]["id"] == "ITEM_B"
    assert result["current_card"]["status"] == "BLOCKED"


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


class _GitHubTransientPollFails:
    def __init__(self) -> None:
        self.calls = 0

    async def poll_board(self):
        self.calls += 1
        raise TransientGitHubError("temporary failure in name resolution")


@pytest.mark.asyncio
async def test_check_board_idle_when_poll_board_raises() -> None:
    """poll_board() exception → phase='idle' (don't crash the loop)."""
    state = initial_state()
    state["github_service"] = _GitHubPollFails()

    result = await check_board(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_check_board_defers_transient_poll_failure_with_retry_queue() -> None:
    state = initial_state()
    github = _GitHubTransientPollFails()
    state["github_service"] = github
    state["phase"] = "monitoring_pr"
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_1"}

    result = await check_board(state)

    assert result["phase"] == "monitoring_pr"
    queue = result.get("github_retry_queue") or []
    assert any(entry.get("operation") == "poll_board" for entry in queue if isinstance(entry, dict))
    assert result.get("github_retry_after") is not None
    assert github.calls == 1


@pytest.mark.asyncio
async def test_check_board_skips_poll_until_deferred_retry_is_due() -> None:
    from datetime import UTC, datetime, timedelta

    class _GitHubShouldNotBeCalled:
        def __init__(self) -> None:
            self.calls = 0

        async def poll_board(self):
            self.calls += 1
            raise AssertionError("poll_board should be deferred and not called")

    state = initial_state()
    github = _GitHubShouldNotBeCalled()
    state["github_service"] = github
    state["phase"] = "monitoring_pr"
    state["github_retry_queue"] = [
        {
            "operation": "poll_board",
            "attempt": 2,
            "retry_at": datetime.now(UTC) + timedelta(seconds=120),
            "error": "dns outage",
            "deferred_at": datetime.now(UTC),
        },
    ]

    result = await check_board(state)

    assert result["phase"] == "monitoring_pr"
    assert github.calls == 0


@pytest.mark.asyncio
async def test_check_board_system_error_when_in_progress_with_error_count() -> None:
    """Tracked in-progress card + system_error_count > 0 → phase='system_error'."""
    state = initial_state()
    state["github_service"] = _GitHubInProgress()
    state["current_card"] = {"id": "ITEM_P", "status": "IN_PROGRESS"}
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


# --- 045: check_board must refresh card metadata from the fresh board snapshot
# so a stale current_card (restored after restart without issue_number) gets
# repopulated before dispatch.  Without this, the implementer opened PRs whose
# body lacked ``Closes #N`` because score.issue_number was 0.


class _GitHubActiveCardWithMetadata:
    """Board has the active card in IN_PROGRESS with full metadata."""

    async def poll_board(self):
        return {
            "snapshot": {"TODO": [], "IN_PROGRESS": ["PVT_ACTIVE"], "IN_REVIEW": []},
            "titles": {"PVT_ACTIVE": "Refreshed Title"},
            "descriptions": {
                "PVT_ACTIVE": "Refreshed description\n- [ ] criterion one\n- [ ] criterion two"
            },
            "issue_numbers": {"PVT_ACTIVE": 89},
            "issue_urls": {"PVT_ACTIVE": "https://github.com/o/r/issues/89"},
            "content_node_ids": {"PVT_ACTIVE": "I_kwDO_abc"},
        }


@pytest.mark.asyncio
async def test_check_board_refreshes_stale_card_metadata_after_restore() -> None:
    """045: When the snapshot restore rehydrates current_card without
    issue_number (pre-045 snapshots, or fields we forgot to persist),
    check_board must repopulate issue_number/url/description from the fresh
    board poll so the next dispatch payload carries ``issue_number: 89``
    instead of ``0``.
    """
    state = initial_state()
    state["github_service"] = _GitHubActiveCardWithMetadata()
    # Stale card — what _restore_from_snapshot would produce before 045.
    state["current_card"] = {
        "id": "PVT_ACTIVE",
        "issue_id": "",
        "title": "Stale Title",
        "status": "IN_PROGRESS",
        "pr_url": None,
        "pr_node_id": None,
    }

    result = await check_board(state)

    card = result["current_card"]
    assert card["issue_number"] == 89
    assert card["issue_url"] == "https://github.com/o/r/issues/89"
    assert card["issue_id"] == "I_kwDO_abc"
    assert card["title"] == "Refreshed Title"
    assert card["acceptance_criteria"] == ["criterion one", "criterion two"]


@pytest.mark.asyncio
async def test_check_board_resets_performer_stage_on_new_card_pickup() -> None:
    """045: When picking up a fresh TODO card after a previous card left
    the pipeline in a non-initial stage (e.g., a prior card blocked at
    ``closing_review``), ``performer_stage`` must reset to
    ``lifecycle_sequence[0]``.  Without this reset the fresh card skips
    past the implementer directly to the closer, which then errors with
    ``pr_url is missing`` because the card never had a PR opened.
    """
    state = initial_state()
    state["github_service"] = _GitHub()
    # Simulate residue from a prior card that blocked at closing_review
    state["performer_stage"] = "closing_review"
    state["lifecycle_sequence"] = [
        "assessing", "architecting", "implementing", "reviewing",
        "security", "qa", "documenting", "closing_review",
    ]
    state["current_card"] = None  # new-card pickup path
    state["system_error_count"] = 2
    state["relay_feedback"] = [{"body": "stale"}]

    result = await check_board(state)

    assert result["current_card"]["id"] == "ITEM_1"
    # Stage MUST rewind to the first configured role.
    assert result["performer_stage"] == "assessing"
    # Stale per-card counters are cleared too.
    assert result["system_error_count"] == 0
    assert result["relay_feedback"] == []


@pytest.mark.asyncio
async def test_check_board_preserves_stage_when_same_card_repicked() -> None:
    """Defensive: the stage reset only fires on a genuine card transition
    (prev_card.id != item).  If check_board is called with the same card
    id already present we must NOT rewind — a re-entry after a Q&A round
    should resume at the current stage, not re-run prior stages.
    """
    state = initial_state()
    state["github_service"] = _GitHub()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["performer_stage"] = "reviewing"

    result = await check_board(state)

    assert result["performer_stage"] == "reviewing"


@pytest.mark.asyncio
async def test_check_board_refresh_preserves_pr_fields() -> None:
    """The refresh path only touches board-derived fields — pr_url/pr_node_id
    live on the card via _advance_stage and must survive the refresh.
    """
    state = initial_state()
    state["github_service"] = _GitHubActiveCardWithMetadata()
    state["current_card"] = {
        "id": "PVT_ACTIVE",
        "issue_id": "",
        "title": "Stale Title",
        "status": "IN_PROGRESS",
        "pr_url": "https://github.com/o/r/pull/94",
        "pr_node_id": "PR_NODE_94",
    }

    result = await check_board(state)

    card = result["current_card"]
    assert card["pr_url"] == "https://github.com/o/r/pull/94"
    assert card["pr_node_id"] == "PR_NODE_94"
    assert card["issue_number"] == 89  # refresh still ran


# --- 046: Card dependency detection ---


class _GitHubDependency:
    """Board with two TODO cards where B depends on A."""

    async def poll_board(self):
        return {
            "snapshot": {"TODO": ["ITEM_A", "ITEM_B"], "IN_PROGRESS": [], "IN_REVIEW": [], "DONE": []},
            "titles": {"ITEM_A": "Set up theming", "ITEM_B": "Add dark mode"},
            "descriptions": {"ITEM_A": "", "ITEM_B": "Depends on #1"},
            "issue_numbers": {"ITEM_A": 1, "ITEM_B": 2},
            "issue_urls": {},
            "content_node_ids": {},
        }


class _GitHubDependencySatisfied:
    """Board where A is DONE, so B's dependency is satisfied."""

    async def poll_board(self):
        return {
            "snapshot": {"TODO": ["ITEM_B"], "DONE": ["ITEM_A"], "IN_PROGRESS": [], "IN_REVIEW": []},
            "titles": {"ITEM_A": "Set up theming", "ITEM_B": "Add dark mode"},
            "descriptions": {"ITEM_A": "", "ITEM_B": "Depends on #1"},
            "issue_numbers": {"ITEM_A": 1, "ITEM_B": 2},
            "issue_urls": {},
            "content_node_ids": {},
        }


class _GitHubCircularDeps:
    """Board where A depends on B and B depends on A."""

    async def poll_board(self):
        return {
            "snapshot": {"TODO": ["ITEM_A", "ITEM_B"], "IN_PROGRESS": [], "IN_REVIEW": [], "DONE": []},
            "titles": {"ITEM_A": "Module A", "ITEM_B": "Module B"},
            "descriptions": {"ITEM_A": "Depends on #2", "ITEM_B": "Depends on #1"},
            "issue_numbers": {"ITEM_A": 1, "ITEM_B": 2},
            "issue_urls": {},
            "content_node_ids": {},
        }


@pytest.mark.asyncio
async def test_check_board_filters_dependent_todo_cards() -> None:
    """046: Card B depends on card A (both TODO). Only A should be dispatched."""
    state = initial_state()
    state["github_service"] = _GitHubDependency()

    result = await check_board(state)

    assert result["phase"] == "dispatching"
    card = result["current_card"]
    assert card["id"] == "ITEM_A"
    assert card["title"] == "Set up theming"


@pytest.mark.asyncio
async def test_check_board_dispatches_when_dependency_satisfied() -> None:
    """046: Card A is DONE → B's dependency is satisfied → B dispatched."""
    state = initial_state()
    state["github_service"] = _GitHubDependencySatisfied()

    result = await check_board(state)

    assert result["phase"] == "dispatching"
    assert result["current_card"]["id"] == "ITEM_B"


@pytest.mark.asyncio
async def test_check_board_blocks_circular_dependencies() -> None:
    """046: A depends on B, B depends on A → neither dispatched (both
    filtered out). Board ends up idle since no eligible TODO remains."""
    state = initial_state()
    state["github_service"] = _GitHubCircularDeps()

    result = await check_board(state)

    # Both filtered — no card dispatched
    assert result.get("phase") != "dispatching" or result.get("current_card") is None


@pytest.mark.asyncio
async def test_check_board_off_board_dep_treated_as_unresolvable() -> None:
    """046: Card depends on #999 which isn't on the board → UNRESOLVABLE
    → card filtered out of eligible TODO."""

    class _GitHubOffBoard:
        async def poll_board(self):
            return {
                "snapshot": {"TODO": ["ITEM_X"], "IN_PROGRESS": [], "IN_REVIEW": [], "DONE": []},
                "titles": {"ITEM_X": "Some feature"},
                "descriptions": {"ITEM_X": "Depends on #999"},
                "issue_numbers": {"ITEM_X": 50},
                "issue_urls": {},
                "content_node_ids": {},
            }

    state = initial_state()
    state["github_service"] = _GitHubOffBoard()

    result = await check_board(state)

    # Card filtered — idle since no eligible TODO
    assert result.get("phase") != "dispatching" or result.get("current_card") is None


@pytest.mark.asyncio
async def test_check_board_unresolvable_dep_moves_card_to_blocked() -> None:
    """046 FR-010: Cards with UNRESOLVABLE deps (off-board, not closed)
    must be moved to BLOCKED with a diagnostic comment — not silently
    left in TODO.  Verify move_card + add_comment are called."""

    class _GitHubUnresolvable:
        def __init__(self):
            self.move_calls: list[tuple[str, str]] = []
            self.comments: list[tuple[str, str]] = []

        async def poll_board(self):
            return {
                "snapshot": {"TODO": ["ITEM_X"], "IN_PROGRESS": [], "IN_REVIEW": [], "DONE": []},
                "titles": {"ITEM_X": "Some feature"},
                "descriptions": {"ITEM_X": "Depends on #999"},
                "issue_numbers": {"ITEM_X": 50},
                "issue_urls": {},
                "content_node_ids": {"ITEM_X": "I_kwDO_X"},
            }

        async def check_issue_state(self, repo, issue_number):
            return "open"  # not closed → UNRESOLVABLE

        async def move_card(self, item_id, status):
            self.move_calls.append((item_id, status))

        async def add_comment(self, subject_id, body):
            self.comments.append((subject_id, body))

    from types import SimpleNamespace

    gh = _GitHubUnresolvable()
    state = initial_state()
    state["github_service"] = gh
    state["config"] = SimpleNamespace(
        github_org="TestOrg", project_name="test-repo",
        max_concurrent_cards=1, priority=SimpleNamespace(field_name=""),
    )

    await check_board(state)

    assert ("ITEM_X", "BLOCKED") in gh.move_calls
    assert len(gh.comments) == 1
    assert gh.comments[0][0] == "I_kwDO_X"
    assert "#999" in gh.comments[0][1]
    assert "not on the project board" in gh.comments[0][1]


@pytest.mark.asyncio
async def test_check_board_off_board_closed_blocker_satisfies_dependency() -> None:
    """046: Card depends on #999 (not on board), but github reports the
    issue as closed → dependency SATISFIED → card dispatched normally.

    This exercises the resolve_off_board_dependencies path that calls
    github.check_issue_state and upgrades UNRESOLVABLE → SATISFIED.
    """
    from types import SimpleNamespace

    class _GitHubOffBoardClosed:
        async def poll_board(self):
            return {
                "snapshot": {"TODO": ["ITEM_X"], "IN_PROGRESS": [], "IN_REVIEW": [], "DONE": []},
                "titles": {"ITEM_X": "Some feature"},
                "descriptions": {"ITEM_X": "Depends on #999"},
                "issue_numbers": {"ITEM_X": 50},
                "issue_urls": {},
                "content_node_ids": {},
            }

        async def check_issue_state(self, repo: str, issue_number: int) -> str:
            return "closed"

    state = initial_state()
    state["github_service"] = _GitHubOffBoardClosed()
    state["config"] = SimpleNamespace(
        github_org="TestOrg", project_name="test-repo",
        max_concurrent_cards=1, priority=SimpleNamespace(field_name=""),
    )

    result = await check_board(state)

    assert result["phase"] == "dispatching"
    assert result["current_card"]["id"] == "ITEM_X"


# ---------------------------------------------------------------------------
# 054: skip-reason state shape (T006)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_board_preserves_session_skip_reasons_field() -> None:
    """054 T006: session_skip_reasons is present in initial_state and check_board
    returns the field unchanged (it is managed by daemon, not check_board)."""
    state = initial_state()
    assert "session_skip_reasons" in state
    assert isinstance(state["session_skip_reasons"], dict)

    state["github_service"] = _GitHub()
    state["session_skip_reasons"] = {"card-A": {"reason": "blocked_column"}}

    result = await check_board(state)

    # check_board must not clobber session_skip_reasons
    assert result.get("session_skip_reasons") == {"card-A": {"reason": "blocked_column"}}


@pytest.mark.asyncio
async def test_check_board_session_skip_reasons_empty_by_default() -> None:
    """054 T006: initial_state always provides an empty dict for session_skip_reasons."""
    state = initial_state()
    assert state["session_skip_reasons"] == {}


# ---------------------------------------------------------------------------
# 054: rebase dispatch targeting / failure isolation (T016, T017)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_board_rebase_not_dispatched_when_no_active_sessions() -> None:
    """054 T017: When there are no active sessions, run_rebase_round is never called
    even if the main SHA changes."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    class _GitHubWithToken:
        async def poll_board(self):
            return {
                "snapshot": {"TODO": ["ITEM_1"]},
                "titles": {"ITEM_1": "Pending"},
                "descriptions": {"ITEM_1": ""},
                "issue_numbers": {"ITEM_1": 10},
                "issue_urls": {},
                "content_node_ids": {},
            }

        async def _current_token(self):
            return "test-token"

    state = initial_state()
    state["github_service"] = _GitHubWithToken()
    state["config"] = SimpleNamespace(
        github_org="acme", project_name="repo",
        max_concurrent_cards=2, priority=SimpleNamespace(field_name=""),
        github_api_url="",
    )
    # No active sessions → rebase block is skipped entirely
    state["active_sessions"] = {}
    state["last_known_main_sha"] = "old-sha-000"

    with patch("coordinare.services.rebase.run_rebase_round", new=AsyncMock()) as mock_rebase:
        await check_board(state)

    mock_rebase.assert_not_called()


@pytest.mark.asyncio
async def test_check_board_rebase_dispatched_for_open_pr_sessions() -> None:
    """054 T016: When an active session has an open PR and main SHA changes,
    run_rebase_round is called and its result is stored."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    class _GitHubWithToken:
        async def poll_board(self):
            return {
                "snapshot": {"IN_REVIEW": ["ITEM_2"]},
                "titles": {"ITEM_2": "Reviewed"},
                "descriptions": {"ITEM_2": ""},
                "issue_numbers": {"ITEM_2": 20},
                "issue_urls": {},
                "content_node_ids": {},
            }

        async def _current_token(self):
            return "test-token"

    from coordinare.models.rebase import RebaseJob, RebaseOutcome, RebaseRound

    rr = RebaseRound(
        trigger_sha="new-sha-222",
        jobs=[RebaseJob(card_id="ITEM_2", branch="feat/item-2", outcome=RebaseOutcome.SKIPPED)],
    )

    state = initial_state()
    state["github_service"] = _GitHubWithToken()
    state["config"] = SimpleNamespace(
        github_org="acme", project_name="repo",
        max_concurrent_cards=2, priority=SimpleNamespace(field_name=""),
        github_api_url="",
    )
    state["active_sessions"] = {
        "ITEM_2": {
            "current_card": {"id": "ITEM_2", "pr_url": "https://github.com/acme/repo/pull/20"},
            "workspace_branch": "coordinare/item-2",
            "phase": "monitoring_performer",
        },
    }
    state["last_known_main_sha"] = "old-sha-111"

    mock_rebase = AsyncMock(return_value=rr)
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="new-sha-222")),
        patch("coordinare.services.rebase.run_rebase_round", new=mock_rebase),
    ):
        result = await check_board(state)

    assert result.get("last_known_main_sha") == "new-sha-222"
    mock_rebase.assert_called_once()
    assert result.get("last_rebase_round") is not None


@pytest.mark.asyncio
async def test_check_board_rebase_exception_does_not_abort_cycle() -> None:
    """054 T016: If run_rebase_round raises, check_board logs and continues normally."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    class _GitHubWithToken:
        async def poll_board(self):
            return {
                "snapshot": {"IN_REVIEW": ["ITEM_3"]},
                "titles": {"ITEM_3": "Card"},
                "descriptions": {"ITEM_3": ""},
                "issue_numbers": {"ITEM_3": 30},
                "issue_urls": {},
                "content_node_ids": {},
            }

        async def _current_token(self):
            return "test-token"

    state = initial_state()
    state["github_service"] = _GitHubWithToken()
    state["config"] = SimpleNamespace(
        github_org="acme", project_name="repo",
        max_concurrent_cards=2, priority=SimpleNamespace(field_name=""),
        github_api_url="",
    )
    state["active_sessions"] = {
        "ITEM_3": {
            "current_card": {"id": "ITEM_3", "pr_url": "https://github.com/acme/repo/pull/30"},
            "workspace_branch": "coordinare/item-3",
            "phase": "monitoring_performer",
        },
    }
    state["last_known_main_sha"] = "old-sha-aaa"

    mock_rebase = AsyncMock(side_effect=RuntimeError("network failure"))
    with (
        patch("coordinare.services.rebase.fetch_main_sha", new=AsyncMock(return_value="new-sha-bbb")),
        patch("coordinare.services.rebase.run_rebase_round", new=mock_rebase),
    ):
        result = await check_board(state)

    # Should not raise; last_known_main_sha updated even on exception
    assert result.get("last_known_main_sha") == "new-sha-bbb"
    # run_rebase_round must have been attempted (session is now detectable as stale)
    mock_rebase.assert_called_once()
