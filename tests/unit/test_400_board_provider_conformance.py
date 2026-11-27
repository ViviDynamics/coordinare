"""Issue #400 — one conformance suite that every work item provider must pass.

Spec 149 showed a stub can drive coordinare. That proves the seam resolves, not
that a provider *works*: "implements the protocol" says nothing about how a
listing treats a state the tracker has and coordinare does not, whether a comment
cursor is honoured, or what a person moving a card mid-run does to coordinare.

Every test here takes a ``harness`` and runs once per provider in ``PROVIDERS``.
Adding a provider means writing its harness and adding it to that list; no test
below is rewritten. A behaviour a provider cannot have is declared on its harness
(GitHub Projects cannot refuse a move), and that case runs only for the providers
that can. No test special-cases a provider.

The GitHub Projects run goes through the bench's ``FakeGitHubService`` and the
real ``GitHubProjectsBoardProvider`` adapter, so the code under test is the code
production runs. The in-memory provider has string ids, string comment cursors and
no issue numbers, which is what a second tracker looks like.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.services.board_provider import (
    BoardProvider,
    GitHubProjectsBoardProvider,
    MoveOutcome,
    move_card_or_warn,
)
from coordinare.services.fake_github import FakeGitHubService
from coordinare.services.in_memory_board import InMemoryBoardProvider
from coordinare.state_store import WorkflowSnapshot

if TYPE_CHECKING:
    from pathlib import Path

#: The canonical states a listing may use. Anything else is a native name leaking.
CANONICAL = {"BACKLOG", "TODO", "BLOCKED", "IN_PROGRESS", "IN_REVIEW", "DONE"}


class RecordingBoard:
    """Wraps a provider and records every move coordinare asks it for.

    How the suite sees that coordinare did *not* write: the provider is the only
    way coordinare can move a card, so an empty ``moves`` list is proof.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.moves: list[tuple[str, str]] = []

    @property
    def github(self) -> Any:
        """Forwarded, so a wrapped GitHub adapter still reads as one to the daemon."""
        return getattr(self._inner, "github", None)

    async def poll_board(self) -> dict[str, Any]:
        board: dict[str, Any] = await self._inner.poll_board()
        return board

    async def move_card(self, card_id: str, status: str) -> MoveOutcome:
        self.moves.append((card_id, status))
        outcome: MoveOutcome = await self._inner.move_card(card_id, status)
        return outcome

    async def get_card(self, card_id: str) -> dict[str, Any]:
        card: dict[str, Any] = await self._inner.get_card(card_id)
        return card

    async def add_card_comment(self, card_id: str, body: str) -> dict[str, Any]:
        posted: dict[str, Any] = await self._inner.add_card_comment(card_id, body)
        return posted

    async def get_card_comments(self, card_id: str, since_id: Any = None) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = await self._inner.get_card_comments(card_id, since_id=since_id)
        return comments


# --- harnesses: how the suite reaches behind each provider ------------------------


class GitHubProjectsHarness:
    """GitHub Projects: the production adapter over the bench's fake service."""

    name = "github_projects"
    #: GitHub Projects has no workflow rules, so it never refuses a move.
    can_refuse_moves = False

    def __init__(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare.git"
        bare.mkdir()
        self.fake = FakeGitHubService(bare_repo_path=bare, work_dir=tmp_path / "ci")
        self.provider = GitHubProjectsBoardProvider(self.fake)
        self._seq = 0

    def seed(self, title: str, status: str = "TODO", body: str = "") -> str:
        self._seq += 1
        item_id = f"PVTI_conf{self._seq}"
        # Issue numbers well clear of the fake's PR numbers, as on real GitHub where
        # issues and pull requests share one sequence.
        self.fake.seed_card(item_id, title=title, body=body, status=status, issue_number=9000 + self._seq)
        return item_id

    def person_moves(self, card_id: str, status: str) -> None:
        self.fake._cards[card_id]["status"] = status

    def park_in_unmapped_state(self, card_id: str) -> None:
        # A column a person added to the project that coordinare has no name for.
        self.fake._cards[card_id]["status"] = "Icebox"

    def person_comments(self, card_id: str, body: str) -> None:
        number = self.fake._cards[card_id]["issue_number"]
        self.fake.add_person_comment(number, author="octo-person", body=body)

    def bodies_coordinare_posted(self, card_id: str) -> list[str]:
        # GitHub comments on the card's issue, so the write must name the issue.
        issue = self.fake._cards[card_id]["content_node_id"]
        return [e["body"] for e in self.fake.events if e["kind"] == "add_comment" and e["subject_id"] == issue]

    def code_host(self) -> Any:
        return self.fake


class InMemoryHarness:
    """A tracker with nothing GitHub-shaped: string keys, its own state names."""

    name = "in_memory"
    can_refuse_moves = True

    def __init__(self, tmp_path: Path) -> None:
        self.provider = InMemoryBoardProvider(project_key="PAY")

    def seed(self, title: str, status: str = "TODO", body: str = "") -> str:
        return self.provider.add_item(title, body=body, status=status)

    def person_moves(self, card_id: str, status: str) -> None:
        self.provider.person_moves(card_id, status)

    def park_in_unmapped_state(self, card_id: str) -> None:
        self.provider.person_moves_to_native(card_id, "Icebox")

    def person_comments(self, card_id: str, body: str) -> None:
        self.provider.person_comments(card_id, author="pat", body=body)

    def bodies_coordinare_posted(self, card_id: str) -> list[str]:
        return [c["body"] for c in self.provider.item(card_id).comments if c["author"] == "coordinare"]

    def refuse(self, card_id: str, status: str) -> None:
        self.provider.forbid(from_status=self.provider.status_of(card_id), to_status=status)

    def code_host(self) -> Any:
        # The code host is still GitHub, but this board is not a GitHub project, so
        # the service has no project id. Restart reconcile must not need one.
        github = MagicMock()
        github.project_id = None
        return github


#: Add a provider here. Nothing else in this file changes.
PROVIDERS = [GitHubProjectsHarness, InMemoryHarness]


@pytest.fixture(params=PROVIDERS, ids=lambda h: h.name)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    return request.param(tmp_path)


@pytest.fixture(params=[h for h in PROVIDERS if h.can_refuse_moves], ids=lambda h: h.name)
def refusing_harness(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    """Providers whose tracker can refuse a move. Declared on the harness, not tested for."""
    return request.param(tmp_path)


def _column_of(board: dict[str, Any], card_id: str) -> list[str]:
    return [status for status, ids in board["snapshot"].items() if card_id in ids]


# --- the protocol ------------------------------------------------------------------


def test_it_satisfies_the_protocol(harness: Any) -> None:
    assert isinstance(harness.provider, BoardProvider)


# --- listing by canonical state ------------------------------------------------------


class TestListing:
    @pytest.mark.asyncio
    async def test_each_item_is_listed_once_under_its_canonical_state(self, harness: Any) -> None:
        todo = harness.seed("Add a health endpoint", "TODO")
        review = harness.seed("Fix the login redirect", "IN_REVIEW")

        board = await harness.provider.poll_board()

        assert _column_of(board, todo) == ["TODO"]
        assert _column_of(board, review) == ["IN_REVIEW"]

    @pytest.mark.asyncio
    async def test_the_listing_speaks_only_canonical_states(self, harness: Any) -> None:
        harness.seed("One", "TODO")
        harness.seed("Two", "DONE")

        board = await harness.provider.poll_board()

        assert set(board["snapshot"]) <= CANONICAL, (
            f"native state names leaked past the provider: {set(board['snapshot']) - CANONICAL}"
        )

    @pytest.mark.asyncio
    async def test_an_item_in_an_unmapped_native_state_is_not_listed(self, harness: Any) -> None:
        """A state coordinare has no name for is not work coordinare may pick up."""
        parked = harness.seed("Someday", "TODO")
        listed = harness.seed("Today", "TODO")
        harness.park_in_unmapped_state(parked)

        board = await harness.provider.poll_board()

        assert _column_of(board, parked) == []
        assert _column_of(board, listed) == ["TODO"]
        assert set(board["snapshot"]) <= CANONICAL

    @pytest.mark.asyncio
    async def test_a_listed_id_reads_back_through_get_card(self, harness: Any) -> None:
        card = harness.seed("Add a health endpoint", "TODO", body="We need /health.")
        await harness.provider.poll_board()

        details = await harness.provider.get_card(card)

        assert details.get("title") == "Add a health endpoint"
        assert details.get("body") == "We need /health."

    @pytest.mark.asyncio
    async def test_ids_are_strings(self, harness: Any) -> None:
        harness.seed("One", "TODO")
        board = await harness.provider.poll_board()
        for ids in board["snapshot"].values():
            assert all(isinstance(i, str) and i for i in ids)


# --- moves -----------------------------------------------------------------------------


class TestMoves:
    @pytest.mark.asyncio
    async def test_a_move_is_reported_and_listed(self, harness: Any) -> None:
        card = harness.seed("One", "TODO")
        await harness.provider.poll_board()

        outcome = await harness.provider.move_card(card, "IN_PROGRESS")

        assert outcome.moved
        assert _column_of(await harness.provider.poll_board(), card) == ["IN_PROGRESS"]

    @pytest.mark.asyncio
    async def test_a_repeated_move_is_a_no_op(self, harness: Any) -> None:
        card = harness.seed("One", "TODO")
        await harness.provider.poll_board()

        first = await harness.provider.move_card(card, "IN_PROGRESS")
        second = await harness.provider.move_card(card, "IN_PROGRESS")

        assert first.moved and second.moved
        assert _column_of(await harness.provider.poll_board(), card) == ["IN_PROGRESS"]

    @pytest.mark.asyncio
    async def test_a_move_the_tracker_refuses_is_an_outcome_and_the_item_stays(
        self, refusing_harness: Any,
    ) -> None:
        harness = refusing_harness
        card = harness.seed("One", "IN_REVIEW")
        await harness.provider.poll_board()
        harness.refuse(card, "DONE")

        outcome = await harness.provider.move_card(card, "DONE")

        assert not outcome.moved
        assert outcome.reason
        assert _column_of(await harness.provider.poll_board(), card) == ["IN_REVIEW"]
        # And coordinare's helper reports it rather than carrying on as if it moved.
        assert await move_card_or_warn(harness.provider, card, "DONE") is False


# --- a person moves a card while coordinare works on it ---------------------------------


def _daemon(board: Any, code_host: Any) -> CoordinareDaemon:
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})

    async def _no_sleep(_: float) -> None:
        return None

    daemon = CoordinareDaemon(graph, poll_interval_seconds=1, max_cycles=1, sleep_func=_no_sleep)
    daemon._state["board_provider"] = board
    daemon._state["github_service"] = code_host
    return daemon


def _snapshot(active_card_id: str | None = None, phase: str = "idle") -> WorkflowSnapshot:
    return WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase=phase, active_card_id=active_card_id)


def _session(card_id: str, phase: str, **extra: Any) -> dict[str, Any]:
    return {"current_card": {"id": card_id}, "performer_stage": "implementing", "phase": phase, **extra}


class TestAPersonsMove:
    @pytest.mark.asyncio
    async def test_the_next_listing_shows_it(self, harness: Any) -> None:
        card = harness.seed("One", "TODO")
        await harness.provider.move_card(card, "IN_PROGRESS")

        harness.person_moves(card, "BLOCKED")

        assert _column_of(await harness.provider.poll_board(), card) == ["BLOCKED"]

    @pytest.mark.asyncio
    async def test_coordinare_does_not_undo_it(self, harness: Any) -> None:
        """A person pulled a running card back to To Do. Coordinare pauses; it never
        writes the card back to In Progress."""
        card = harness.seed("One", "TODO")
        await harness.provider.poll_board()
        await harness.provider.move_card(card, "IN_PROGRESS")
        harness.person_moves(card, "TODO")

        board = RecordingBoard(harness.provider)
        daemon = _daemon(board, harness.code_host())
        daemon._state["active_sessions"] = {
            card: _session(card, "monitoring_performer", agent_dispatch={"session_id": "s-1"}),
        }

        await daemon._reconcile_with_board(_snapshot())

        assert board.moves == [], f"coordinare wrote the card back: {board.moves}"
        assert daemon._state["active_sessions"][card]["board_paused"] is True
        assert _column_of(await harness.provider.poll_board(), card) == ["TODO"]


# --- comments ------------------------------------------------------------------------------


class TestComments:
    @pytest.mark.asyncio
    async def test_a_comment_posted_by_listed_id_lands_on_the_item(self, harness: Any) -> None:
        card = harness.seed("One", "TODO")
        await harness.provider.poll_board()

        await harness.provider.add_card_comment(card, "coordinare picked this up")

        assert harness.bodies_coordinare_posted(card) == ["coordinare picked this up"]

    @pytest.mark.asyncio
    async def test_comments_come_back_oldest_first_in_the_common_shape(self, harness: Any) -> None:
        card = harness.seed("One", "TODO")
        await harness.provider.poll_board()
        harness.person_comments(card, "please also cover the 404 case")
        harness.person_comments(card, "and the 410 case")

        comments = await harness.provider.get_card_comments(card)

        assert [c["body"] for c in comments] == ["please also cover the 404 case", "and the 410 case"]
        for c in comments:
            assert {"id", "author", "body", "created_at"} <= set(c)

    @pytest.mark.asyncio
    async def test_only_comments_after_the_cursor_are_returned(self, harness: Any) -> None:
        card = harness.seed("One", "TODO")
        await harness.provider.poll_board()
        harness.person_comments(card, "first")
        harness.person_comments(card, "second")
        harness.person_comments(card, "third")

        everything = await harness.provider.get_card_comments(card)
        cursor = everything[0]["id"]  # opaque: passed back exactly as the provider issued it

        after = await harness.provider.get_card_comments(card, since_id=cursor)
        assert [c["body"] for c in after] == ["second", "third"]

        last = everything[-1]["id"]
        assert await harness.provider.get_card_comments(card, since_id=last) == []

    @pytest.mark.asyncio
    async def test_comment_ids_are_ints_or_non_empty_strings(self, harness: Any) -> None:
        """398: the only ids coordinare can keep and hand back as a cursor."""
        card = harness.seed("One", "TODO")
        await harness.provider.poll_board()
        harness.person_comments(card, "a question")
        await harness.provider.add_card_comment(card, "an answer")

        for c in await harness.provider.get_card_comments(card):
            cid = c["id"]
            assert (isinstance(cid, int) and not isinstance(cid, bool)) or (isinstance(cid, str) and cid)

    @pytest.mark.asyncio
    async def test_coordinare_routes_comments_by_the_providers_own_cursor(self, harness: Any) -> None:
        """398: the cursor coordinare keeps is the id the provider issued, untouched."""
        from coordinare.graph.nodes.route_issue_comments import route_issue_comments
        from coordinare.graph.state import initial_state

        card = harness.seed("One", "TODO")
        await harness.provider.poll_board()
        harness.person_comments(card, "what about the 404 case?")
        harness.person_comments(card, "and the 410 case?")
        issued = [c["id"] for c in await harness.provider.get_card_comments(card)]
        state = initial_state()
        state["board_provider"] = harness.provider
        state["current_card"] = {"id": card}

        await route_issue_comments(state)

        assert state["last_issue_comment_id"] == issued[-1]
        assert state["processed_issue_comment_ids"] == set(issued)
        routed = len(state["card_clarifications"])
        await route_issue_comments(state)
        assert len(state["card_clarifications"]) == routed, "a comment was routed twice"

    @pytest.mark.asyncio
    async def test_an_unknown_card_has_no_comments_rather_than_an_error(self, harness: Any) -> None:
        harness.seed("One", "TODO")
        await harness.provider.poll_board()
        assert await harness.provider.get_card_comments("NOPE-404") == []


# --- restart reconcile ------------------------------------------------------------------------


class TestRestartReconcile:
    @pytest.mark.asyncio
    async def test_the_board_wins_over_a_restored_session(self, harness: Any) -> None:
        """A card restored as blocked that a person reopened is free to pick up again."""
        card = harness.seed("One", "TODO")
        daemon = _daemon(RecordingBoard(harness.provider), harness.code_host())
        daemon._state["active_sessions"] = {card: _session(card, "blocked")}

        await daemon._reconcile_with_board(_snapshot())

        assert daemon._state["active_sessions"][card]["phase"] == "idle"

    @pytest.mark.asyncio
    async def test_a_session_for_a_finished_item_is_retired(self, harness: Any) -> None:
        card = harness.seed("One", "DONE")
        daemon = _daemon(RecordingBoard(harness.provider), harness.code_host())
        daemon._state["active_sessions"] = {card: _session(card, "monitoring_pr")}

        await daemon._reconcile_with_board(_snapshot())

        assert card not in daemon._state["active_sessions"]

    @pytest.mark.asyncio
    async def test_reconcile_writes_nothing_to_the_board(self, harness: Any) -> None:
        card = harness.seed("One", "IN_REVIEW")
        board = RecordingBoard(harness.provider)
        daemon = _daemon(board, harness.code_host())
        daemon._state["active_sessions"] = {card: _session(card, "blocked")}

        await daemon._reconcile_with_board(_snapshot())

        assert board.moves == []


# --- what makes the in-memory provider a fair second provider -----------------------------------


class TestTheInMemoryProviderHasNothingGitHubShaped:
    @pytest.mark.asyncio
    async def test_coordinare_reads_its_comments_today(self) -> None:
        """Its ids are strings coordinare's integer cursor can still hold until #398."""
        from coordinare.services.issue_comment_service import fetch_new_issue_comments

        provider = InMemoryBoardProvider(project_key="PAY")
        card = provider.add_item("One")
        provider.person_comments(card, author="pat", body="first")
        provider.person_comments(card, author="pat", body="second")

        events = await fetch_new_issue_comments(card, None, provider)
        assert [e.body for e in events] == ["first", "second"]
        later = await fetch_new_issue_comments(card, events[0].comment_id, provider)
        assert [e.body for e in later] == ["second"]

    @pytest.mark.asyncio
    async def test_no_issue_numbers_or_node_ids_in_its_listing(self) -> None:
        provider = InMemoryBoardProvider(project_key="PAY")
        card = provider.add_item("One")
        board = await provider.poll_board()

        assert card == "PAY-1"
        for github_only in ("issue_numbers", "content_node_ids", "issue_urls", "native_blockers"):
            assert github_only not in board

    @pytest.mark.asyncio
    async def test_its_comment_cursors_are_strings(self) -> None:
        provider = InMemoryBoardProvider(project_key="PAY")
        card = provider.add_item("One")
        provider.person_comments(card, author="pat", body="hi")
        (comment,) = await provider.get_card_comments(card)
        assert isinstance(comment["id"], str)

    @pytest.mark.asyncio
    async def test_its_comment_ids_are_not_numbers(self) -> None:
        """398: nothing that reads an id as a number, or sorts ids, can pass with it."""
        provider = InMemoryBoardProvider(project_key="PAY")
        card = provider.add_item("One")
        provider.person_comments(card, author="pat", body="hi")
        (comment,) = await provider.get_card_comments(card)
        assert not comment["id"].isdigit()
