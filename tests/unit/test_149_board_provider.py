"""Spec 149 / issue #203 — the board seam.

Coordinare reads work from GitHub Projects and nowhere else. This is the protocol
that makes another source possible, plus the GitHub implementation moved behind
it. Jira is the reason it exists; Jira is not in this spec.

The acceptance evidence for the extraction is unusual and strong: **the existing
suite passes unmodified**. These tests cover what that cannot — the protocol's
shape, and that it does not quietly assume GitHub.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from coordinare.services.board_provider import (
    BoardProvider,
    GitHubProjectsBoardProvider,
    MoveOutcome,
)

#: What coordinare actually calls. Derived from the call-site survey, not from
#: GitHubService's surface — the point of the exercise.
EXPECTED_SURFACE = {
    "poll_board",
    "move_card",
    "get_card",
    "add_card_comment",
}


class FakeGitHub:
    """Records what the adapter forwards, so delegation can be asserted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def poll_board(self):
        self.calls.append(("poll_board", ()))
        return {"snapshot": {"IN_PROGRESS": ["card-1"]}}

    async def move_card(self, item_id, status):
        self.calls.append(("move_card", (item_id, status)))

    async def get_issue_details(self, issue_id):
        self.calls.append(("get_issue_details", (issue_id,)))
        return {"title": "t"}

    async def add_comment(self, subject_id, body):
        self.calls.append(("add_comment", (subject_id, body)))
        return {"id": "c1"}

    async def get_issue_comments(self, issue_number, since_id=None):
        self.calls.append(("get_issue_comments", (issue_number, since_id)))
        return []


class TestTheProtocolIsNoBiggerThanTheCallSites:
    """SC-002 — the finding that made this worth doing.

    ``GitHubService`` has 48 methods and 26 are called externally. A protocol
    built from its surface would have been three times this size and would have
    pulled the code host in with it.
    """

    def test_the_surface_is_exactly_the_board_operations(self) -> None:
        actual = {m for m in dir(BoardProvider) if not m.startswith("_")}
        assert actual == EXPECTED_SURFACE, (
            f"protocol surface drifted to {sorted(actual)}. Adding an operation means "
            "every future provider must implement it — so add it only when a call "
            "site genuinely needs it."
        )

    def test_no_code_host_operation_leaked_in(self) -> None:
        """FR-008 / SC-005 — a Jira board pairs with a git host, and cannot replace it."""
        forbidden = ("pr", "merge", "branch", "review", "diff", "commit", "file", "job")
        for name in EXPECTED_SURFACE:
            assert not any(word in name.lower() for word in forbidden), (
                f"{name} is a code-host operation; those stay GitHub"
            )


class TestItDoesNotAssumeGitHub:
    def test_card_ids_are_opaque(self) -> None:
        """FR-006 — on GitHub a card IS an issue in the PR's repo; on Jira it is not.

        Asserted on the signatures: nothing takes an owner, a repo or an issue
        number, because each of those would make a Jira key unrepresentable.
        """
        for name in EXPECTED_SURFACE:
            params = set(inspect.signature(getattr(BoardProvider, name)).parameters)
            assert not params & {"owner", "repo", "issue_number", "repository"}, (
                f"{name} takes a GitHub-shaped argument, so a Jira key could not be passed"
            )

    def test_a_refused_move_is_an_outcome_rather_than_a_fault(self) -> None:
        """FR-005 — Jira workflows can forbid a transition, which is an answer.

        A provider that had to raise for this would make every call site treat a
        normal board rule as an error.
        """
        refused = MoveOutcome.refused("transition not allowed from Done")
        assert not refused.moved
        assert "transition" in refused.reason
        assert MoveOutcome.ok().moved

    def test_the_protocol_is_documented_as_carrying_canonical_statuses(self) -> None:
        """FR-009 — a native lane name crossing this boundary would defeat the seam."""
        doc = (BoardProvider.__doc__ or "").lower()
        assert "canonical" in doc and "native" in doc


class TestTheGitHubProviderDelegatesRatherThanReimplements:
    """FR-003 — 'no behaviour change' should be structural, not a claim.

    The adapter forwards to the same service methods that ran before, so the code
    executing is the code that already existed.
    """

    def test_it_satisfies_the_protocol(self) -> None:
        assert isinstance(GitHubProjectsBoardProvider(FakeGitHub()), BoardProvider)

    @pytest.mark.parametrize(
        ("call", "args", "forwards_to"),
        [
            ("poll_board", (), "poll_board"),
            ("move_card", ("card-1", "IN_REVIEW"), "move_card"),
            ("get_card", ("card-1",), "get_issue_details"),
            ("add_card_comment", ("card-1", "hello"), "add_comment"),
        ],
    )
    def test_each_operation_forwards_to_the_existing_method(
        self, call: str, args: tuple, forwards_to: str
    ) -> None:
        import asyncio

        github = FakeGitHub()
        provider = GitHubProjectsBoardProvider(github)
        asyncio.run(getattr(provider, call)(*args))
        assert [name for name, _ in github.calls] == [forwards_to]

    def test_a_successful_move_reports_success(self) -> None:
        import asyncio

        provider = GitHubProjectsBoardProvider(FakeGitHub())
        assert asyncio.run(provider.move_card("card-1", "DONE")).moved

    def test_a_real_failure_still_raises(self) -> None:
        """Refusal is an outcome; an unreachable board is not.

        Collapsing both into MoveOutcome would hide a broken deployment behind a
        value that reads like a board rule.
        """
        import asyncio

        class Broken(FakeGitHub):
            async def move_card(self, item_id, status):
                raise ConnectionError("board unreachable")

        provider = GitHubProjectsBoardProvider(Broken())
        with pytest.raises(ConnectionError):
            asyncio.run(provider.move_card("card-1", "DONE"))


class TestTheExclusionsAreDeliberate:
    """Four operations look board-ish and are not.

    ``ensure_labels_exist``, ``add_labels``, ``list_open_issues`` and
    ``check_issue_state`` operate on a GitHub *repository's* issues — the advocate
    scanning for candidate work, and dependency checks. On Jira the board is the
    source of work and there is no repo to scan, so the equivalent is a different
    feature rather than a different implementation. In the protocol they would
    oblige every provider to implement something meaningless to it.
    """

    @pytest.mark.parametrize(
        "excluded",
        ["ensure_labels_exist", "add_labels", "list_open_issues", "check_issue_state"],
    )
    def test_repository_scoped_operations_stay_out(self, excluded: str) -> None:
        assert excluded not in EXPECTED_SURFACE
        assert not hasattr(BoardProvider, excluded)

    def test_the_reasoning_is_written_down(self) -> None:
        """So the next person does not 'complete' the protocol by adding them."""
        module = Path("src/coordinare/services/board_provider.py").read_text()
        assert "deliberately NOT here" in module
        assert "ensure_labels_exist" in module


class TestNoCallerBranchesOnProviderType:
    """SC-004 / FR-004 — a conditional on provider type means nothing separated."""

    def test_the_graph_and_daemon_contain_no_provider_isinstance(self) -> None:
        offenders: list[str] = []
        for path in [*Path("src/coordinare/graph").rglob("*.py"), Path("src/coordinare/daemon.py")]:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call) and getattr(node.func, "id", "") == "isinstance"
                ):
                    continue
                rendered = ast.unparse(node)
                if "BoardProvider" in rendered or "GitHubProjects" in rendered:
                    offenders.append(f"{path}: {rendered}")
        assert not offenders, offenders


class TestAStubProviderCanDriveCoordinare:
    """SC-003 — the criterion that separates a real seam from a protocol.

    A protocol can exist while every call site still reaches GitHub directly. The
    question is whether something that is *not* GitHub can supply the board, so
    this substitutes one and asserts coordinare talks to it — and, just as
    importantly, does not talk to the GitHub service for board work.
    """

    class StubBoard:
        """A board that is not GitHub, implementing nothing but the protocol."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def poll_board(self):
            self.calls.append("poll_board")
            return {"snapshot": {"TODO": ["JIRA-1"]}, "titles": {"JIRA-1": "a card"}}

        async def move_card(self, card_id, status):
            self.calls.append(f"move_card:{card_id}->{status}")
            return MoveOutcome.ok()

        async def get_card(self, card_id):
            self.calls.append("get_card")
            return {"title": "a card", "body": ""}

        async def add_card_comment(self, card_id, body):
            self.calls.append("add_card_comment")
            return {}

    class ExplodingGitHub:
        """Fails loudly if board work is routed to the code host.

        A silent fallback would let the seam look complete while coordinare still
        depended on GitHub for the board — exactly what this test exists to rule
        out.
        """

        def __getattr__(self, name):
            board_ops = {
                "poll_board",
                "move_card",
                "get_issue_details",
                "add_comment",
                "get_issue_comments",
            }
            if name in board_ops:
                raise AssertionError(
                    f"board operation {name!r} reached the GitHub service; the seam leaks"
                )
            raise AttributeError(name)

    def test_the_stub_satisfies_the_protocol(self) -> None:
        assert isinstance(self.StubBoard(), BoardProvider)

    def test_board_work_goes_to_the_stub_and_never_to_github(self) -> None:
        import asyncio

        from coordinare.services.board_provider import board_of

        stub = self.StubBoard()
        state = {"board_provider": stub, "github_service": self.ExplodingGitHub()}

        provider = board_of(state)
        assert provider is stub, "the configured provider must win over the service"

        asyncio.run(provider.poll_board())
        asyncio.run(provider.move_card("JIRA-1", "IN_PROGRESS"))
        assert stub.calls == ["poll_board", "move_card:JIRA-1->IN_PROGRESS"]

    def test_a_card_id_that_is_not_a_github_node_works(self) -> None:
        """FR-006 — 'JIRA-1' is not a GitHub node id, and nothing may assume it is."""
        import asyncio

        stub = self.StubBoard()
        assert asyncio.run(stub.move_card("PROJ-123", "DONE")).moved

    def test_the_graph_reaches_the_board_only_through_the_provider(self) -> None:
        """Asserted over the AST, because a single missed call site is the whole bug.

        Grepping the method name would not do: what matters is the *receiver*. A
        call left on the GitHub service works today, since both are GitHub, and
        breaks silently the moment a second provider exists.

        The receiver must be the bare name ``board_provider`` rather than merely
        contain it. A substring test would accept ``board_provider_github`` and
        so could miss the very leak it exists to catch; requiring the exact name
        makes the convention enforceable, and a rename is a deliberate edit here.
        """
        board_ops = {"poll_board", "move_card", "get_issue_details", "get_issue_comments"}
        leaks: list[str] = []
        for path in Path("src/coordinare/graph").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in board_ops:
                    continue
                receiver = ast.unparse(node.func.value)
                if receiver != "board_provider":
                    leaks.append(f"{path.name}:{node.lineno} {receiver}.{node.func.attr}")
        assert not leaks, f"board calls still bound to the code host: {leaks}"

    def test_every_move_goes_through_the_helper_that_reports_refusals(self) -> None:
        """A bare ``provider.move_card`` would drop a refusal on the floor.

        ``move_card_or_warn`` is what makes ``MoveOutcome`` load-bearing, so a
        call site that skips it re-opens the silence the outcome exists to close.
        """
        bare: list[str] = []
        for directory in ("src/coordinare/graph", "src/coordinare/services"):
            for path in Path(directory).rglob("*.py"):
                if path.name == "board_provider.py":
                    continue  # the helper itself must call it
                for node in ast.walk(ast.parse(path.read_text())):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "move_card"
                        and ast.unparse(node.func.value) == "board_provider"
                    ):
                        bare.append(f"{path.name}:{node.lineno}")
        assert not bare, f"moves bypassing move_card_or_warn, so a refusal is silent: {bare}"

    @pytest.mark.asyncio
    async def test_a_real_node_completes_a_cycle_against_a_non_github_board(self) -> None:
        """SC-003 in full — the actual `check_board` node, not a stand-in for it.

        The earlier tests prove the seam resolves. This proves coordinare *runs* on
        the other side of it: a real graph node polls a board that is not GitHub,
        picks up a card whose id no GitHub API would accept, and reaches dispatch.
        The GitHub service is present throughout (repo work still needs it) and is
        asserted never to have been used for board work.
        """
        from unittest.mock import AsyncMock

        from coordinare.graph.nodes.check_board import check_board

        stub = self.StubBoard()
        stub.poll_board = self._board_with_one_card(stub)  # type: ignore[method-assign]
        github = AsyncMock()

        result = await check_board(
            {
                "board_provider": stub,
                "github_service": github,
                "advocate_handled_label": "advocate-handled",
                "advocate_escalation_label": "needs-human",
                "advocate_history": set(),
            }
        )

        assert result["phase"] == "dispatching"
        assert result["active_card_id"] == "JIRA-1"
        assert stub.calls == ["poll_board"]
        assert not github.poll_board.called, "the node polled GitHub, not the board"

    @staticmethod
    def _board_with_one_card(stub):
        async def poll_board():
            stub.calls.append("poll_board")
            return {
                "snapshot": {
                    "TODO": ["JIRA-1"],
                    "IN_PROGRESS": [],
                    "IN_REVIEW": [],
                    "BLOCKED": [],
                    "DONE": [],
                },
                "titles": {"JIRA-1": "Add a health endpoint"},
                "descriptions": {"JIRA-1": "We need /health."},
                "issue_numbers": {"JIRA-1": 1},
                "item_labels": {"JIRA-1": []},
            }

        return poll_board


class TestARefusalIsNotSilent:
    """The gap an adversarial review found: `MoveOutcome` that nobody reads.

    A protocol can model a board declining a transition and still lose the
    information entirely, if every call site throws the return value away. Then
    coordinare carries on believing the card moved while the board says otherwise
    — worse than not modelling refusal at all, because the design looks handled.
    """

    class RefusingBoard:
        async def move_card(self, card_id, status):
            return MoveOutcome.refused("workflow forbids In Review -> Done")

    class AcceptingBoard:
        def __init__(self) -> None:
            self.moved: list[tuple[str, str]] = []

        async def move_card(self, card_id, status):
            self.moved.append((card_id, status))
            return MoveOutcome.ok()

    @pytest.mark.asyncio
    async def test_a_refusal_is_reported_and_returned(self) -> None:
        from unittest.mock import patch

        from coordinare.services import board_provider as mod

        with patch.object(mod, "logger") as log:
            moved = await mod.move_card_or_warn(self.RefusingBoard(), "PROJ-7", "DONE")

        assert moved is False, "the caller must be able to see that nothing moved"
        assert log.warning.called, "a refused move passed without a word"
        event, kwargs = log.warning.call_args[0][0], log.warning.call_args[1]
        assert event == "board.move_refused"
        assert kwargs["reason"] == "workflow forbids In Review -> Done"

    @pytest.mark.asyncio
    async def test_an_accepted_move_is_quiet_and_reaches_the_board(self) -> None:
        from unittest.mock import patch

        from coordinare.services import board_provider as mod

        board = self.AcceptingBoard()
        with patch.object(mod, "logger") as log:
            moved = await mod.move_card_or_warn(board, "PROJ-7", "DONE")

        assert moved is True
        assert board.moved == [("PROJ-7", "DONE")]
        assert not log.warning.called, "a normal move must not warn"

    @pytest.mark.asyncio
    async def test_no_board_raises_the_way_it_used_to(self) -> None:
        """Behaviour preservation, not tidiness.

        The old code awaited ``github.move_card`` on a possibly-``None`` service,
        so a missing board raised ``AttributeError`` into a handler that every
        call site already has — and several of those handlers do more than log.
        Returning quietly here would skip that work in silence.
        """
        from coordinare.services.board_provider import move_card_or_warn

        with pytest.raises(AttributeError):
            await move_card_or_warn(None, "PROJ-7", "DONE")


class TestCancellationUsesTheConfiguredBoard:
    """Found by review: `cancel.py` built its own GitHub provider.

    Constructing ``GitHubProjectsBoardProvider(github)`` directly type-checks and
    passes every test, because today there is only one provider. It also ignores
    whatever board the deployment configured — so the card returns to TODO on
    GitHub while the real board still shows it active.
    """

    def test_it_resolves_the_board_rather_than_constructing_one(self) -> None:
        source = Path("src/coordinare/cancel.py").read_text()
        assert "GitHubProjectsBoardProvider(" not in source, (
            "cancel.py constructs a GitHub provider directly, bypassing any configured board"
        )
        assert "board_of(state)" in source

    @pytest.mark.asyncio
    async def test_it_moves_the_card_on_the_configured_board(self) -> None:
        from unittest.mock import AsyncMock

        from coordinare.cancel import cancel_active_card

        board = TestARefusalIsNotSilent.AcceptingBoard()
        github = AsyncMock()
        state = {
            "board_provider": board,
            "github_service": github,
            "current_card": {"id": "JIRA-9"},
            "phase": "implementing",
        }
        await cancel_active_card(state, move_to_todo=True)

        assert board.moved == [("JIRA-9", "TODO")]
        assert not github.move_card.called, "cancellation wrote to GitHub, not the board"


class TestTheCommentGapIsStatedRatherThanHidden:
    """SC-002 held honestly: the protocol dropped what nothing calls.

    ``get_card_comments`` was in the protocol and called from nowhere. Keeping it
    would have advertised a capability the graph never exercises, and its GitHub
    adapter had to take an issue *number* where every other method takes a card
    id — a contract that was already false. Removing it leaves a real limitation,
    so the limitation is written down.
    """

    def test_the_protocol_does_not_carry_an_uncalled_method(self) -> None:
        assert "get_card_comments" not in EXPECTED_SURFACE
        assert not hasattr(BoardProvider, "get_card_comments")

    def test_the_limitation_is_documented(self) -> None:
        import coordinare.services.board_provider as mod

        doc = " ".join((mod.__doc__ or "").lower().split())
        assert "comment" in doc, "the comment-routing gap must be stated, not silently left"
        assert "issue number" in doc, (
            "the gap is the id model — say so, so the next reader does not "
            "'fix' it by widening the protocol"
        )
