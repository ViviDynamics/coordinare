"""Issue #397: a work item is named on the code host without a GitHub issue number.

GitHub Projects keeps today's strings: the branch is ``coordinare/{card id}/{slug}``,
the pull request body says ``Closes #N`` and plan files go under
``docs/cards/{N}-{slug}``. An item from another tracker has no issue number, so
its provider supplies a reference: a token safe in a branch and a path, and the
line its pull request body carries. Its pull request is found again by branch
when the stored link is lost.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from performer.backends._card_docs import card_doc_folder
from performer.github import _pr_body
from performer.main import _doc_folder
from performer.models import Score

from coordinare.graph.nodes.check_board import _build_card_dict, check_board
from coordinare.services.dispatch_guard import canonical_branch_name
from coordinare.services.in_memory_board import InMemoryBoardProvider
from coordinare.services.pr_recovery import find_pr_for_card
from coordinare.services.work_items import safe_reference, work_items_of

GITHUB_BOARD: dict[str, Any] = {
    "snapshot": {"TODO": ["PVTI_1"]},
    "titles": {"PVTI_1": "Add a health endpoint"},
    "descriptions": {"PVTI_1": "We need /health."},
    "issue_numbers": {"PVTI_1": 42},
    "issue_urls": {"PVTI_1": "https://github.com/o/r/issues/42"},
    "content_node_ids": {"PVTI_1": "I_42"},
}


class TestGitHubProjectsIsUnchanged:
    def test_the_card_carries_no_reference_keys(self) -> None:
        card = _build_card_dict("PVTI_1", GITHUB_BOARD, "TODO")

        assert "work_item_ref" not in card
        assert "work_item_pr_line" not in card
        assert (card["issue_id"], card["issue_number"]) == ("I_42", 42)

    def test_the_record_names_the_issue(self) -> None:
        item = work_items_of(GITHUB_BOARD)["PVTI_1"]
        assert (item.reference, item.pr_line) == ("42", "Closes #42")

    def test_the_branch_body_and_docs_path_are_todays(self) -> None:
        card = _build_card_dict("PVTI_1", GITHUB_BOARD, "TODO")
        score = Score(
            title=card["title"], issue_number=42,
            repo_url="https://github.com/o/r.git", branch=canonical_branch_name(card).full_name,
        )

        assert canonical_branch_name(card).full_name == "coordinare/PVTI_1/add-a-health-endpoint"
        assert _pr_body(score).splitlines()[0] == "Closes #42"
        assert _doc_folder(score) == "docs/cards/42-add-a-health-endpoin"
        assert card_doc_folder(score) == "docs/cards/42-add-a-health-endpoin"

    @pytest.mark.asyncio
    async def test_recovery_still_uses_the_closing_reference(self) -> None:
        github = AsyncMock()
        github.find_pr_for_issue.return_value = {"pr_node_id": "PR_1", "pr_url": "u"}
        card = _build_card_dict("PVTI_1", GITHUB_BOARD, "IN_REVIEW")

        assert await find_pr_for_card(github, card, "I_42") == {"pr_node_id": "PR_1", "pr_url": "u"}
        github.find_pr_for_issue.assert_awaited_once_with("I_42")
        github.list_open_pull_requests.assert_not_called()


class TestAnItemWithNoIssueNumber:
    @staticmethod
    async def _current_card(board: InMemoryBoardProvider) -> dict[str, Any]:
        result = await check_board(
            {"board_provider": board, "github_service": AsyncMock(), "advocate_handled_label": "h",
             "advocate_escalation_label": "e", "advocate_history": set()},
        )
        card: dict[str, Any] = result["current_card"]
        return card

    @pytest.mark.asyncio
    async def test_the_card_carries_the_providers_reference(self) -> None:
        board = InMemoryBoardProvider(project_key="PAY")
        board.add_item("Add a health endpoint")

        card = await self._current_card(board)

        assert card["issue_number"] == 0
        assert card["work_item_ref"] == "PAY-1"
        assert card["work_item_pr_line"] == "Work item: PAY-1 (memory://work-items/PAY-1)"

    @pytest.mark.asyncio
    async def test_the_branch_carries_the_key_and_the_body_links_the_item(self) -> None:
        board = InMemoryBoardProvider(project_key="PAY")
        board.add_item("Add a health endpoint")
        card = await self._current_card(board)
        score = Score(
            title=card["title"], issue_number=card["issue_number"],
            repo_url="https://github.com/o/r.git", branch=canonical_branch_name(card).full_name,
            work_item_ref=card["work_item_ref"], work_item_pr_line=card["work_item_pr_line"],
        )

        assert canonical_branch_name(card).full_name == "coordinare/PAY-1/add-a-health-endpoint"
        body = _pr_body(score)
        assert body.splitlines()[0] == "Work item: PAY-1 (memory://work-items/PAY-1)"
        assert "Closes #" not in body
        assert _doc_folder(score) == "docs/cards/PAY-1-add-a-health-endpoin"
        assert card_doc_folder(score) == "docs/cards/PAY-1-add-a-health-endpoin"

    @pytest.mark.asyncio
    async def test_its_pull_request_is_found_again_by_branch(self) -> None:
        github = AsyncMock()
        github.org = "o"
        github.project_name = "r"
        github.list_open_pull_requests.return_value = [
            {"id": "PR_9", "url": "https://github.com/o/r/pull/9", "head_ref": "coordinare/PAY-10/other",
             "head_repo": "o/r"},
            {"id": "PR_7", "url": "https://github.com/o/r/pull/7", "head_ref": "coordinare/PAY-1/add-a-health-endpoint",
             "head_repo": "o/r"},
        ]
        card = {"id": "PAY-1", "issue_id": "PAY-1", "work_item_ref": "PAY-1"}

        found = await find_pr_for_card(github, card, "PAY-1")

        assert found == {"pr_node_id": "PR_7", "pr_url": "https://github.com/o/r/pull/7"}
        github.list_open_pull_requests.assert_awaited_once_with("o", "r")
        github.find_pr_for_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_matching_branch_finds_nothing(self) -> None:
        github = AsyncMock()
        github.org = "o"
        github.project_name = "r"
        github.list_open_pull_requests.return_value = []

        assert await find_pr_for_card(github, {"id": "PAY-1", "work_item_ref": "PAY-1"}, "PAY-1") is None


@pytest.mark.asyncio
async def test_monitor_pr_recovers_a_lost_link_by_branch() -> None:
    """The acceptance case: the stored link is gone and the PR is found again."""
    from coordinare.graph.nodes.monitor_pr import monitor_pr

    github = AsyncMock()
    github.org = "o"
    github.project_name = "r"
    github.list_open_pull_requests.return_value = [
        {"id": "PR_7", "url": "https://github.com/o/r/pull/7", "head_ref": "coordinare/PAY-1/add-a-health-endpoint",
         "head_repo": "o/r"},
    ]
    github.find_pr_for_issue.side_effect = AssertionError("an item with no issue has no closing reference")
    card = {"id": "PAY-1", "issue_id": "PAY-1", "issue_number": 0, "work_item_ref": "PAY-1",
            "title": "Add a health endpoint", "status": "IN_REVIEW"}
    state: dict[str, Any] = {
        "github_service": github, "current_card": card, "active_card_id": "PAY-1",
        "active_sessions": {"PAY-1": {"current_card": card, "phase": "monitoring_pr"}},
        "phase": "monitoring_pr",
    }

    result = await monitor_pr(state)

    recovered = result.get("current_card") or {}
    assert recovered.get("pr_node_id") == "PR_7"
    assert recovered.get("pr_url") == "https://github.com/o/r/pull/7"


def test_a_key_is_made_safe_for_a_branch_and_a_path() -> None:
    assert safe_reference("PAY-1") == "PAY-1"
    assert safe_reference("ops/team #3") == "ops-team-3"


def _github_with(prs: list[dict[str, Any]]) -> AsyncMock:
    github = AsyncMock()
    github.org = "o"
    github.project_name = "r"
    github.list_open_pull_requests.return_value = prs
    return github


@pytest.mark.asyncio
async def test_a_pull_request_from_a_fork_is_not_adopted() -> None:
    github = _github_with([
        {"id": "PR_8", "url": "https://github.com/o/r/pull/8", "head_ref": "coordinare/PAY-1/x", "head_repo": "mallory/r"},
    ])
    assert await find_pr_for_card(github, {"id": "PAY-1", "work_item_ref": "PAY-1"}, "PAY-1") is None


@pytest.mark.asyncio
async def test_two_matching_pull_requests_are_divergence_not_a_choice() -> None:
    github = _github_with([
        {"id": "PR_1", "url": "u1", "head_ref": "coordinare/PAY-1/old-slug", "head_repo": "o/r"},
        {"id": "PR_2", "url": "u2", "head_ref": "coordinare/PAY-1/new-slug", "head_repo": "o/r"},
    ])
    assert await find_pr_for_card(github, {"id": "PAY-1", "work_item_ref": "PAY-1"}, "PAY-1") is None


@pytest.mark.asyncio
async def test_a_restored_card_without_its_reference_is_still_found_by_branch() -> None:
    """A snapshot does not keep the reference; the card's own id still says it has no issue."""
    github = _github_with([{"id": "PR_7", "url": "u7", "head_ref": "coordinare/PAY-1/x", "head_repo": "o/r"}])
    github.find_pr_for_issue.side_effect = AssertionError("no closing reference to look up")

    found = await find_pr_for_card(github, {"id": "PAY-1", "issue_id": "PAY-1", "issue_number": 0}, "PAY-1")

    assert found == {"pr_node_id": "PR_7", "pr_url": "u7"}


@pytest.mark.asyncio
async def test_a_github_draft_card_keeps_the_closing_reference_path() -> None:
    github = _github_with([])
    github.find_pr_for_issue.return_value = None
    await find_pr_for_card(github, {"id": "PVTI_5", "issue_id": "", "issue_number": 0}, "")
    github.find_pr_for_issue.assert_awaited_once_with("")


def test_an_http_item_url_becomes_a_link() -> None:
    board = InMemoryBoardProvider(project_key="PAY", base_url="https://tracker.example/items")
    card = board.add_item("One")
    import asyncio

    (item,) = work_items_of(asyncio.run(board.poll_board())).values()
    assert item.pr_line == f"Work item: [{card}](https://tracker.example/items/{card})"


def test_the_resume_probe_looks_where_the_performer_writes() -> None:
    from coordinare.graph.nodes.check_board import _card_docs_dir

    assert _card_docs_dir({"title": "Add a health endpoint", "work_item_ref": "PAY-1"}) == (
        "docs/cards/PAY-1-add-a-health-endpoin"
    )
    assert _card_docs_dir({"title": "Add a health endpoint", "issue_number": 42}) == "docs/cards/42-add-a-health-endpoin"


def test_a_reference_is_a_valid_ref_segment() -> None:
    assert safe_reference("REL..2") == "REL-2"
    assert safe_reference("x.lock") == "x"


@pytest.mark.asyncio
async def test_a_restored_active_card_regains_its_reference() -> None:
    """A snapshot rebuilds the card without the reference; the next poll puts it back."""
    board = InMemoryBoardProvider(project_key="PAY")
    card_id = board.add_item("Add a health endpoint", status="IN_PROGRESS")
    restored = {"id": card_id, "title": "Add a health endpoint", "status": "IN_PROGRESS"}

    result = await check_board(
        {"board_provider": board, "github_service": AsyncMock(), "current_card": restored,
         "active_card_id": card_id, "active_sessions": {card_id: {"current_card": restored, "phase": "monitoring_performer"}},
         "phase": "monitoring_performer", "advocate_handled_label": "h", "advocate_escalation_label": "e",
         "advocate_history": set()},
    )

    assert result["current_card"]["work_item_ref"] == card_id
