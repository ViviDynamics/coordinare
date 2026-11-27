"""Issue #396, second slice: the last work item writes, and blocker state from the listing.

The assessor's "not work" verdict, the QA evidence comment and the state of an
off-board blocker each went to GitHub directly. Each now goes through the work
item provider; on GitHub Projects the adapter makes the same GitHub call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from coordinare.services.board_provider import (
    NOT_WORK,
    BoardProvider,
    GitHubProjectsBoardProvider,
    MoveOutcome,
)
from coordinare.services.card_identity import CardIdentityMap
from tests.unit.test_400_board_provider_conformance import PROVIDERS

if TYPE_CHECKING:
    from pathlib import Path


class _GitHub(CardIdentityMap):
    """Records the GitHub calls the adapter makes, as the real service would receive them."""

    def __init__(self, fail_close: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._fail_close = fail_close
        self._remember_content_ids({"PVTI_1": "I_node1"})

    async def close_issue(self, issue_id: str) -> dict[str, Any]:
        self.calls.append(("close_issue", issue_id))
        if self._fail_close:
            raise RuntimeError("graphql down")
        return {"closed": True}

    async def move_card(self, item_id: str, status: str) -> None:
        self.calls.append(("move_card", item_id, status))

    async def add_comment(self, subject_id: str, body: str) -> dict[str, Any]:
        self.calls.append(("add_comment", subject_id, body))
        return {}


# --- "not work" on the seam ----------------------------------------------------------------------


class TestNotWork:
    def test_it_is_a_move_target_and_not_a_sixth_operation(self) -> None:
        assert NOT_WORK == "NOT_WORK"
        assert not hasattr(BoardProvider, "close_card")
        assert not hasattr(BoardProvider, "mark_not_work")

    @pytest.mark.asyncio
    async def test_github_projects_closes_the_issue_and_moves_the_item_to_done(self) -> None:
        github = _GitHub()
        outcome = await GitHubProjectsBoardProvider(github).move_card("PVTI_1", NOT_WORK)

        assert outcome == MoveOutcome.ok()
        assert github.calls == [("close_issue", "I_node1"), ("move_card", "PVTI_1", "DONE")]

    @pytest.mark.asyncio
    async def test_a_failed_close_still_moves_the_item_out_of_todo(self) -> None:
        """As before: the next poll would admit a card left in To Do again."""
        github = _GitHub(fail_close=True)
        await GitHubProjectsBoardProvider(github).move_card("PVTI_1", NOT_WORK)

        assert github.calls == [("close_issue", "I_node1"), ("move_card", "PVTI_1", "DONE")]

    @pytest.mark.asyncio
    async def test_any_other_move_is_forwarded_unchanged(self) -> None:
        github = _GitHub()
        await GitHubProjectsBoardProvider(github).move_card("PVTI_1", "BACKLOG")

        assert github.calls == [("move_card", "PVTI_1", "BACKLOG")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("harness_type", PROVIDERS, ids=lambda h: h.name)
    async def test_every_provider_lists_a_not_work_item_as_done(self, harness_type: Any, tmp_path: Path) -> None:
        harness = harness_type(tmp_path)
        card = harness.seed("Not a real problem", "TODO")
        await harness.provider.poll_board()

        outcome = await harness.provider.move_card(card, NOT_WORK)
        board = await harness.provider.poll_board()

        assert outcome.moved
        assert card in board["snapshot"].get("DONE", [])
        assert card not in board["snapshot"].get("TODO", [])


# --- the assessor's decline -----------------------------------------------------------------------


class _Board:
    """A provider that is not GitHub: every write it receives is recorded here."""

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str]] = []

    async def poll_board(self) -> dict[str, Any]:
        return {"snapshot": {}}

    async def move_card(self, card_id: str, status: str) -> MoveOutcome:
        self.moves.append((card_id, status))
        return MoveOutcome.ok()

    async def get_card(self, card_id: str) -> dict[str, Any]:
        return {}

    async def add_card_comment(self, card_id: str, body: str) -> dict[str, Any]:
        self.comments.append((card_id, body))
        return {}

    async def get_card_comments(self, card_id: str, since_id: Any = None) -> list[dict[str, Any]]:
        return []


def _decline_state() -> dict[str, Any]:
    return {
        "config": None, "active_card_id": "PAY-7", "current_card": "PAY-7",
        "active_sessions": {"PAY-7": {"performer_stage": "assessing"}},
        "lifecycle_sequence": ["implementing", "assessing"],
    }


_REPORT = {"report": {"assessment": {"expected_behavior": "the premise is wrong"}}}


class TestTheDecline:
    @pytest.mark.asyncio
    async def test_not_work_reaches_the_configured_board_and_not_github(self) -> None:
        from unittest.mock import AsyncMock

        from coordinare.graph.nodes.monitor_performer import _apply_assessor_decline

        board, github = _Board(), AsyncMock()
        card = {"id": "PAY-7", "issue_id": "PAY-7"}
        out = await _apply_assessor_decline(
            _decline_state(), card, "PAY-7", "assessment_not_work", _REPORT, board, github,
        )

        assert [c for c, _ in board.comments] == ["PAY-7"]
        assert "**Assessor verdict: not work**" in board.comments[0][1]
        assert "the premise is wrong" in board.comments[0][1]
        assert board.moves == [("PAY-7", NOT_WORK)]
        github.add_comment.assert_not_called()
        github.close_issue.assert_not_called()
        assert out["phase"] == "idle"

    @pytest.mark.asyncio
    async def test_needs_split_reaches_the_configured_board_and_not_github(self) -> None:
        from unittest.mock import AsyncMock

        from coordinare.graph.nodes.monitor_performer import _apply_assessor_decline

        board, github = _Board(), AsyncMock()
        await _apply_assessor_decline(
            _decline_state(), {"id": "PAY-7"}, "PAY-7", "assessment_needs_split", _REPORT, board, github,
        )

        assert "**Assessor verdict: needs split**" in board.comments[0][1]
        assert board.moves == [("PAY-7", "BACKLOG")]
        github.add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_github_projects_receives_the_same_comment_text_as_before(self) -> None:
        from coordinare.graph.nodes.monitor_performer import _apply_assessor_decline

        github = _GitHub()
        await _apply_assessor_decline(
            _decline_state(), {"id": "PVTI_1", "issue_id": "I_node1"}, "PVTI_1", "assessment_not_work",
            _REPORT, GitHubProjectsBoardProvider(github), github,
        )

        from coordinare.graph.attribution import coordinare_attribution

        header = coordinare_attribution(None, None)
        expected = (
            f"{header}\n\n**Assessor verdict: not work**\n\nthe premise is wrong\n\n"
            "Closing this issue without building it. Correct the premise and reopen, or split the work."
        )
        assert github.calls == [
            ("add_comment", "I_node1", expected),
            ("close_issue", "I_node1"),
            ("move_card", "PVTI_1", "DONE"),
        ]


# --- the QA evidence comment ----------------------------------------------------------------------


def _qa_state(response: dict[str, Any], github: Any = None) -> dict[str, Any]:
    from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer

    return _make_state(
        service=_Performer(response), stage="qa", sequence=["implementing", "qa", "documenting"],
        card={"id": "PAY-7", "issue_id": "PAY-7", "status": "IN_REVIEW", "pr_url": "https://github.com/o/r/pull/3"},
        github=github,
    )


class TestTheQaEvidenceComment:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["qa_passed", "qa_failed", "qa_env_blocked", "blocked"])
    async def test_coordinare_posts_it_on_the_configured_board(self, status: str) -> None:
        from unittest.mock import AsyncMock

        from coordinare.graph.nodes.monitor_performer import monitor_performer

        github, board = AsyncMock(), _Board()
        state = _qa_state(
            {"status": status, "session_id": "s1", "work_item_comment": "## QA Evidence\n\nPASSED",
             "report": {"criteria_checked": 1}, "questions": ["QA: 1 failure"]},
            github,
        )
        state["board_provider"] = board

        await monitor_performer(state)

        assert board.comments == [("PAY-7", "## QA Evidence\n\nPASSED")]
        github.add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_status_without_the_comment_posts_nothing(self) -> None:
        """An older performer posted it itself, so coordinare must not post it again."""
        from unittest.mock import AsyncMock

        from coordinare.graph.nodes.monitor_performer import monitor_performer

        board = _Board()
        state = _qa_state({"status": "qa_passed", "session_id": "s1", "report": {"criteria_checked": 1}}, AsyncMock())
        state["board_provider"] = board

        await monitor_performer(state)

        assert board.comments == []

    @pytest.mark.asyncio
    async def test_github_projects_puts_it_on_the_issue(self) -> None:
        from coordinare.graph.nodes.monitor_performer import monitor_performer

        github = _GitHub()
        state = _qa_state({"status": "qa_passed", "session_id": "s1", "work_item_comment": "## QA Evidence"}, github)
        state["current_card"] = {**state["current_card"], "id": "PVTI_1", "issue_id": "I_node1"}
        state["board_provider"] = GitHubProjectsBoardProvider(github)

        await monitor_performer(state)

        assert ("add_comment", "I_node1", "## QA Evidence") in github.calls
        assert [c for c in github.calls if c[0] == "add_comment"] == [("add_comment", "I_node1", "## QA Evidence")]

    @pytest.mark.asyncio
    async def test_a_board_failure_does_not_stop_the_verdict(self) -> None:
        from unittest.mock import AsyncMock

        from coordinare.graph.nodes.monitor_performer import monitor_performer

        board = _Board()
        board.add_card_comment = AsyncMock(side_effect=RuntimeError("tracker down"))  # type: ignore[method-assign]
        state = _qa_state({"status": "qa_failed", "session_id": "s1", "work_item_comment": "## QA Evidence",
                           "failures": [{"criterion": "c", "expected": "e", "actual": "a"}]}, AsyncMock())
        state["board_provider"] = board

        result = await monitor_performer(state)

        assert result["phase"] != "system_error"


def test_the_dispatch_payload_says_coordinare_posts_work_item_comments() -> None:
    from coordinare.graph.nodes.dispatch_performer import _base_card_context

    context, _ = _base_card_context({}, {"id": "PAY-7"}, "PAY-7", "qa")

    assert context["work_item_comments_by_coordinare"] is True
