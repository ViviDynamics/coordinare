"""Spec 153 / issue #232 — one card id model.

Spec 149 put four board operations behind a protocol and left reading a card's
comments on the code host, because coordinare addresses that read by GitHub issue
number while every other card operation uses a node id. A method promising a card
id would have been false.

This closes it. The translation now happens inside the GitHub provider, from a
pairing ``poll_board`` already fetches, so it costs no extra API call — and these
tests assert that cost, not just the correctness, because a working seam that
doubled the calls in the hot loop would be traded away.
"""

from __future__ import annotations

import inspect

import pytest

from coordinare.services.board_provider import BoardProvider
from coordinare.services.card_identity import CardIdentityMap
from coordinare.services.github import GitHubService

#: The surface after this spec. Spec 149's file pins the same set; both must agree.
EXPECTED_SURFACE = {
    "poll_board",
    "move_card",
    "get_card",
    "add_card_comment",
    "get_card_comments",
}


class RecordingGitHub(CardIdentityMap):
    """A fake that records EVERY call by name, in order.

    Not ``AsyncMock``: it auto-creates attributes, so a mistyped assertion passes
    and a call coordinare never made can still be "verified". The whole point of
    the cost assertions below is to notice a call nobody predicted, which a mock
    that invents attributes on demand cannot do.
    """

    def __init__(
        self,
        *,
        issue_numbers: dict[str, int] | None = None,
        details: dict[str, dict] | None = None,
        comments: list[dict] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._issue_numbers = issue_numbers if issue_numbers is not None else {}
        self._details = details or {}
        self._comments = comments if comments is not None else []

    # The translation under test is production code, inherited rather than
    # reimplemented. A fake that reimplemented it would pass while the real
    # service was broken -- which is the whole failure this file exists to catch.

    async def poll_board(self) -> dict:
        self.calls.append("poll_board")
        self._remember_issue_numbers(dict(self._issue_numbers))
        return {
            "snapshot": {"TODO": [], "IN_PROGRESS": [], "IN_REVIEW": [], "BLOCKED": [], "DONE": []},
            "titles": {},
            "descriptions": {},
            "issue_numbers": dict(self._issue_numbers),
            "item_labels": {},
        }

    async def get_issue_details(self, card_id: str) -> dict:
        self.calls.append("get_issue_details")
        return self._details.get(card_id, {})

    async def get_issue_comments(
        self, issue_number: int, since_id: int | None = None,
    ) -> list[dict]:
        self.calls.append("get_issue_comments")
        self.last_issue_number = issue_number
        self.last_since_id = since_id
        return [c for c in self._comments if since_id is None or int(c["id"]) > since_id]


COMMENT = {"id": 5, "author": "jason", "body": "try the other approach", "created_at": "2026-08-30"}


class StubBoard:
    """A board with no GitHub in it. Cards are keyed the way Jira keys them."""

    def __init__(self, comments: list[dict] | None = None) -> None:
        self.calls: list[str] = []
        self._comments = comments if comments is not None else [COMMENT]

    async def poll_board(self) -> dict:
        self.calls.append("poll_board")
        return {"snapshot": {}, "titles": {}}

    async def move_card(self, card_id: str, status: str):
        from coordinare.services.board_provider import MoveOutcome

        self.calls.append("move_card")
        return MoveOutcome.ok()

    async def get_card(self, card_id: str) -> dict:
        self.calls.append("get_card")
        return {}

    async def add_card_comment(self, card_id: str, body: str) -> dict:
        self.calls.append("add_card_comment")
        return {}

    async def get_card_comments(self, card_id: str, since_id: int | None = None) -> list[dict]:
        self.calls.append(f"get_card_comments:{card_id}")
        return list(self._comments)


class TestTheProtocolGainedExactlyOneOperation:
    def test_the_surface_is_the_five_board_operations(self) -> None:
        """SC-004 — one operation added, deliberately, and the surface stays pinned."""
        actual = {m for m in dir(BoardProvider) if not m.startswith("_")}
        assert actual == EXPECTED_SURFACE, (
            f"protocol surface is {sorted(actual)}. Every operation here must be implemented "
            "by every future provider, so it grows only when a call site needs it."
        )

    def test_no_operation_takes_a_github_shaped_argument(self) -> None:
        """FR-002 — an issue number in a signature would rebuild the two-id model."""
        for name in EXPECTED_SURFACE:
            params = set(inspect.signature(getattr(BoardProvider, name)).parameters)
            assert not params & {"owner", "repo", "issue_number", "repository"}, (
                f"{name} takes a GitHub-shaped argument, so a Jira key could not be passed"
            )


class TestACardIdIsOpaque:
    def test_a_jira_style_board_satisfies_the_protocol(self) -> None:
        """FR-010 — a provider with one identifier implements this with no translation."""
        assert isinstance(StubBoard(), BoardProvider)

    @pytest.mark.asyncio
    async def test_a_key_that_is_not_a_number_is_passed_through_untouched(self) -> None:
        """FR-002 / SC-002 — nothing parses, casts, or rejects the id."""
        board = StubBoard()
        got = await board.get_card_comments("PROJ-123", since_id=None)

        assert got == [COMMENT]
        assert board.calls == ["get_card_comments:PROJ-123"], (
            "the id reaching the provider must be exactly the id coordinare holds"
        )


class TestTheTranslationCostsNothing:
    """US2 — the requirement that makes US1 deployable under a rate limit."""

    @pytest.mark.asyncio
    async def test_a_polled_card_costs_exactly_one_request(self) -> None:
        """SC-003 — asserted as the whole call sequence, not the absence of one call.

        Checking ``get_issue_details`` was not called only catches the extra call
        you predicted. Asserting the exact sequence fails on any round trip that
        appears later, including one nobody thought of.
        """
        from coordinare.services.board_provider import GitHubProjectsBoardProvider

        github = RecordingGitHub(issue_numbers={"PVTI_abc": 42}, comments=[COMMENT])
        provider = GitHubProjectsBoardProvider(github)

        await provider.poll_board()
        got = await provider.get_card_comments("PVTI_abc")

        assert got == [COMMENT]
        assert github.calls == ["poll_board", "get_issue_comments"], (
            f"expected one poll and one comment fetch, got {github.calls}"
        )
        assert github.last_issue_number == 42, "the node id must be translated, not passed through"

    @pytest.mark.asyncio
    async def test_an_unpolled_card_resolves_once_and_is_remembered(self) -> None:
        """FR-005 — the fallback fires, and only once.

        Without remembering, a card outside the polled set pays the extra lookup
        on every cycle rather than once, which is the same rate-limit problem in
        slow motion.
        """
        from coordinare.services.board_provider import GitHubProjectsBoardProvider

        github = RecordingGitHub(details={"PVTI_zzz": {"number": 77}}, comments=[COMMENT])
        provider = GitHubProjectsBoardProvider(github)

        await provider.get_card_comments("PVTI_zzz")
        assert github.calls == ["get_issue_details", "get_issue_comments"]

        await provider.get_card_comments("PVTI_zzz")
        assert github.calls == [
            "get_issue_details",
            "get_issue_comments",
            "get_issue_comments",
        ], "the second read resolved again; the fallback result was not remembered"

    @pytest.mark.asyncio
    async def test_an_unresolvable_card_yields_nothing_and_does_not_raise(self) -> None:
        """FR-006 — matches today: a board hiccup must not stall the cycle."""
        from coordinare.services.board_provider import GitHubProjectsBoardProvider

        github = RecordingGitHub(details={})  # nothing resolves
        provider = GitHubProjectsBoardProvider(github)

        assert await provider.get_card_comments("PROJ-nope") == []

    @pytest.mark.asyncio
    async def test_two_services_do_not_share_a_map(self) -> None:
        """FR-004 / research R5 — a shared map would be a correctness bug, not untidiness.

        Two symphonies on different repositories can hold the same issue number
        for different cards, so the map must be per service instance.
        """
        from coordinare.services.github import GitHubService

        one = GitHubService.__new__(GitHubService)
        two = GitHubService.__new__(GitHubService)
        one._remember_issue_numbers({"CARD": 1})
        two._remember_issue_numbers({"CARD": 2})

        assert await one.issue_number_for_card("CARD") == 1
        assert await two.issue_number_for_card("CARD") == 2


class TestCommentRoutingRunsOnANonGitHubBoard:
    """US1 / SC-001 — the whole feature, exercised through the real graph node."""

    @staticmethod
    def _state(board: StubBoard, github, card: dict) -> dict:
        return {
            "board_provider": board,
            "github_service": github,
            "current_card": card,
            "last_issue_comment_id": None,
            "processed_issue_comment_ids": [],
        }

    @pytest.mark.asyncio
    async def test_the_node_reads_comments_from_the_board_not_the_code_host(self) -> None:
        from coordinare.graph.nodes.route_issue_comments import route_issue_comments

        board = StubBoard()
        github = RecordingGitHub()
        card = {"id": "PROJ-123", "issue_number": 7}

        await route_issue_comments(self._state(board, github, card))

        assert board.calls == ["get_card_comments:PROJ-123"]
        assert "get_issue_comments" not in github.calls, (
            "the node fetched comments from the code host; the seam leaks"
        )

    @pytest.mark.asyncio
    async def test_a_card_with_no_issue_number_still_routes(self) -> None:
        """Research R3 — the gate that would have made this feature silently do nothing.

        ``route_issue_comments`` used to return early when the card carried no
        GitHub issue number. A Jira card never has one, so every provider beneath
        it could be perfect and no comment would ever route.
        """
        from coordinare.graph.nodes.route_issue_comments import route_issue_comments

        board = StubBoard()
        github = RecordingGitHub()
        card = {"id": "PROJ-123"}  # no issue_number at all

        await route_issue_comments(self._state(board, github, card))

        assert board.calls == ["get_card_comments:PROJ-123"], (
            "a card without a GitHub issue number routed nothing"
        )


class TestTheRealPollBoardWiresTheRemembering:
    """The fake above calls ``_remember_issue_numbers`` because it was told to.

    That proves the translation works, not that production ever populates it. If
    the real ``poll_board`` stopped remembering, every fake-based test here would
    still pass while coordinare paid a details lookup on every comment read. So
    this drives the real method with only its transport stubbed.
    """

    @pytest.mark.asyncio
    async def test_polling_populates_the_map(self, monkeypatch) -> None:
        service = GitHubService.__new__(GitHubService)
        service.project_id = "PVT_test"

        page = {
            "node": {
                "items": {
                    "nodes": [
                        {
                            "id": "PVTI_abc",
                            "fieldValues": {"nodes": [{"name": "Todo"}]},
                            "content": {"id": "I_1", "number": 42, "title": "t", "body": "b"},
                        },
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            },
        }
        monkeypatch.setattr(GitHubService, "_ensure_initialized", lambda self: None)
        monkeypatch.setattr(
            GitHubService, "_guarded_execute", lambda self, q, v=None: _immediately(page),
        )

        await service.poll_board()

        # Resolves with no details lookup, because polling supplied the pairing.
        assert await service.issue_number_for_card("PVTI_abc") == 42


async def _immediately(value):
    return value


class TestACardThatWillNeverResolve:
    """A draft issue in a GitHub project has no issue number, ever.

    Found by tracing the graph rather than by a failing test: ``route_issue_comments``
    runs immediately before ``check_board``, so it reads comments once per cycle. A card
    that cannot resolve and is not remembered as such would repeat the details lookup on
    every one of those cycles, forever — the exact per-cycle API cost this whole design
    exists to avoid, reintroduced by the failure path.
    """

    @pytest.mark.asyncio
    async def test_it_is_looked_up_once_and_not_again(self) -> None:
        github = RecordingGitHub(details={})  # nothing resolves

        assert await github.issue_number_for_card("DRAFT_1") is None
        assert await github.issue_number_for_card("DRAFT_1") is None
        assert await github.issue_number_for_card("DRAFT_1") is None

        assert github.calls == ["get_issue_details"], (
            f"an unresolvable card was looked up {github.calls.count('get_issue_details')} "
            "times; it must be remembered as unresolvable after the first"
        )

    @pytest.mark.asyncio
    async def test_a_later_poll_can_still_resolve_it(self) -> None:
        """Remembering the failure must not make it permanent.

        A draft issue converted to a real one gains a number, and the next poll
        supplies it. A negative entry that could never be overwritten would leave
        that card unreadable for the life of the process.
        """
        github = RecordingGitHub(details={})
        assert await github.issue_number_for_card("DRAFT_1") is None

        github._remember_issue_numbers({"DRAFT_1": 99})  # the card was converted

        assert await github.issue_number_for_card("DRAFT_1") == 99
        assert github.calls == ["get_issue_details"], "no second lookup was needed"

    @pytest.mark.asyncio
    async def test_a_lookup_that_throws_is_also_remembered(self) -> None:
        """The failure path had the same defect as the empty-result path.

        Caching only the "resolved to nothing" case leaves the "blew up" case
        retrying every cycle — worst exactly when the API is already unhappy, since
        that is when lookups throw. Found by review after the first fix.
        """

        class ExplodingGitHub(RecordingGitHub):
            async def get_issue_details(self, card_id: str) -> dict:
                self.calls.append("get_issue_details")
                raise RuntimeError("GitHub said no")

        github = ExplodingGitHub()

        assert await github.issue_number_for_card("ITEM_X") is None
        assert await github.issue_number_for_card("ITEM_X") is None

        assert github.calls == ["get_issue_details"], (
            "a throwing lookup was retried; it must be remembered like any other failure"
        )

    @pytest.mark.asyncio
    async def test_a_poll_heals_a_remembered_failure(self) -> None:
        """Remembering a transient failure must not strand the card permanently."""

        class ExplodingGitHub(RecordingGitHub):
            async def get_issue_details(self, card_id: str) -> dict:
                self.calls.append("get_issue_details")
                raise RuntimeError("GitHub said no")

        github = ExplodingGitHub()
        assert await github.issue_number_for_card("ITEM_X") is None

        await github.poll_board()  # the board still has the card
        github._remember_issue_numbers({"ITEM_X": 55})

        assert await github.issue_number_for_card("ITEM_X") == 55
