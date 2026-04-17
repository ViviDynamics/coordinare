"""Unit tests for the dependency detection service (046)."""
from __future__ import annotations

from coordinare.models.dependency import (
    DependencySource,
    DependencyStatus,
)
from coordinare.services.dependency import (
    build_graph,
    filter_eligible_todo,
    parse_dependencies,
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
