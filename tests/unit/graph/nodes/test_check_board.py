from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.graph.nodes.check_board import (
    _allowed_github_host,
    _card_docs_dir,
    _dep_announcement_signature,
    _derive_resume_stage,
    _safe_issue_url,
    _sort_by_priority,
    check_board,
)
from coordinare.graph.state import initial_state
from coordinare.services.github import TransientGitHubError

_LIFECYCLE = [
    "assessing", "architecting", "implementing",
    "reviewing", "security", "qa", "documenting", "closing_review",
]


def _fake_github(present_files: set[str]) -> object:
    """Fake GitHubService whose get_file_blob_sha returns a sha only for paths
    whose basename is in *present_files*."""
    gh = SimpleNamespace(_org="ViviDynamics", _project_name="website")

    async def _blob_sha(owner, repo, path, ref="HEAD"):
        return "deadbeef" if path.rsplit("/", 1)[-1] in present_files else None

    gh.get_file_blob_sha = _blob_sha
    return gh


_CARD_150 = {"id": "PVTI_x", "issue_number": 150,
             "title": "Feature: Time tracking schema and model foundation"}


class TestDeriveResumeStage:
    def test_card_docs_dir_matches_performer_slug(self) -> None:
        # Must match agent/performer _doc_folder: lowercased, non-alnum->dash, [:20].
        assert _card_docs_dir(_CARD_150) == "docs/cards/150-feature-time-trackin"

    def test_card_docs_dir_none_for_missing_title(self) -> None:
        assert _card_docs_dir({"issue_number": 1}) is None

    def test_card_docs_dir_without_issue_number(self) -> None:
        # No issue_number => slug-only path (still mirrors the performer).
        assert _card_docs_dir({"title": "Some Card!"}) == "docs/cards/some-card"

    @pytest.mark.asyncio
    async def test_all_mapped_stages_complete_returns_last(self) -> None:
        # When every lifecycle stage has a doc artifact and all are present,
        # the scan falls through to the final stage (no unmapped resume point).
        gh = _fake_github({"assessment.md", "plan.md", "tasks.md"})
        assert await _derive_resume_stage(gh, _CARD_150, ["assessing", "architecting"]) == "architecting"

    @pytest.mark.asyncio
    async def test_empty_lifecycle_returns_none(self) -> None:
        gh = _fake_github({"assessment.md"})
        assert await _derive_resume_stage(gh, _CARD_150, []) is None

    @pytest.mark.asyncio
    async def test_assessment_only_resumes_at_architecting(self) -> None:
        # The #150 case: assessor ran (assessment.md) but architect never did.
        gh = _fake_github({"assessment.md"})
        assert await _derive_resume_stage(gh, _CARD_150, _LIFECYCLE) == "architecting"

    @pytest.mark.asyncio
    async def test_plan_and_tasks_present_resumes_at_implementing(self) -> None:
        gh = _fake_github({"assessment.md", "plan.md", "tasks.md"})
        assert await _derive_resume_stage(gh, _CARD_150, _LIFECYCLE) == "implementing"

    @pytest.mark.asyncio
    async def test_partial_architect_output_still_resumes_at_architecting(self) -> None:
        # plan.md without tasks.md => architecting NOT complete.
        gh = _fake_github({"assessment.md", "plan.md"})
        assert await _derive_resume_stage(gh, _CARD_150, _LIFECYCLE) == "architecting"

    @pytest.mark.asyncio
    async def test_no_artifacts_resumes_at_first_stage(self) -> None:
        gh = _fake_github(set())  # no branch / no artifacts
        assert await _derive_resume_stage(gh, _CARD_150, _LIFECYCLE) == "assessing"

    @pytest.mark.asyncio
    async def test_none_github_returns_none(self) -> None:
        assert await _derive_resume_stage(None, _CARD_150, _LIFECYCLE) is None

    @pytest.mark.asyncio
    async def test_github_error_is_non_fatal(self) -> None:
        gh = SimpleNamespace(_org="o", _project_name="r")
        gh.get_file_blob_sha = AsyncMock(side_effect=RuntimeError("boom"))
        assert await _derive_resume_stage(gh, _CARD_150, _LIFECYCLE) is None

    @pytest.mark.asyncio
    async def test_missing_owner_repo_returns_none(self) -> None:
        gh = SimpleNamespace()  # no _org/_project_name
        gh.get_file_blob_sha = AsyncMock(return_value="x")
        assert await _derive_resume_stage(gh, _CARD_150, _LIFECYCLE) is None

    @pytest.mark.asyncio
    async def test_check_board_readopt_applies_derived_stage(self) -> None:
        """Integration: a re-adopted IN_PROGRESS card with no snapshot stage has
        its derived resume stage applied to the session (covers the wiring in
        the check_board re-adopt loop).  assessment.md present, no plan/tasks =>
        the session resumes at architecting, NOT the "implementing" default."""
        github = AsyncMock()
        github.poll_board.return_value = {
            "snapshot": {"TODO": [], "IN_PROGRESS": ["PVI_1"]},
            "titles": {"PVI_1": "Feature: Time tracking schema and model foundation"},
            "descriptions": {"PVI_1": "d"},
            "issue_numbers": {"PVI_1": 150},
            "issue_urls": {"PVI_1": "https://github.com/issues/150"},
            "content_node_ids": {"PVI_1": "node_PVI_1"},
            "item_labels": {},
            "item_field_values": {},
        }
        github._org = "ViviDynamics"
        github._project_name = "website"

        async def _blob(owner, repo, path, ref="HEAD"):
            return "sha" if path.endswith("assessment.md") else None

        github.get_file_blob_sha = _blob

        state = initial_state()
        state["config"] = MagicMock(max_concurrent_cards=3, priority=MagicMock(field_name=None))
        state["github_service"] = github
        state["lifecycle_sequence"] = _LIFECYCLE

        result = await check_board(state)

        sess = result["active_sessions"]["PVI_1"]
        assert sess["performer_stage"] == "architecting"


class TestAllowedGithubHost:
    def test_none_config_defaults_to_github_com(self) -> None:
        assert _allowed_github_host(None) == "github.com"

    def test_missing_attribute_defaults_to_github_com(self) -> None:
        assert _allowed_github_host(SimpleNamespace()) == "github.com"

    def test_empty_endpoint_defaults_to_github_com(self) -> None:
        assert _allowed_github_host(SimpleNamespace(github_endpoint="")) == "github.com"

    def test_ghe_endpoint(self) -> None:
        cfg = SimpleNamespace(github_endpoint="https://ghe.corp.example/api/v3")
        assert _allowed_github_host(cfg) == "ghe.corp.example"

    def test_api_github_maps_to_github_com(self) -> None:
        cfg = SimpleNamespace(github_endpoint="https://api.github.com")
        assert _allowed_github_host(cfg) == "github.com"

    def test_uppercase_host_normalized(self) -> None:
        cfg = SimpleNamespace(github_endpoint="https://GHE.CORP.EXAMPLE/api/v3")
        assert _allowed_github_host(cfg) == "ghe.corp.example"


class TestSafeIssueUrl:
    def test_matching_host_returned(self) -> None:
        url = "https://github.com/owner/repo/issues/42"
        assert _safe_issue_url(url, "github.com") == url

    def test_host_mismatch_rejected(self) -> None:
        assert _safe_issue_url("https://evil.com/x", "github.com") is None

    def test_javascript_scheme_rejected(self) -> None:
        assert _safe_issue_url("javascript:alert(1)", "github.com") is None

    def test_data_scheme_rejected(self) -> None:
        assert _safe_issue_url("data:text/html,<script>", "github.com") is None

    def test_empty_url_returns_none(self) -> None:
        assert _safe_issue_url("", "github.com") is None

    def test_empty_allowed_host_returns_none(self) -> None:
        assert _safe_issue_url("https://github.com/x", "") is None
        assert _safe_issue_url("https://github.com/x", None) is None

    def test_http_allowed(self) -> None:
        url = "http://github.com/x"
        assert _safe_issue_url(url, "github.com") == url


class TestDepAnnouncementSignature:
    def test_blocker_order_independent(self) -> None:
        a = _dep_announcement_signature("ITEM_1", "blocked", [3, 1, 2])
        b = _dep_announcement_signature("ITEM_1", "blocked", [2, 3, 1])
        assert a == b

    def test_reason_changes_signature(self) -> None:
        a = _dep_announcement_signature("ITEM_1", "blocked", [1])
        b = _dep_announcement_signature("ITEM_1", "unblocked", [1])
        assert a != b

    def test_item_id_changes_signature(self) -> None:
        a = _dep_announcement_signature("ITEM_1", "blocked", [1])
        b = _dep_announcement_signature("ITEM_2", "blocked", [1])
        assert a != b

    def test_blocker_set_changes_signature(self) -> None:
        a = _dep_announcement_signature("ITEM_1", "blocked", [1, 2])
        b = _dep_announcement_signature("ITEM_1", "blocked", [1, 2, 3])
        assert a != b

    def test_empty_blockers(self) -> None:
        sig = _dep_announcement_signature("ITEM_1", "reason", [])
        assert sig == "ITEM_1|reason|"


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
                "descriptions": dict.fromkeys(assignees_by_item, ""),
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
                ],
            },
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
    # Questions and cutoff belong to this legacy flat card, not anonymous history.
    state["current_card"] = {"id": "ITEM_B", "status": "BLOCKED"}
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
async def test_check_board_blocked_card_uses_session_watermark_when_top_level_is_none() -> None:
    """069 follow-up: after a daemon restart, the top-level
    ``last_blocked_notified_at`` mirror is None but the per-session watermark
    survives via PersistedSession. check_board must fall back to the session
    value when detecting new user comments — otherwise the card stays blocked
    forever and never picks up the user's reply.
    """
    state = initial_state()
    github = _GitHubBlockedWithNewComment()
    state["github_service"] = github
    state["last_blocked_notified_at"] = None
    state["active_sessions"] = {
        "ITEM_B": {
            "current_card": {"id": "ITEM_B"},
            "phase": "blocked",
            "performer_stage": "implementing",
            "last_blocked_notified_at": datetime(2026, 2, 25, 10, 0, tzinfo=UTC),
        },
    }
    state["open_questions"] = ["What routes need breadcrumbs?"]

    result = await check_board(state)

    assert result["phase"] == "dispatching"
    assert github.moved_to == "IN_PROGRESS"
    assert len(result["card_clarifications"]) == 1
    assert result["card_clarifications"][0]["answer"] == "Here is the answer"


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

    # 069 follow-up: a still-blocked card must stay phase="blocked" so the
    # session sits in NON_SLOT_PHASES and frees the concurrency slot.
    # handle_blocked's own dedup gates the reminder re-post.
    assert result["phase"] == "blocked"


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
                ],
            },
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
    # Questions and cutoff belong to this legacy flat card, not anonymous history.
    state["current_card"] = {"id": "ITEM_B", "status": "BLOCKED"}
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
                ],
            },
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
    # Questions and cutoff belong to this legacy flat card, not anonymous history.
    state["current_card"] = {"id": "ITEM_B", "status": "BLOCKED"}
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
                    },
                ],
            },
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
async def test_check_board_in_review_with_stale_blocked_phase_still_picks_up_todo() -> None:
    """087 regression (sticky-blocked-phase trap): if a prior cycle left the
    global ``phase`` at "blocked" (set when a BLOCKED card was handled) and an
    IN_REVIEW card is present, the IN_REVIEW early-return must NOT short-circuit
    TODO pickup while concurrency slots are free.

    Observed in production: symphony "website" sat at ``phase=blocked`` with 0
    sessions and 3 free slots forever; TODO cards #124/#125 were never
    dispatched because the IN_REVIEW branch returned early (it was not
    slot-aware, unlike the IN_PROGRESS branch).
    """
    from types import SimpleNamespace

    state = initial_state()
    state["github_service"] = _GitHubInReviewWithTodo()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=3,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    state["active_sessions"] = {}
    # The trap trigger: a stale global phase persisted from an earlier cycle.
    state["phase"] = "blocked"

    result = await check_board(state)

    sessions = result.get("active_sessions") or {}
    assert "ITEM_R" in sessions, "IN_REVIEW card should be re-adopted into active_sessions"
    assert "ITEM_T" in sessions, (
        "TODO card must be picked up despite stale phase=blocked and a free slot; "
        f"got sessions={list(sessions)}"
    )
    assert sessions["ITEM_T"]["phase"] == "dispatching"


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
        },
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
async def test_check_board_singlecard_in_review_adopts_into_active_sessions() -> None:
    """066 FR-009: IN_REVIEW unification.  Even in single-card mode the
    re-adopted IN_REVIEW card lands in ``active_sessions`` so all flat-state
    reads can be migrated to the session map without a separate code path.
    The phase still routes to ``monitoring_pr``.
    """
    state = initial_state()
    state["github_service"] = _GitHubInReviewWithTodo()
    # No config / max_concurrent_cards defaults to 1

    result = await check_board(state)

    assert result["phase"] == "monitoring_pr"
    sessions = result.get("active_sessions") or {}
    assert "ITEM_R" in sessions, f"IN_REVIEW card must be adopted: {sessions}"
    assert sessions["ITEM_R"].get("phase") == "monitoring_pr"
    assert result["current_card"]["id"] == "ITEM_R"


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
    """069: a freshly-readopted IN_PROGRESS card has no agent_dispatch.session_id,
    so monitor_performer cannot poll status — readopt must leave the session at
    phase="dispatching" so a fresh container spins up rather than burning the
    system_error retry budget on transport errors against a non-existent job.
    Snapshot-restored sessions that carry a real session_id are handled by Fix
    22 earlier in check_board and never reach the readopt loop.
    """
    state = initial_state()
    state["github_service"] = _GitHubInProgress()

    result = await check_board(state)

    sessions = result.get("active_sessions") or {}
    assert "ITEM_P" in sessions
    assert sessions["ITEM_P"]["phase"] == "dispatching"
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

    # 069: fresh-readopt has no session_id → phase=dispatching so a new
    # container spins up. The fresh CardSession from create_session_from_card
    # zeroes stale per-card residue; the flat-state mirror reflects the
    # re-derived session.
    sessions = result.get("active_sessions") or {}
    assert "ITEM_DIRTY" in sessions
    assert sessions["ITEM_DIRTY"]["phase"] == "dispatching"
    assert result["current_card"]["id"] == "ITEM_DIRTY"
    assert result["system_error_count"] == 0
    assert result["system_error_reason"] is None
    assert result["system_error_notified"] is False
    assert result["relay_feedback"] == []
    assert result["open_questions"] == []
    assert result["feedback_cycle_count"] == 0
    assert result["blocked_by_dependencies"] == []


class _GitHubInProgressWithTodos:
    async def poll_board(self):
        return {
            "snapshot": {
                "IN_PROGRESS": ["ITEM_P1"],
                "TODO": ["ITEM_T1", "ITEM_T2"],
                "IN_REVIEW": [],
                "BLOCKED": [],
            },
            "titles": {
                "ITEM_P1": "Already In Progress",
                "ITEM_T1": "Fresh TODO 1",
                "ITEM_T2": "Fresh TODO 2",
            },
            "descriptions": {"ITEM_P1": "", "ITEM_T1": "", "ITEM_T2": ""},
            "issue_numbers": {"ITEM_P1": 200, "ITEM_T1": 201, "ITEM_T2": 202},
            "issue_urls": {},
            "content_node_ids": {},
        }


@pytest.mark.asyncio
async def test_check_board_multicard_readopts_in_progress_and_picks_up_todos() -> None:
    """065 US2 regression: in multi-card mode, an IN_PROGRESS card must NOT
    short-circuit the function — it should be re-adopted into
    ``active_sessions`` AND the function must fall through to TODO pickup so
    the remaining concurrency slots fill in the same cycle.  Without the
    fall-through, a single IN_PROGRESS card silently serialises work to one
    card at a time even when ``max_concurrent_cards > 1``.
    """
    state = initial_state()
    state["github_service"] = _GitHubInProgressWithTodos()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=3,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    state["active_sessions"] = {}

    result = await check_board(state)

    sessions = result.get("active_sessions") or {}
    assert "ITEM_P1" in sessions, (
        "IN_PROGRESS card must be re-adopted into active_sessions"
    )
    assert sessions["ITEM_P1"]["phase"] == "dispatching"
    assert sessions["ITEM_P1"]["current_card"]["status"] == "IN_PROGRESS"
    assert "ITEM_T1" in sessions, "First TODO must be picked up in same cycle"
    assert "ITEM_T2" in sessions, "Second TODO must be picked up in same cycle"
    assert sessions["ITEM_T1"]["phase"] == "dispatching"
    assert sessions["ITEM_T2"]["phase"] == "dispatching"
    assert len(sessions) == 3, (
        f"Expected 3 sessions filling cap=3, got {len(sessions)}"
    )


@pytest.mark.asyncio
async def test_check_board_multicard_per_session_in_progress_preserves_monitoring_agent() -> None:
    """065 US2: a per-session invocation whose current_card is itself
    IN_PROGRESS must preserve phase=monitoring_agent (not fall through to
    TODO pickup and clobber its current_card)."""
    state = initial_state()
    state["github_service"] = _GitHubInProgressWithTodos()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=3,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    state["active_sessions"] = {
        "ITEM_P1": {
            "current_card": {
                "id": "ITEM_P1",
                "issue_number": 200,
                "title": "Already In Progress",
                "status": "IN_PROGRESS",
            },
            "phase": "monitoring_agent",
        },
    }
    state["current_card"] = {
        "id": "ITEM_P1",
        "issue_number": 200,
        "title": "Already In Progress",
        "status": "IN_PROGRESS",
    }

    result = await check_board(state)

    assert result["phase"] == "monitoring_agent"
    assert result["current_card"]["id"] == "ITEM_P1"


@pytest.mark.asyncio
async def test_check_board_in_progress_readopt_preserves_snapshot_stage_v1() -> None:
    """065 Fix 7a: when a v1 snapshot restored top-level performer_stage and
    current_card.id matches the IN_PROGRESS card being re-adopted, the
    re-adopt path must override the default ``implementing`` stage from
    create_session_from_card with the snapshot stage."""
    state = initial_state()
    state["github_service"] = _GitHubInProgressWithTodos()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=3,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    # v1 snapshot restored: current_card + top-level performer_stage, no active_sessions
    state["active_sessions"] = {}
    state["current_card"] = {"id": "ITEM_P1"}
    state["performer_stage"] = "closing_review"

    result = await check_board(state)

    sessions = result.get("active_sessions") or {}
    assert "ITEM_P1" in sessions
    assert sessions["ITEM_P1"]["performer_stage"] == "closing_review", (
        "Snapshot-restored stage must survive re-adopt instead of being clobbered "
        "to 'implementing' by create_session_from_card."
    )


@pytest.mark.asyncio
async def test_check_board_in_progress_readopt_uses_persisted_session_stage() -> None:
    """065 Fix 7b: when active_sessions already has the card with a
    persisted performer_stage, prefer it (even if the card_id doesn't match
    current_card)."""
    state = initial_state()
    state["github_service"] = _GitHubInProgressWithTodos()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=3,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    # v2 snapshot restored: active_sessions stubs carrying performer_stage
    state["active_sessions"] = {
        "ITEM_P1": {"id": "ITEM_P1", "performer_stage": "reviewing"},
    }

    result = await check_board(state)

    sessions = result.get("active_sessions") or {}
    assert sessions["ITEM_P1"]["performer_stage"] == "reviewing"


@pytest.mark.asyncio
async def test_check_board_singlecard_in_progress_still_short_circuits() -> None:
    """065 US2: single-card mode must keep the original behaviour — an
    IN_PROGRESS card does NOT fall through to TODO pickup."""
    state = initial_state()
    state["github_service"] = _GitHubInProgressWithTodos()
    # No config / max_concurrent_cards defaults to 1.

    result = await check_board(state)

    # Single-card: re-adopt the IN_PROGRESS card and dispatch — TODO cards
    # are NOT picked up in the same cycle.
    assert result["current_card"]["id"] == "ITEM_P1"
    # 066: unified path populates active_sessions even at N=1 — only the
    # re-adopted IN_PROGRESS card; TODOs are not picked up this cycle.
    sessions = result.get("active_sessions") or {}
    assert set(sessions.keys()) == {"ITEM_P1"}


@pytest.mark.asyncio
async def test_check_board_multicard_per_session_monitoring_performer_falls_through_to_todo_pickup() -> None:
    """065 Fix 5 regression: in multi-card steady state, a per-session
    invocation arrives with ``phase="monitoring_performer"`` and a
    ``current_card`` pointing at its IN_PROGRESS card.  The phase-preservation
    guard at the top of the in_progress branch previously short-circuited
    here, so the 035 multi-card TODO pickup was never reached and the daemon
    silently serialised work to one card at a time.  The primary in-flight
    session must fall through to TODO pickup when open concurrency slots
    remain, while preserving its own phase/current_card.
    """
    state = initial_state()
    state["github_service"] = _GitHubInProgressWithTodos()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=3,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    # Steady-state per-session invocation: monitoring_performer carried in
    # via session_to_state, current_card is the IN_PROGRESS card, the session
    # already lives in active_sessions consuming one slot.
    state["phase"] = "monitoring_performer"
    state["current_card"] = {
        "id": "ITEM_P1",
        "issue_number": 200,
        "title": "Already In Progress",
        "status": "IN_PROGRESS",
    }
    state["active_sessions"] = {
        "ITEM_P1": {
            "current_card": dict(state["current_card"]),
            "phase": "monitoring_performer",
        },
    }

    result = await check_board(state)

    # The owning session preserves its own state…
    assert result["phase"] == "monitoring_performer", (
        "phase must NOT be downgraded from monitoring_performer"
    )
    assert result["current_card"]["id"] == "ITEM_P1"
    # …AND the per-cycle invocation must have filled the two open slots.
    sessions = result.get("active_sessions") or {}
    assert "ITEM_T1" in sessions, "first TODO should fill the second slot"
    assert "ITEM_T2" in sessions, "second TODO should fill the third slot"
    assert sessions["ITEM_P1"]["phase"] == "monitoring_performer"


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
                ],
            },
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
async def test_check_board_multicard_blocked_does_not_starve_todo_pickup() -> None:
    """Regression: in multi-card mode, a single BLOCKED card on the board
    must not block TODO pickup.  A blocked card is waiting on a human and
    holds no live performer, so the remaining concurrency slots should be
    filled from TODO.  Phase stays "blocked" so the router still sends the
    cycle to handle_blocked for the reminder; newly added active_sessions
    entries get dispatched on the next cycle.
    """
    from types import SimpleNamespace

    state = initial_state()
    state["github_service"] = _GitHubBlockedAndTodo()
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
    assert "ITEM_T" in sessions, (
        "TODO card must be picked up even when a BLOCKED card is present "
        "in multi-card mode"
    )
    assert sessions["ITEM_T"]["current_card"]["id"] == "ITEM_T"
    # Blocked card remains the cycle's current_card so handle_blocked can
    # post the reminder; phase stays "blocked".
    assert result["phase"] == "blocked"
    assert result["current_card"]["id"] == "ITEM_B"


@pytest.mark.asyncio
async def test_check_board_multicard_per_session_invocation_preserves_todo_current_card() -> None:
    """Regression: in multi-card mode, per-session graph invocations re-enter
    check_board with state["current_card"] already populated by
    session_to_state.  When the current session is processing a TODO card
    and a different card is BLOCKED on the board, the BLOCKED branch must
    NOT overwrite state["current_card"] — otherwise state_to_session writes
    the BLOCKED card's data back into the TODO session, and the dashboard
    swimlane shows every TODO session as the BLOCKED card.
    """
    from types import SimpleNamespace

    state = initial_state()
    state["github_service"] = _GitHubBlockedAndTodo()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=2,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    # Simulate a per-session invocation: a TODO session is already loaded
    # (session_to_state set current_card to the TODO card before invoking
    # the graph).  active_sessions also already contains this session.
    todo_card = {
        "id": "ITEM_T",
        "issue_id": "",
        "issue_number": 3,
        "issue_url": "",
        "title": "New TODO",
        "description": "",
        "acceptance_criteria": [],
        "status": "TODO",
        "previous_status": "TODO",
    }
    state["current_card"] = todo_card
    state["phase"] = "dispatching"
    state["active_sessions"] = {
        "ITEM_T": {"current_card": todo_card, "phase": "dispatching"},
    }

    result = await check_board(state)

    # The TODO session's current_card must be preserved — NOT replaced with
    # the BLOCKED card's data.
    assert result["current_card"]["id"] == "ITEM_T", (
        f"BLOCKED branch clobbered TODO session's current_card: "
        f"{result['current_card']}"
    )
    assert result["current_card"]["status"] == "TODO"
    # Phase must not be flipped to "blocked" for this TODO session.
    assert result["phase"] != "blocked"


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
    """Tracked in-progress card with live phase=system_error stays in system_error."""
    # 065 Fix 21: the check_board guard now ALSO requires phase=="system_error"
    # so it can't hijack monitoring_performer mid-retry (when Fix 18 preserves
    # system_error_count across a successful re-dispatch).  The realistic
    # invariant — error setters always pair count>0 with phase=system_error —
    # is preserved here by setting both.
    state = initial_state()
    state["github_service"] = _GitHubInProgress()
    state["current_card"] = {"id": "ITEM_P", "status": "IN_PROGRESS"}
    state["system_error_count"] = 1
    state["phase"] = "system_error"

    result = await check_board(state)

    assert result["phase"] == "system_error"


@pytest.mark.asyncio
async def test_check_board_does_not_hijack_monitoring_mid_retry() -> None:
    """065 Fix 21: count>0 + phase=monitoring_performer must NOT be coerced to system_error.

    Reproduces the retry-loop / container-leak bug: after a successful retry
    re-dispatch, Fix 18 preserves system_error_count=1 while dispatch_performer
    sets phase=monitoring_performer.  The old guard rewrote that back to
    system_error every cycle, so monitor_performer never ran and each retry
    iteration leaked a fresh ephemeral container.
    """
    state = initial_state()
    state["github_service"] = _GitHubInProgress()
    state["current_card"] = {"id": "ITEM_P", "status": "IN_PROGRESS"}
    state["system_error_count"] = 1
    state["phase"] = "monitoring_performer"

    result = await check_board(state)

    assert result["phase"] == "monitoring_performer"


@pytest.mark.asyncio
async def test_check_board_preserves_parked_system_error_session() -> None:
    """069 follow-up: a parked system_error session (waiting on retry interval)
    must be preserved verbatim by check_board so handle_system_error can re-fire
    on the next cycle.  The wedge: handle_system_error's wait branch used to set
    phase=idle, which exited the graph; nothing else routed back to
    handle_system_error, so the 90s retry timer never elapsed against a fresh
    invocation.  Fix is two-sided — handle_system_error keeps phase=system_error
    during wait, and this early-return relays that back through next cycle
    without clobbering current_card or agent_dispatch.
    """
    state = initial_state()
    state["github_service"] = _GitHubInProgress()
    card = {"id": "ITEM_P", "status": "IN_PROGRESS"}
    state["current_card"] = card
    state["system_error_count"] = 1
    state["system_error_reason"] = "transient failure"
    state["phase"] = "system_error"
    state["agent_dispatch"] = {"session_id": "sess-parked"}
    state["agent_dispatch_at"] = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    # 066 FR-010: active_sessions is the source of truth; current_card is a
    # mirror.  Populate the session so _finalize_active_card's rederive doesn't
    # null out the mirror at the end of check_board.
    state["active_sessions"] = {
        "ITEM_P": {
            "current_card": card,
            "phase": "system_error",
            "system_error_count": 1,
            "agent_dispatch": {"session_id": "sess-parked"},
        },
    }
    state["active_card_id"] = "ITEM_P"

    result = await check_board(state)

    assert result["phase"] == "system_error"
    assert result["current_card"] is not None
    assert result["current_card"]["id"] == "ITEM_P"
    assert result["agent_dispatch"] == {"session_id": "sess-parked"}
    assert result["system_error_count"] == 1


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
                ],
            },
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

    # Comment was old → no requeue. Reminder not due, but the session still
    # needs to remain at phase="blocked" so it stays in NON_SLOT_PHASES and
    # the concurrency slot is released. handle_blocked's own dedup gates the
    # comment re-post. (069 follow-up: phase="idle" used to wedge the slot.)
    assert result["phase"] == "blocked"


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
                "PVT_ACTIVE": "Refreshed description\n- [ ] criterion one\n- [ ] criterion two",
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


# ---------------------------------------------------------------------------
# 065 Fix 16 — multi-card un-block feedback reset
# ---------------------------------------------------------------------------


class _GitHubUnblockedNowTodo:
    """Board state: a card previously BLOCKED is now in TODO (operator moved
    it back).  The coordinare still holds an ``active_sessions`` entry for it
    with ``current_card.status='BLOCKED'``.
    """

    async def poll_board(self):
        return {
            "snapshot": {
                "IN_PROGRESS": [],
                "TODO": ["ITEM_UB"],
                "IN_REVIEW": [],
                "BLOCKED": [],
            },
            "titles": {"ITEM_UB": "Was blocked, now unblocked"},
            "descriptions": {"ITEM_UB": ""},
            "issue_numbers": {"ITEM_UB": 70},
            "issue_urls": {},
            "content_node_ids": {},
        }


@pytest.mark.asyncio
async def test_check_board_multicard_unblock_resets_feedback_cycle_count() -> None:
    """065 Fix 16: in multi-card mode, when an operator un-blocks a card
    (moves it from BLOCKED to TODO), the retained session's
    ``feedback_cycle_count`` must reset to 0, monotonic counters
    (``total_feedback_cycles``, ``triage_blocks``) must be preserved,
    the session's ``current_card.status`` must flip to ``TODO`` with
    ``previous_status='BLOCKED'``, and a ``dispatcher.feedback_cycle_reset``
    log record must fire with ``mode='multi'``.
    """
    import structlog.testing

    state = initial_state()
    state["github_service"] = _GitHubUnblockedNowTodo()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=3,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    state["active_sessions"] = {
        "ITEM_UB": {
            "current_card": {
                "id": "ITEM_UB",
                "issue_number": 70,
                "title": "Was blocked, now unblocked",
                "status": "BLOCKED",
            },
            "phase": "blocked",
            "feedback_cycle_count": 4,
            "total_feedback_cycles": 7,
            "triage_blocks": 2,
        },
    }

    with structlog.testing.capture_logs() as cap_logs:
        result = await check_board(state)

    sessions = result.get("active_sessions") or {}
    assert "ITEM_UB" in sessions
    sess = sessions["ITEM_UB"]
    assert sess["feedback_cycle_count"] == 0
    assert sess["total_feedback_cycles"] == 7
    assert sess["triage_blocks"] == 2
    assert sess["current_card"]["status"] == "TODO"
    assert sess["current_card"]["previous_status"] == "BLOCKED"

    reset_events = [
        e for e in cap_logs
        if e.get("event") == "dispatcher.feedback_cycle_reset"
        and e.get("card_id") == "ITEM_UB"
    ]
    assert len(reset_events) == 1, f"Expected one reset log, got: {reset_events}"
    evt = reset_events[0]
    assert evt["prior_count"] == 4
    assert evt["total_feedback_cycles"] == 7
    assert evt["triage_blocks"] == 2
    assert evt["mode"] == "multi"


@pytest.mark.asyncio
async def test_check_board_multicard_fresh_pickup_no_reset_log() -> None:
    """065 Fix 16: a fresh TODO card with no prior session must NOT emit
    the ``dispatcher.feedback_cycle_reset`` log — the reset path is
    strictly for un-blocked retained sessions.
    """
    import structlog.testing

    state = initial_state()
    state["github_service"] = _GitHubInProgressWithTodos()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=3,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    state["active_sessions"] = {}

    with structlog.testing.capture_logs() as cap_logs:
        await check_board(state)

    reset_events = [
        e for e in cap_logs
        if e.get("event") == "dispatcher.feedback_cycle_reset"
    ]
    assert reset_events == [], (
        f"No reset log expected on fresh pickup, got: {reset_events}"
    )


# ---------------------------------------------------------------------------
# 065 retry-counter survival probes
# ---------------------------------------------------------------------------
#
# Goal: pinpoint which counter-reset site fires during the live infinite
# retry loop observed on 2026-05-19, where
# ``handle_system_error.retrying attempt=1`` repeated dozens of times without
# the counter ever advancing past 1.
#
# Each test seeds a "mid-retry" state (count=2, last_at set, notified=False)
# matching the bookkeeping monitor_performer.py:935 writes when it bumps the
# counter on a transport failure. Then it exercises one suspected reset path
# and asserts whether ``system_error_count`` survives.
#
# The IN_PROGRESS recovery + new-card pickup paths zero out count/reason/
# notified but leave ``system_error_last_at`` untouched — which means the
# mid-retry guard in dispatch_performer.py:735 (``last_at is not None AND not
# notified``) silently stops protecting the counter on the very next
# successful dispatch. These tests document the asymmetry.


@pytest.mark.asyncio
async def test_in_progress_readopt_clears_count_but_leaves_last_at_stale() -> None:
    """Candidate #1 — check_board.py:583 (fresh-start IN_PROGRESS recovery).

    When ``current_card`` is None and the board has an IN_PROGRESS card, the
    recovery path zeroes ``system_error_count`` / ``_reason`` / ``_notified``
    but does NOT touch ``system_error_last_at``. The mismatch is what makes
    handle_system_error's retry budget unable to advance: every cycle's bump
    starts from zero, but ``last_at`` keeps reading "we're mid-retry".
    """
    state = initial_state()
    state["github_service"] = _GitHubInProgress()
    state["current_card"] = None  # forces recovery path
    state["system_error_count"] = 2
    state["system_error_reason"] = "Transport failure during status check: TransportError"
    state["system_error_notified"] = False
    state["system_error_last_at"] = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)

    result = await check_board(state)

    assert result["current_card"]["id"] == "ITEM_P"
    assert result["system_error_count"] == 0
    assert result["system_error_reason"] is None
    assert result["system_error_notified"] is False
    # 065 fix: last_at must also be cleared so dispatch_performer's mid_retry
    # guard doesn't misread the fresh re-adopt as "still mid-retry".
    assert result["system_error_last_at"] is None


class _GitHubFreshTodo:
    async def poll_board(self):
        return {
            "snapshot": {"TODO": ["ITEM_NEW"], "IN_PROGRESS": [], "IN_REVIEW": [], "BLOCKED": []},
            "titles": {"ITEM_NEW": "Fresh card"},
            "descriptions": {"ITEM_NEW": ""},
            "issue_numbers": {"ITEM_NEW": 7},
        }


@pytest.mark.asyncio
async def test_new_card_pickup_clears_count_but_leaves_last_at_stale() -> None:
    """Candidate #2 — check_board.py:1061 (single-card new-card pickup).

    Same shape of bug as #1 but on the TODO transition.  When the previous
    card's id differs from the freshly-pulled TODO card the per-card
    counters reset — except ``system_error_last_at``.
    """
    state = initial_state()
    state["github_service"] = _GitHubFreshTodo()
    state["current_card"] = {"id": "ITEM_OLD", "status": "TODO"}
    state["system_error_count"] = 2
    state["system_error_reason"] = "stale"
    state["system_error_notified"] = False
    state["system_error_last_at"] = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)

    result = await check_board(state)

    assert result["current_card"]["id"] == "ITEM_NEW"
    assert result["system_error_count"] == 0
    assert result["system_error_reason"] is None
    assert result["system_error_notified"] is False
    assert result["system_error_last_at"] is None


# ----------------------------------------------------------------------------
# 065 Fix 22: stale-session re-dispatch after daemon restart
# ----------------------------------------------------------------------------


class _GitHubInProgress:
    async def poll_board(self):
        return {
            "snapshot": {
                "IN_PROGRESS": ["ITEM_P"],
                "TODO": [],
                "IN_REVIEW": [],
                "BLOCKED": [],
            },
            "titles": {"ITEM_P": "In Flight"},
            "descriptions": {"ITEM_P": ""},
            "issue_numbers": {"ITEM_P": 42},
            "issue_urls": {},
            "content_node_ids": {},
        }


class _StalePerformerService:
    """Ephemeral HTTP performer that reports no live session after restart."""

    _config = SimpleNamespace(mode="ephemeral")

    def has_live_session(self, session_id: str) -> bool:
        return False


class _LivePerformerService:
    def has_live_session(self, session_id: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_check_board_redispatches_stale_monitoring_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """When phase=monitoring_performer but the performer service has no live
    session (post-restart), check_board rewrites phase to dispatching and
    clears agent_dispatch so dispatch_performer can launch a fresh container.
    """
    monkeypatch.setattr(
        "coordinare.services.docker_executor.DockerExecutor.list_containers_by_label",
        AsyncMock(return_value=[]),
    )
    state = initial_state()
    state["github_service"] = _GitHubInProgress()
    state["current_card"] = {"id": "ITEM_P", "status": "IN_PROGRESS"}
    state["phase"] = "monitoring_performer"
    state["performer_stage"] = "developer"
    state["agent_dispatch"] = {"session_id": "sess-restart"}
    state["agent_dispatch_at"] = datetime(2026, 5, 19, tzinfo=UTC)
    state["performer_services"] = {"developer": _StalePerformerService()}
    state["system_error_count"] = 0

    result = await check_board(state)

    assert result["phase"] == "dispatching"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None
    # retry counter must be untouched — this is a restart, not a real failure
    assert result["system_error_count"] == 0


@pytest.mark.asyncio
async def test_check_board_preserves_live_monitoring_session() -> None:
    """When the service still has a live session, phase and dispatch are kept."""
    state = initial_state()
    state["github_service"] = _GitHubInProgress()
    state["current_card"] = {"id": "ITEM_P", "status": "IN_PROGRESS"}
    state["phase"] = "monitoring_performer"
    state["performer_stage"] = "developer"
    state["agent_dispatch"] = {"session_id": "sess-live"}
    state["performer_services"] = {"developer": _LivePerformerService()}

    result = await check_board(state)

    assert result["phase"] == "monitoring_performer"
    assert result["agent_dispatch"] == {"session_id": "sess-live"}


@pytest.mark.asyncio
async def test_check_board_redispatches_stale_active_session_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale entries in active_sessions are rewritten too, so other sessions
    don't trip the transport error when their per-session turn comes.
    """
    monkeypatch.setattr(
        "coordinare.services.docker_executor.DockerExecutor.list_containers_by_label",
        AsyncMock(return_value=[]),
    )
    state = initial_state()
    state["github_service"] = _GitHubInProgress()
    state["performer_services"] = {"developer": _StalePerformerService()}
    state["active_sessions"] = {
        "ITEM_OTHER": {
            "current_card": {"id": "ITEM_OTHER", "status": "IN_PROGRESS"},
            "phase": "monitoring_performer",
            "performer_stage": "developer",
            "agent_dispatch": {"session_id": "sess-other"},
            "agent_dispatch_at": datetime(2026, 5, 19, tzinfo=UTC),
        },
    }

    await check_board(state)

    sess = state["active_sessions"]["ITEM_OTHER"]
    assert sess["phase"] == "dispatching"
    assert sess["agent_dispatch"] == {}
    assert sess["agent_dispatch_at"] is None


@pytest.mark.asyncio
async def test_check_board_no_stale_redispatch_without_session_id() -> None:
    """A monitoring phase with no session_id isn't considered stale."""
    state = initial_state()
    state["github_service"] = _GitHubInProgress()
    state["current_card"] = {"id": "ITEM_P", "status": "IN_PROGRESS"}
    state["phase"] = "monitoring_performer"
    state["performer_stage"] = "developer"
    state["agent_dispatch"] = {}
    state["performer_services"] = {"developer": _StalePerformerService()}

    result = await check_board(state)

    assert result["phase"] == "monitoring_performer"



# ---------------------------------------------------------------------------
# 066 T008: I3 invariant — check_board must always exit with
# state["current_card"] == active_sessions[active_card_id]["current_card"]
# (or both None).  Exercises a representative spread of branches.
# ---------------------------------------------------------------------------


class _GitHubEmpty:
    async def poll_board(self):
        return {
            "snapshot": {"IN_PROGRESS": [], "TODO": [], "IN_REVIEW": [], "BLOCKED": []},
            "titles": {},
            "descriptions": {},
            "issue_numbers": {},
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "github_factory",
    [_GitHubEmpty, _GitHub, _GitHubInProgressWithTodos],
)
async def test_check_board_preserves_i3_invariant(
    github_factory, assert_current_card_invariant,
) -> None:
    """066 FR-010: after any check_board invocation, current_card MUST be
    the derived mirror of active_sessions[active_card_id]['current_card'],
    or both must be None.
    """
    state = initial_state()
    state["github_service"] = github_factory()

    result = await check_board(state)

    assert_current_card_invariant(result)


# ---------------------------------------------------------------------------
# 066 T010 — un-block feedback reset at N=1 (single-card mode).
# Mirrors the N=3 test above to confirm FR-003: un-block detection fires
# regardless of max_concurrent_cards.
# ---------------------------------------------------------------------------


class _GitHubUnblockedNowTodoN1:
    async def poll_board(self):
        return {
            "snapshot": {
                "IN_PROGRESS": [],
                "TODO": ["ITEM_UB1"],
                "IN_REVIEW": [],
                "BLOCKED": [],
            },
            "titles": {"ITEM_UB1": "Was blocked, now unblocked"},
            "descriptions": {"ITEM_UB1": ""},
            "issue_numbers": {"ITEM_UB1": 71},
            "issue_urls": {},
            "content_node_ids": {},
        }


@pytest.mark.asyncio
async def test_check_board_singlecard_unblock_resets_feedback_cycle_count() -> None:
    """066 FR-003 / T010: at N=1, when the same card is re-picked with
    ``previous_status='BLOCKED'``, ``feedback_cycle_count`` resets to 0,
    monotonic stats are preserved, and a ``dispatcher.feedback_cycle_reset``
    log fires with the documented fields.
    """
    import structlog.testing

    state = initial_state()
    state["github_service"] = _GitHubUnblockedNowTodoN1()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=1,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    # Same card was previously BLOCKED; simulate the same-card re-pickup path.
    state["current_card"] = {
        "id": "ITEM_UB1",
        "issue_number": 71,
        "title": "Was blocked, now unblocked",
        "status": "BLOCKED",
        "previous_status": "BLOCKED",
    }
    state["feedback_cycle_count"] = 3  # type: ignore[typeddict-unknown-key]
    state["total_feedback_cycles"] = 5  # type: ignore[typeddict-unknown-key]
    state["triage_blocks"] = 1  # type: ignore[typeddict-unknown-key]

    with structlog.testing.capture_logs() as cap_logs:
        result = await check_board(state)

    assert result.get("feedback_cycle_count") == 0
    assert result.get("total_feedback_cycles") == 5
    assert result.get("triage_blocks") == 1

    reset_events = [
        e for e in cap_logs
        if e.get("event") == "dispatcher.feedback_cycle_reset"
        and e.get("card_id") == "ITEM_UB1"
    ]
    assert len(reset_events) == 1, f"Expected one reset log, got: {reset_events}"
    evt = reset_events[0]
    assert evt["prior_count"] == 3
    assert evt["total_feedback_cycles"] == 5
    assert evt["triage_blocks"] == 1


# 066 SC-001: centralized allow-list for legitimate `max_cards`/`max_concurrent_cards`
# comparisons in check_board.py.  Each entry is (substring, reason); a compare
# node is allowed iff the source of its *enclosing statement* (via ast.unparse)
# contains the substring.  Using the enclosing statement rather than a fixed
# line window means the check survives reformatting / long boolean expressions
# without losing precision.  Add new entries here (with a clear reason) rather
# than tagging individual lines with comments.
_SC001_ALLOWED: tuple[tuple[str, str], ...] = (
    ("_board_cache", "board-cache optimisation — read-only fast path, no pickup divergence"),
    ("_main_sha_cache", "main-sha cache — avoids redundant ls-remote per session"),
    # ast.unparse normalises strings to single quotes, so match that form.
    ("mode='multi'", "structlog label — diagnostic only, not control flow"),
)


def test_check_board_sc001_no_pickup_time_max_cards_branches() -> None:
    """066 SC-001: zero `max_cards > 1` pickup-time branches in check_board.py.

    The permitted exceptions live in ``_SC001_ALLOWED`` above with documented
    reasons (caches + log labels).  Any other ``max_cards`` /
    ``max_concurrent_cards`` comparison is a pickup-time divergence and
    violates SC-001.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[4] / "src/coordinare/graph/nodes/check_board.py"
    text = src.read_text()
    tree = ast.parse(text)
    text_lines = text.splitlines()

    # Build a child→innermost-enclosing-statement map so each Compare can be
    # scored against the full ``if … : <body>`` (or other stmt) it lives in,
    # not just the line it happens to occupy.  Single pre-order walk tracking
    # the innermost stmt on the stack (O(N) over the AST).
    enclosing_stmt: dict[int, ast.stmt] = {}

    def _walk(node: ast.AST, innermost: ast.stmt | None) -> None:
        next_stmt = node if isinstance(node, ast.stmt) else innermost
        if next_stmt is not None and not isinstance(node, ast.stmt):
            enclosing_stmt[id(node)] = next_stmt
        for child in ast.iter_child_nodes(node):
            _walk(child, next_stmt)

    _walk(tree, None)

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Name) and left.id in {"max_cards", "max_concurrent_cards"}):
            continue
        stmt = enclosing_stmt.get(id(node))
        haystack = ast.unparse(stmt) if stmt is not None else text_lines[node.lineno - 1]
        if any(needle in haystack for needle, _ in _SC001_ALLOWED):
            continue
        violations.append((node.lineno, text_lines[node.lineno - 1].strip()))

    assert not violations, (
        "066 SC-001 violated — pickup-time `max_cards` branches found in "
        "check_board.py.  Either remove the branch or add an entry to "
        "_SC001_ALLOWED in this test with a clear reason:\n"
        + "\n".join(f"  L{ln}: {s}" for ln, s in violations)
    )


# ---------------------------------------------------------------------------
# 066 T009 — parameterised parity: un-block reset and fresh pickup must
# behave identically at N=1 and N=3 (FR-002, FR-008).
# ---------------------------------------------------------------------------


def _config_for(n: int):
    return SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=n,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("max_n", [1, 3])
async def test_unblock_reset_parity_across_n(max_n: int) -> None:
    """066 FR-002/FR-003: un-block reset behaviour is identical at any N."""
    import structlog.testing

    state = initial_state()
    state["github_service"] = _GitHubUnblockedNowTodo()
    state["config"] = _config_for(max_n)
    state["active_sessions"] = {
        "ITEM_UB": {
            "current_card": {
                "id": "ITEM_UB",
                "issue_number": 70,
                "title": "Was blocked, now unblocked",
                "status": "BLOCKED",
            },
            "phase": "blocked",
            "feedback_cycle_count": 4,
            "total_feedback_cycles": 7,
            "triage_blocks": 2,
        },
    }

    with structlog.testing.capture_logs() as cap_logs:
        result = await check_board(state)

    sess = (result.get("active_sessions") or {}).get("ITEM_UB")
    assert sess is not None
    assert sess["feedback_cycle_count"] == 0
    assert sess["total_feedback_cycles"] == 7
    assert sess["triage_blocks"] == 2
    assert sess["current_card"]["status"] == "TODO"
    assert sess["current_card"]["previous_status"] == "BLOCKED"
    assert sess["phase"] == "dispatching"

    reset_events = [
        e for e in cap_logs
        if e.get("event") == "dispatcher.feedback_cycle_reset"
        and e.get("card_id") == "ITEM_UB"
    ]
    assert len(reset_events) == 1
    assert reset_events[0]["prior_count"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("max_n", [1, 3])
async def test_fresh_pickup_no_reset_log_across_n(max_n: int) -> None:
    """066 FR-002: fresh TODO pickup never emits the reset log at any N."""
    import structlog.testing

    state = initial_state()
    state["github_service"] = _GitHubInProgressWithTodos()
    state["config"] = _config_for(max_n)
    state["active_sessions"] = {}

    with structlog.testing.capture_logs() as cap_logs:
        await check_board(state)

    reset_events = [
        e for e in cap_logs
        if e.get("event") == "dispatcher.feedback_cycle_reset"
    ]
    assert reset_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("max_n", [1, 3])
async def test_unified_pickup_log_fires_across_n(max_n: int) -> None:
    """066: `check_board.unified_pickup` fires exactly once per cycle at any N."""
    import structlog.testing

    state = initial_state()
    state["github_service"] = _GitHubInProgressWithTodos()
    state["config"] = _config_for(max_n)

    with structlog.testing.capture_logs() as cap_logs:
        await check_board(state)

    unified = [e for e in cap_logs if e.get("event") == "check_board.unified_pickup"]
    assert len(unified) == 1
    assert unified[0]["max_concurrent_cards"] == max_n


@pytest.mark.asyncio
async def test_check_board_per_session_in_progress_preserves_monitoring_pr() -> None:
    """073 post-US6 regression: when ``assess_card`` short-circuits an
    IN_PROGRESS card with an open PR to ``phase="monitoring_pr"``, the next
    per-session ``check_board`` invocation must NOT clobber that phase to
    ``monitoring_agent``. Doing so caused ``monitor_performer`` (with no
    session_id on a re-adopted card) to reset ``phase="dispatching"``,
    re-triggering ``dispatch_card → notify`` on every cycle and producing
    duplicate "dispatched to <role>" Slack messages every 10 min (the
    dedup window).
    """
    state = initial_state()
    state["github_service"] = _GitHubInProgressWithTodos()
    state["config"] = SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=3,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )
    state["active_sessions"] = {
        "ITEM_P1": {
            "current_card": {
                "id": "ITEM_P1",
                "issue_number": 138,
                "title": "Card with open PR",
                "status": "IN_PROGRESS",
            },
            "phase": "monitoring_pr",
            "pr_url": "https://github.com/acme/repo/pull/139",
        },
    }
    state["current_card"] = {
        "id": "ITEM_P1",
        "issue_number": 138,
        "title": "Card with open PR",
        "status": "IN_PROGRESS",
    }
    state["phase"] = "monitoring_pr"

    result = await check_board(state)

    assert result["phase"] == "monitoring_pr", (
        "IN_PROGRESS per-session invocation with open PR must preserve "
        "monitoring_pr so route_from_board_check goes to monitor_pr "
        "instead of re-dispatching every cycle"
    )
    assert result["current_card"]["id"] == "ITEM_P1"
