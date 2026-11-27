"""Issue #395: callers read one typed record per work item, not GitHub side tables.

``poll_board`` used to return the snapshot plus seven parallel dicts keyed by card
id. A tracker with no issue numbers and no node ids could not fill half of them,
so the callers that read them could not run against it. They now read
``work_items_of(board)``. The GitHub Projects listing is translated in the
provider layer, so the service and every existing fake keep their shape.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from coordinare.models.dependency import DependencySource, DependencyStatus
from coordinare.services.dependency import (
    build_graph,
    filter_eligible_todo,
    resolve_off_board_dependencies,
)
from coordinare.services.in_memory_board import InMemoryBoardProvider
from coordinare.services.work_items import (
    CLOSED,
    OPEN,
    Blocker,
    IssueLink,
    WorkItem,
    board_metadata,
    work_items_of,
)

#: The callers the issue names, and the side tables they may no longer read.
CALLERS = [
    "src/coordinare/graph/nodes/check_board.py",
    "src/coordinare/services/dependency.py",
    "src/coordinare/services/card_ownership.py",
    "src/coordinare/services/todo_waits.py",
    "src/coordinare/graph/nodes/dispatch_performer.py",
    "src/coordinare/services/orphan_pr.py",
    "src/coordinare/dashboard/store.py",
    "src/coordinare/daemon.py",
]
SIDE_TABLES = {"issue_numbers", "content_node_ids", "item_labels", "item_assignees", "native_blockers"}


def test_no_caller_reads_a_side_table() -> None:
    """Asserted on the AST: a string constant naming a side table is a read of it."""
    offenders: list[str] = []
    for path in CALLERS:
        for node in ast.walk(ast.parse(Path(path).read_text())):
            if isinstance(node, ast.Constant) and node.value in SIDE_TABLES:
                offenders.append(f"{path}:{node.lineno} {node.value!r}")
    assert not offenders, f"side tables still read by callers: {offenders}"


# --- the GitHub Projects listing, translated ----------------------------------------------


GITHUB_BOARD: dict[str, Any] = {
    "snapshot": {"TODO": ["PVTI_1", "PVTI_2"], "DONE": ["PVTI_3"]},
    "titles": {"PVTI_1": "One", "PVTI_2": "Two", "PVTI_3": "Three"},
    "descriptions": {"PVTI_1": "Depends on #3", "PVTI_2": "", "PVTI_3": ""},
    "issue_numbers": {"PVTI_1": 1, "PVTI_2": 2, "PVTI_3": 3},
    "issue_urls": {"PVTI_1": "https://github.com/o/r/issues/1"},
    "content_node_ids": {"PVTI_1": "I_1", "PVTI_2": "I_2"},
    "pr_urls": {"PVTI_1": "https://github.com/o/r/pull/9"},
    "item_labels": {"PVTI_1": ["bug"]},
    "item_assignees": {"PVTI_2": ["alice"]},
    "native_blockers": {"PVTI_2": [{"number": 7, "state": "CLOSED", "repo": "o/other"}]},
}


class TestTheGitHubListingTranslation:
    def test_each_card_becomes_one_record(self) -> None:
        items = work_items_of(GITHUB_BOARD)

        assert list(items) == ["PVTI_1", "PVTI_2", "PVTI_3"]
        one = items["PVTI_1"]
        assert one.key == "#1"
        assert one.url == "https://github.com/o/r/issues/1"
        assert one.title == "One"
        assert one.status == "TODO"
        assert one.labels == ("bug",)
        assert one.pr_url == "https://github.com/o/r/pull/9"
        assert one.issue == IssueLink(number=1, node_id="I_1")
        assert items["PVTI_2"].owners == ("alice",)

    def test_both_blocker_syntaxes_are_on_the_record(self) -> None:
        items = work_items_of(GITHUB_BOARD)

        (text,) = items["PVTI_1"].blockers
        assert text == Blocker(ref="#3", item_id="PVTI_3", issue=IssueLink(number=3))
        (native,) = items["PVTI_2"].blockers
        assert native.native and native.state == CLOSED
        assert native.ref == "o/other#7"

    def test_the_dependency_check_reads_them_as_before(self) -> None:
        graph = build_graph(GITHUB_BOARD, repo="o/r")

        (explicit,) = graph.by_dependent["PVTI_1"]
        assert (explicit.blocker_issue_number, explicit.source, explicit.status) == (
            3, DependencySource.EXPLICIT, DependencyStatus.SATISFIED,
        )
        (native,) = graph.by_dependent["PVTI_2"]
        assert native.source == DependencySource.NATIVE_RELATIONSHIP
        assert native.status == DependencyStatus.SATISFIED
        assert native.blocker_repo == "o/other"

    def test_the_state_maps_keep_their_shape(self) -> None:
        metadata = board_metadata(GITHUB_BOARD)

        assert metadata["_board_issue_numbers"] == {"PVTI_1": 1, "PVTI_2": 2, "PVTI_3": 3}
        assert metadata["_board_item_labels"]["PVTI_1"] == ["bug"]
        assert metadata["_board_pr_urls"] == {"PVTI_1": "https://github.com/o/r/pull/9"}


# --- a provider with nothing GitHub-shaped ------------------------------------------------


class TestATrackerWithoutIssueNumbers:
    @pytest.mark.asyncio
    async def test_its_records_carry_no_issue(self) -> None:
        board = InMemoryBoardProvider(project_key="PAY")
        card = board.add_item("One", labels=("docs",), owners=("pat",))

        (item,) = work_items_of(await board.poll_board()).values()

        assert item == WorkItem(
            id=card, key=card, url=f"memory://work-items/{card}", title="One", status="TODO",
            native_status="To Do", owners=("pat",), labels=("docs",),
        )
        assert item.issue is None

    @pytest.mark.asyncio
    async def test_an_on_board_blocker_holds_the_item_until_it_is_done(self) -> None:
        board = InMemoryBoardProvider(project_key="PAY")
        first = board.add_item("First")
        second = board.add_item("Second", blocked_by=(first,))

        listing = await board.poll_board()
        graph = build_graph(listing)
        (dep,) = graph.by_dependent[second]
        assert dep.status == DependencyStatus.PENDING
        assert dep.label == first
        assert filter_eligible_todo([first, second], graph) == [first]

        await board.move_card(first, "DONE")
        graph = build_graph(await board.poll_board())
        assert filter_eligible_todo([second], graph) == [second]

    @pytest.mark.asyncio
    async def test_an_off_board_blocker_reads_the_state_the_tracker_reports(self) -> None:
        board = InMemoryBoardProvider(project_key="PAY")
        closed = board.add_item("A", blocked_by=("OPS-1",), external_states={"OPS-1": CLOSED})
        still_open = board.add_item("B", blocked_by=("OPS-2",), external_states={"OPS-2": OPEN})
        unknown = board.add_item("C", blocked_by=("OPS-3",))

        graph = build_graph(await board.poll_board())

        assert graph.by_dependent[closed][0].status == DependencyStatus.SATISFIED
        assert graph.by_dependent[still_open][0].status == DependencyStatus.PENDING
        assert graph.by_dependent[unknown][0].status == DependencyStatus.UNRESOLVABLE

    @pytest.mark.asyncio
    async def test_a_cycle_between_items_is_found_by_id(self) -> None:
        board = InMemoryBoardProvider(project_key="PAY")
        board.add_item("A", blocked_by=("PAY-2",))
        board.add_item("B", blocked_by=("PAY-1",))

        graph = build_graph(await board.poll_board())

        assert graph.cycles == [["PAY-1", "PAY-2"]]

    @pytest.mark.asyncio
    async def test_github_is_never_asked_about_a_blocker_it_does_not_have(self) -> None:
        board = InMemoryBoardProvider(project_key="PAY")
        board.add_item("A", blocked_by=("OPS-3",))
        graph = build_graph(await board.poll_board())

        class Github:
            def __init__(self) -> None:
                self.asked: list[int] = []

            async def check_issue_state(self, repo: str, number: int) -> str:
                self.asked.append(number)
                return "closed"

        github = Github()
        await resolve_off_board_dependencies(graph, github, "o/r")
        assert github.asked == []

    @pytest.mark.asyncio
    async def test_empty_owners_labels_and_blockers_need_no_special_case(self) -> None:
        from coordinare.services.card_ownership import OwnershipPolicy, owns_card

        board = InMemoryBoardProvider(project_key="PAY")
        card = board.add_item("Bare")
        listing = await board.poll_board()

        assert build_graph(listing).by_dependent == {}
        assert board_metadata(listing)["_board_item_labels"] == {card: []}
        owners = {item_id: list(wi.owners) for item_id, wi in work_items_of(listing).items()}
        assert owns_card(OwnershipPolicy(), card, owners)


# --- the graph node, against a provider with no issue numbers -------------------------------


@pytest.mark.asyncio
async def test_check_board_builds_the_card_from_the_record() -> None:
    from unittest.mock import AsyncMock

    from coordinare.graph.nodes.check_board import check_board

    board = InMemoryBoardProvider(project_key="PAY")
    card = board.add_item("Add a health endpoint", body="We need /health.", labels=("docs",))

    result = await check_board(
        {
            "board_provider": board,
            "github_service": AsyncMock(),
            "advocate_handled_label": "advocate-handled",
            "advocate_escalation_label": "needs-human",
            "advocate_history": set(),
        },
    )

    assert result["active_card_id"] == card
    current = result["current_card"]
    assert current["title"] == "Add a health endpoint"
    assert current["description"] == "We need /health."
    assert current["issue_number"] == 0
    assert current["issue_id"] == ""
    assert current["issue_url"] == f"memory://work-items/{card}"
    assert result["_board_item_labels"] == {card: ["docs"]}


@pytest.mark.asyncio
async def test_an_escalated_item_is_left_alone_on_any_provider() -> None:
    from unittest.mock import AsyncMock

    from coordinare.graph.nodes.check_board import check_board

    board = InMemoryBoardProvider(project_key="PAY")
    board.add_item("Needs a person", labels=("needs-human",))

    result = await check_board(
        {
            "board_provider": board,
            "github_service": AsyncMock(),
            "advocate_handled_label": "advocate-handled",
            "advocate_escalation_label": "needs-human",
            "advocate_history": set(),
        },
    )

    assert not result.get("active_card_id")


@pytest.mark.asyncio
async def test_a_blocker_without_an_issue_is_named_by_its_reference() -> None:
    from coordinare.services.todo_waits import _label

    board = InMemoryBoardProvider(project_key="PAY")
    first = board.add_item("First")
    second = board.add_item("Second", blocked_by=(first,))

    (dep,) = build_graph(await board.poll_board()).by_dependent[second]

    assert dep.blocker_issue_number == 0
    assert _label(dep) == "PAY-1"


def test_an_unknown_field_adds_no_item() -> None:
    board = InMemoryBoardProvider(project_key="PAY")
    with pytest.raises(TypeError, match="label"):
        board.add_item("x", label=("bug",))
    assert board.add_item("y") == "PAY-1"
