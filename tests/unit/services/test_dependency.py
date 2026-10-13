"""Unit tests for the dependency detection service (046)."""
from __future__ import annotations

import pytest

from coordinare.models.dependency import (
    DependencySource,
    DependencyStatus,
)
from coordinare.services.dependency import (
    build_graph,
    filter_eligible_todo,
    parse_dependencies,
    resolve_comment_dependencies,
    resolve_off_board_dependencies,
)

# ---------------------------------------------------------------------------
# T008: parse_dependencies tests
# ---------------------------------------------------------------------------


class TestParseDependencies:
    def test_depends_on(self) -> None:
        assert parse_dependencies("Depends on #42") == [42]

    def test_blocked_by(self) -> None:
        assert parse_dependencies("Blocked by #10") == [10]

    def test_after(self) -> None:
        assert parse_dependencies("After #5") == [5]

    def test_requires(self) -> None:
        assert parse_dependencies("Requires #99") == [99]

    def test_case_insensitive(self) -> None:
        assert parse_dependencies("DEPENDS ON #7") == [7]
        assert parse_dependencies("blocked BY #8") == [8]

    def test_multiple_deps(self) -> None:
        text = "Depends on #10, also requires #20 and blocked by #30"
        result = parse_dependencies(text)
        assert result == [10, 20, 30]

    def test_deduplicates(self) -> None:
        text = "Depends on #5. Also depends on #5."
        assert parse_dependencies(text) == [5]

    def test_empty_input(self) -> None:
        assert parse_dependencies("") == []
        assert parse_dependencies("No dependencies here") == []

    def test_no_match(self) -> None:
        assert parse_dependencies("This card is about #42 but not a dep") == []

    def test_self_reference_not_filtered_here(self) -> None:
        """parse_dependencies doesn't know the card's own issue number —
        self-reference filtering is done in build_graph."""
        assert parse_dependencies("Depends on #1") == [1]

    def test_multiline_description(self) -> None:
        text = "## Description\nSome work.\n\nDepends on #10\nAlso after #20\n"
        assert parse_dependencies(text) == [10, 20]


# ---------------------------------------------------------------------------
# T009: build_graph + detect_cycles tests
# ---------------------------------------------------------------------------


def _board(
    items: dict[str, dict],
    done: list[str] | None = None,
) -> dict:
    """Helper to build a minimal board snapshot for testing.

    ``items`` maps item_id → {"number": int, "description": str, "title": str}.
    All items default to the TODO column unless their item_id is in ``done``.
    """
    done_set = set(done or [])
    snapshot: dict[str, list[str]] = {"TODO": [], "DONE": [], "IN_PROGRESS": [], "IN_REVIEW": []}
    titles: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    issue_numbers: dict[str, int] = {}

    for item_id, info in items.items():
        col = "DONE" if item_id in done_set else "TODO"
        snapshot[col].append(item_id)
        titles[item_id] = info.get("title", "")
        descriptions[item_id] = info.get("description", "")
        issue_numbers[item_id] = info.get("number", 0)

    return {
        "snapshot": snapshot,
        "titles": titles,
        "descriptions": descriptions,
        "issue_numbers": issue_numbers,
    }


class TestBuildGraph:
    def test_no_dependencies(self) -> None:
        board = _board({
            "A": {"number": 1, "description": "No deps"},
            "B": {"number": 2, "description": "Also clean"},
        })
        graph = build_graph(board)
        assert graph.dependencies == []
        assert graph.cycles == []

    def test_linear_chain(self) -> None:
        board = _board({
            "A": {"number": 1, "description": ""},
            "B": {"number": 2, "description": "Depends on #1"},
        })
        graph = build_graph(board)
        assert len(graph.dependencies) == 1
        dep = graph.dependencies[0]
        assert dep.dependent_item_id == "B"
        assert dep.blocker_issue_number == 1
        assert dep.status == DependencyStatus.PENDING
        assert dep.source == DependencySource.EXPLICIT

    def test_satisfied_dependency(self) -> None:
        board = _board(
            {"A": {"number": 1, "description": ""},
             "B": {"number": 2, "description": "Depends on #1"}},
            done=["A"],
        )
        graph = build_graph(board)
        dep = graph.dependencies[0]
        assert dep.status == DependencyStatus.SATISFIED

    def test_diamond_dependency(self) -> None:
        board = _board({
            "A": {"number": 1, "description": ""},
            "B": {"number": 2, "description": "Depends on #1"},
            "C": {"number": 3, "description": "Depends on #1"},
            "D": {"number": 4, "description": "Depends on #2. Depends on #3"},
        })
        graph = build_graph(board)
        d_deps = graph.by_dependent["D"]
        assert len(d_deps) == 2
        blocker_nums = {d.blocker_issue_number for d in d_deps}
        assert blocker_nums == {2, 3}

    def test_self_reference_filtered(self) -> None:
        board = _board({
            "A": {"number": 5, "description": "Depends on #5"},
        })
        graph = build_graph(board)
        assert graph.dependencies == []

    def test_off_board_dependency_unresolvable(self) -> None:
        board = _board({
            "A": {"number": 1, "description": "Depends on #999"},
        })
        graph = build_graph(board)
        dep = graph.dependencies[0]
        assert dep.status == DependencyStatus.UNRESOLVABLE

    def test_mixed_statuses(self) -> None:
        board = _board(
            {"A": {"number": 1, "description": ""},
             "B": {"number": 2, "description": ""},
             "C": {"number": 3, "description": "Depends on #1. Depends on #2"}},
            done=["A"],
        )
        graph = build_graph(board)
        c_deps = graph.by_dependent["C"]
        statuses = {d.blocker_issue_number: d.status for d in c_deps}
        assert statuses[1] == DependencyStatus.SATISFIED
        assert statuses[2] == DependencyStatus.PENDING


class TestDetectCycles:
    def test_no_cycles(self) -> None:
        board = _board({
            "A": {"number": 1, "description": ""},
            "B": {"number": 2, "description": "Depends on #1"},
        })
        graph = build_graph(board)
        assert graph.cycles == []

    def test_two_node_cycle(self) -> None:
        board = _board({
            "A": {"number": 1, "description": "Depends on #2"},
            "B": {"number": 2, "description": "Depends on #1"},
        })
        graph = build_graph(board)
        assert len(graph.cycles) == 1
        assert set(graph.cycles[0]) == {"A", "B"}

    def test_three_node_cycle(self) -> None:
        board = _board({
            "A": {"number": 1, "description": "Depends on #2"},
            "B": {"number": 2, "description": "Depends on #3"},
            "C": {"number": 3, "description": "Depends on #1"},
        })
        graph = build_graph(board)
        assert len(graph.cycles) == 1
        assert set(graph.cycles[0]) == {"A", "B", "C"}

    def test_non_cycled_dependency_of_cycled_card_excluded(self) -> None:
        """Copilot round 8: if A↔B is a cycle and A also depends on C,
        C must NOT be flagged as part of the cycle — it's just a blocker
        that A happens to need, not itself in any loop."""
        board = _board({
            "A": {"number": 1, "description": "Depends on #2. Depends on #3"},
            "B": {"number": 2, "description": "Depends on #1"},
            "C": {"number": 3, "description": ""},
        })
        graph = build_graph(board)
        assert len(graph.cycles) == 1
        cycle = set(graph.cycles[0])
        assert cycle == {"A", "B"}, f"expected {{A, B}}, got {cycle}"
        assert "C" not in cycle

    def test_downstream_of_cycle_excluded(self) -> None:
        """Copilot round 9: if A↔B is a cycle and C depends on A,
        C must NOT be flagged — it's downstream of the cycle (its
        prerequisite A can never be met) but not itself in a loop."""
        board = _board({
            "A": {"number": 1, "description": "Depends on #2"},
            "B": {"number": 2, "description": "Depends on #1"},
            "C": {"number": 3, "description": "Depends on #1"},
        })
        graph = build_graph(board)
        assert len(graph.cycles) == 1
        cycle = set(graph.cycles[0])
        assert cycle == {"A", "B"}, f"expected {{A, B}}, got {cycle}"
        assert "C" not in cycle

    def test_disjoint_cycles_reported_separately(self) -> None:
        """Copilot round 10: A↔B and C↔D are two independent cycles.
        They must be reported as two separate cycle entries, not merged
        into one misleading list."""
        board = _board({
            "A": {"number": 1, "description": "Depends on #2"},
            "B": {"number": 2, "description": "Depends on #1"},
            "C": {"number": 3, "description": "Depends on #4"},
            "D": {"number": 4, "description": "Depends on #3"},
        })
        graph = build_graph(board)
        assert len(graph.cycles) == 2
        cycle_sets = [set(c) for c in graph.cycles]
        assert {"A", "B"} in cycle_sets
        assert {"C", "D"} in cycle_sets

    def test_cycle_with_satisfied_edge_is_not_a_cycle(self) -> None:
        """If one edge in the cycle is SATISFIED (blocker in DONE),
        the cycle is broken — those nodes should NOT appear in cycles."""
        board = _board(
            {"A": {"number": 1, "description": "Depends on #2"},
             "B": {"number": 2, "description": "Depends on #1"}},
            done=["A"],
        )
        graph = build_graph(board)
        # A is in DONE → the A→B edge is SATISFIED → only B→A remains as
        # PENDING, but A is satisfied so it's not an active node in the
        # PENDING-only subgraph → no cycle.
        assert graph.cycles == []


# ---------------------------------------------------------------------------
# T010: filter_eligible_todo tests
# ---------------------------------------------------------------------------


class TestFilterEligibleTodo:
    def test_all_satisfied_pass_through(self) -> None:
        board = _board(
            {"A": {"number": 1, "description": ""},
             "B": {"number": 2, "description": "Depends on #1"}},
            done=["A"],
        )
        graph = build_graph(board)
        assert filter_eligible_todo(["B"], graph) == ["B"]

    def test_pending_filtered_out(self) -> None:
        board = _board({
            "A": {"number": 1, "description": ""},
            "B": {"number": 2, "description": "Depends on #1"},
        })
        graph = build_graph(board)
        assert filter_eligible_todo(["A", "B"], graph) == ["A"]

    def test_all_pending_empty_result(self) -> None:
        board = _board({
            "A": {"number": 1, "description": "Depends on #2"},
            "B": {"number": 2, "description": "Depends on #1"},
        })
        graph = build_graph(board)
        assert filter_eligible_todo(["A", "B"], graph) == []

    def test_no_deps_pass_through(self) -> None:
        board = _board({
            "A": {"number": 1, "description": "No deps"},
            "B": {"number": 2, "description": "Also no deps"},
        })
        graph = build_graph(board)
        assert filter_eligible_todo(["A", "B"], graph) == ["A", "B"]

    def test_mixed_deps_and_no_deps(self) -> None:
        board = _board({
            "A": {"number": 1, "description": ""},
            "B": {"number": 2, "description": "Depends on #3"},
            "C": {"number": 3, "description": ""},
        })
        graph = build_graph(board)
        result = filter_eligible_todo(["A", "B", "C"], graph)
        assert "A" in result
        assert "C" in result
        assert "B" not in result  # blocked by #3 which is still in TODO


# ---------------------------------------------------------------------------
# 091: native blocked-by relationship + comment-declared dependencies
# ---------------------------------------------------------------------------


class TestNativeBlockers:
    """NATIVE_RELATIONSHIP edges built from board["native_blockers"]."""

    def test_open_blocker_pending(self) -> None:
        board = _board({
            "A": {"number": 1, "description": ""},
            "B": {"number": 2, "description": ""},
        })
        board["native_blockers"] = {"A": [{"number": 2, "state": "OPEN", "repo": "o/r"}]}
        graph = build_graph(board)
        assert len(graph.dependencies) == 1
        dep = graph.dependencies[0]
        assert dep.source == DependencySource.NATIVE_RELATIONSHIP
        assert dep.status == DependencyStatus.PENDING

    def test_closed_blocker_satisfied(self) -> None:
        board = _board({"A": {"number": 1, "description": ""}})
        board["native_blockers"] = {"A": [{"number": 2, "state": "CLOSED", "repo": "o/r"}]}
        graph = build_graph(board)
        assert graph.dependencies[0].status == DependencyStatus.SATISFIED

    def test_open_blocker_on_board_done_satisfied(self) -> None:
        """Union semantics: a native blocker whose card sits in DONE column
        still counts as satisfied (matches 046 column logic)."""
        board = _board({"A": {"number": 1, "description": ""}})
        board["native_blockers"] = {"A": [{"number": 2, "state": "OPEN", "repo": "o/r"}]}
        # Put blocker #2 on the board in DONE directly.
        board["snapshot"]["DONE"].append("B")
        board["issue_numbers"]["B"] = 2
        graph = build_graph(board)
        assert graph.dependencies[0].status == DependencyStatus.SATISFIED

    def test_off_board_open_blocker_pending_not_unresolvable(self) -> None:
        """The native relationship proves the blocker exists and is open,
        so it's PENDING — never UNRESOLVABLE (which would park the card)."""
        board = _board({"A": {"number": 1, "description": ""}})
        board["native_blockers"] = {"A": [{"number": 999, "state": "OPEN", "repo": "o/r"}]}
        graph = build_graph(board)
        assert graph.dependencies[0].status == DependencyStatus.PENDING

    def test_self_reference_filtered(self) -> None:
        board = _board({"A": {"number": 5, "description": ""}})
        board["native_blockers"] = {"A": [{"number": 5, "state": "OPEN", "repo": "o/r"}]}
        graph = build_graph(board)
        assert graph.dependencies == []

    def test_explicit_wins_over_native(self) -> None:
        board = _board({
            "A": {"number": 1, "description": "Depends on #2"},
            "B": {"number": 2, "description": ""},
        })
        board["native_blockers"] = {"A": [{"number": 2, "state": "OPEN", "repo": "o/r"}]}
        graph = build_graph(board)
        assert len(graph.dependencies) == 1
        assert graph.dependencies[0].source == DependencySource.EXPLICIT

    def test_native_filter_integration(self) -> None:
        board = _board({
            "A": {"number": 1, "description": ""},
            "B": {"number": 2, "description": ""},
        })
        board["native_blockers"] = {"A": [{"number": 2, "state": "OPEN", "repo": "o/r"}]}
        graph = build_graph(board)
        assert filter_eligible_todo(["A", "B"], graph) == ["B"]

    def test_cross_repo_native_blocker_no_column_collision(self) -> None:
        """Copilot round 1: issue numbers are repository-local.  A foreign
        open blocker #2 must not be satisfied by an unrelated same-repo #2
        sitting in DONE."""
        board = _board({"A": {"number": 1, "description": ""}})
        board["snapshot"]["DONE"].append("B")
        board["issue_numbers"]["B"] = 2
        board["native_blockers"] = {"A": [{"number": 2, "state": "OPEN", "repo": "other/repo"}]}
        graph = build_graph(board, repo="o/r")
        assert graph.dependencies[0].status == DependencyStatus.PENDING

    def test_cross_repo_closed_blocker_satisfied(self) -> None:
        board = _board({"A": {"number": 1, "description": ""}})
        board["native_blockers"] = {"A": [{"number": 2, "state": "CLOSED", "repo": "other/repo"}]}
        graph = build_graph(board, repo="o/r")
        assert graph.dependencies[0].status == DependencyStatus.SATISFIED

    def test_same_repo_native_column_lookup_still_works(self) -> None:
        board = _board({"A": {"number": 1, "description": ""}})
        board["snapshot"]["DONE"].append("B")
        board["issue_numbers"]["B"] = 2
        board["native_blockers"] = {"A": [{"number": 2, "state": "OPEN", "repo": "o/r"}]}
        graph = build_graph(board, repo="o/r")
        assert graph.dependencies[0].status == DependencyStatus.SATISFIED

    def test_no_repo_slug_falls_back_to_number_lookup(self) -> None:
        """Without a known repo slug, by-number resolution is the legacy
        best-effort behavior (no crash, no false SATISFIED beyond it)."""
        board = _board({"A": {"number": 1, "description": ""}})
        board["snapshot"]["DONE"].append("B")
        board["issue_numbers"]["B"] = 2
        board["native_blockers"] = {"A": [{"number": 2, "state": "OPEN", "repo": "other/repo"}]}
        graph = build_graph(board)
        assert graph.dependencies[0].status == DependencyStatus.SATISFIED


class _FakeGithub:
    def __init__(
        self,
        comments: dict[int, list[dict]] | None = None,
        issue_states: dict[int, str] | None = None,
        fail_issue_numbers: set[int] | None = None,
    ) -> None:
        self._comments = comments or {}
        self._issue_states = issue_states or {}
        self._fail_issue_numbers = fail_issue_numbers or set()

    async def get_issue_comments(self, issue_number: int, since_id: int | None = None, strict: bool = False) -> list[dict]:
        if issue_number in self._fail_issue_numbers:
            raise RuntimeError(f"fetch failed for #{issue_number}")
        return self._comments.get(issue_number, [])

    async def check_issue_state(self, repo: str, issue_number: int) -> str:
        return self._issue_states.get(issue_number, "not_found")


class _LenientGithub:
    """Mirrors GitHubService's production contract: swallows errors, returns []."""

    def __init__(self, comments: dict[int, list[dict]] | None = None) -> None:
        self._comments = comments or {}

    async def get_issue_comments(self, issue_number: int, since_id: int | None = None) -> list[dict]:
        return self._comments.get(issue_number, [])


class TestCommentDependencies:
    """COMMENT-sourced edges parsed from issue comments (#91)."""

    @pytest.mark.asyncio
    async def test_comment_creates_pending_edge(self) -> None:
        board = _board({
            "A": {"number": 1, "description": ""},
            "B": {"number": 2, "description": ""},
        })
        github = _FakeGithub(comments={1: [{"id": 9, "body": "Blocked by #2"}]})
        graph = build_graph(board)
        await resolve_comment_dependencies(graph, board, github, ["A"])
        assert len(graph.dependencies) == 1
        dep = graph.dependencies[0]
        assert dep.source == DependencySource.COMMENT
        assert dep.blocker_issue_number == 2
        assert dep.status == DependencyStatus.PENDING

    @pytest.mark.asyncio
    async def test_comment_blocker_done_satisfied(self) -> None:
        board = _board(
            {"A": {"number": 1, "description": ""},
             "B": {"number": 2, "description": ""}},
            done=["B"],
        )
        github = _FakeGithub(comments={1: [{"id": 9, "body": "Depends on #2"}]})
        graph = build_graph(board)
        await resolve_comment_dependencies(graph, board, github, ["A"])
        assert graph.dependencies[0].status == DependencyStatus.SATISFIED
        assert filter_eligible_todo(["A"], graph) == ["A"]

    @pytest.mark.asyncio
    async def test_explicit_wins_over_comment(self) -> None:
        board = _board({
            "A": {"number": 1, "description": "Depends on #2"},
            "B": {"number": 2, "description": ""},
        })
        github = _FakeGithub(comments={1: [{"id": 9, "body": "Blocked by #2"}]})
        graph = build_graph(board)
        await resolve_comment_dependencies(graph, board, github, ["A"])
        assert len(graph.dependencies) == 1
        assert graph.dependencies[0].source == DependencySource.EXPLICIT

    @pytest.mark.asyncio
    async def test_comment_replaces_native(self) -> None:
        board = _board({"A": {"number": 1, "description": ""}})
        board["native_blockers"] = {"A": [{"number": 2, "state": "OPEN", "repo": "o/r"}]}
        github = _FakeGithub(comments={1: [{"id": 9, "body": "Blocked by #2"}]})
        graph = build_graph(board)
        await resolve_comment_dependencies(graph, board, github, ["A"])
        assert len(graph.dependencies) == 1
        assert graph.dependencies[0].source == DependencySource.COMMENT

    @pytest.mark.asyncio
    async def test_self_reference_filtered(self) -> None:
        board = _board({"A": {"number": 5, "description": ""}})
        github = _FakeGithub(comments={5: [{"id": 9, "body": "Blocked by #5"}]})
        graph = build_graph(board)
        await resolve_comment_dependencies(graph, board, github, ["A"])
        assert graph.dependencies == []

    @pytest.mark.asyncio
    async def test_duplicate_blockers_deduped(self) -> None:
        board = _board({"A": {"number": 1, "description": ""}})
        github = _FakeGithub(comments={1: [
            {"id": 9, "body": "Blocked by #2"},
            {"id": 10, "body": "Still blocked by #2"},
        ]})
        graph = build_graph(board)
        await resolve_comment_dependencies(graph, board, github, ["A"])
        assert len(graph.dependencies) == 1

    @pytest.mark.asyncio
    async def test_none_github_noop(self) -> None:
        board = _board({"A": {"number": 1, "description": ""}})
        graph = build_graph(board)
        await resolve_comment_dependencies(graph, board, None, ["A"])
        assert graph.dependencies == []

    @pytest.mark.asyncio
    async def test_off_board_comment_blocker_upgraded_when_closed(self) -> None:
        """Comment-declared off-board blockers go through the 046
        closed-check upgrade path (resolve_off_board_dependencies)."""
        board = _board({"A": {"number": 1, "description": ""}})
        github = _FakeGithub(
            comments={1: [{"id": 9, "body": "Blocked by #999"}]},
            issue_states={999: "closed"},
        )
        graph = build_graph(board)
        await resolve_comment_dependencies(graph, board, github, ["A"])
        assert graph.dependencies[0].status == DependencyStatus.UNRESOLVABLE
        await resolve_off_board_dependencies(graph, github, "o/r")
        assert graph.dependencies[0].status == DependencyStatus.SATISFIED

    @pytest.mark.asyncio
    async def test_comment_replace_keeps_native_pending(self) -> None:
        """Copilot round 1: replacing a native edge must not downgrade its
        authoritative PENDING (open off-board blocker) to UNRESOLVABLE."""
        board = _board({"A": {"number": 1, "description": ""}})
        board["native_blockers"] = {"A": [{"number": 999, "state": "OPEN", "repo": "o/r"}]}
        github = _FakeGithub(comments={1: [{"id": 9, "body": "Blocked by #999"}]})
        graph = build_graph(board)
        await resolve_comment_dependencies(graph, board, github, ["A"])
        assert graph.dependencies[0].status == DependencyStatus.PENDING
        assert graph.dependencies[0].source == DependencySource.COMMENT
        # resolve_off_board_dependencies must not park the card as UNRESOLVABLE.
        await resolve_off_board_dependencies(graph, github, "o/r")
        assert graph.dependencies[0].status == DependencyStatus.PENDING

    @pytest.mark.asyncio
    async def test_comment_declared_cycle_detected(self) -> None:
        """Copilot round 1: cycles must be recomputed after comment edges
        are merged, or comment-only cycles stay invisible."""
        board = _board({
            "A": {"number": 1, "description": ""},
            "B": {"number": 2, "description": ""},
        })
        github = _FakeGithub(comments={
            1: [{"id": 9, "body": "Depends on #2"}],
            2: [{"id": 10, "body": "Depends on #1"}],
        })
        graph = build_graph(board)
        assert graph.cycles == []
        await resolve_comment_dependencies(graph, board, github, ["A", "B"])
        assert len(graph.cycles) == 1
        assert set(graph.cycles[0]) == {"A", "B"}

    @pytest.mark.asyncio
    async def test_fetch_failure_marks_candidate_unknown(self) -> None:
        """Copilot round 1: unreadable comments mean unknown, not
        dependency-free — the candidate is returned for conservative skip."""
        board = _board({
            "A": {"number": 1, "description": ""},
            "B": {"number": 2, "description": ""},
        })
        github = _FakeGithub(
            comments={2: [{"id": 9, "body": "nothing here"}]},
            fail_issue_numbers={1},
        )
        graph = build_graph(board)
        unknown = await resolve_comment_dependencies(graph, board, github, ["A", "B"])
        assert unknown == {"A"}
        assert filter_eligible_todo(["A", "B"], graph) == ["A", "B"]  # no fake edges for A

    @pytest.mark.asyncio
    async def test_lenient_contract_no_unknowns_on_success(self) -> None:
        board = _board({"A": {"number": 1, "description": ""}})
        github = _LenientGithub(comments={1: [{"id": 9, "body": "Blocked by #2"}]})
        graph = build_graph(board)
        unknown = await resolve_comment_dependencies(graph, board, github, ["A"])
        assert unknown == set()
        assert len(graph.dependencies) == 1

    @pytest.mark.asyncio
    async def test_concurrent_candidates_all_processed(self) -> None:
        """Bounded concurrency must still produce edges for every candidate."""
        board = _board({
            "A": {"number": 1, "description": ""},
            "B": {"number": 2, "description": ""},
            "C": {"number": 3, "description": ""},
            "D": {"number": 4, "description": ""},
        })
        github = _FakeGithub(comments={
            1: [{"id": 9, "body": "Depends on #3"}],
            2: [{"id": 10, "body": "Blocked by #4"}],
        })
        graph = build_graph(board)
        await resolve_comment_dependencies(graph, board, github, ["A", "B", "C", "D"])
        assert len(graph.dependencies) == 2
        assert graph.by_dependent["A"][0].blocker_issue_number == 3
        assert graph.by_dependent["B"][0].blocker_issue_number == 4
