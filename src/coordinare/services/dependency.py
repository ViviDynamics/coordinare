"""Card dependency detection service (046).

Parses explicit dependency syntax from card descriptions, builds a
dependency graph from the board snapshot, detects cycles via Kahn's
algorithm, and filters the TODO queue to exclude cards whose blockers
haven't reached DONE.

Stateless — the graph is rebuilt from scratch on every poll cycle.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any

import structlog

from coordinare.models.dependency import (
    CardDependency,
    DependencyGraph,
    DependencySource,
    DependencyStatus,
)

logger = structlog.get_logger(__name__)

# Matches "Depends on #N", "Blocked by #N", "After #N", "Requires #N"
# Case-insensitive, captures the issue number.
_DEP_PATTERN = re.compile(
    r"(?:depends\s+on|blocked\s+by|after|requires)\s+#(\d+)",
    re.IGNORECASE,
)


def parse_dependencies(description: str) -> list[int]:
    """Extract explicit dependency issue numbers from a card description.

    Supports: "Depends on #N", "Blocked by #N", "After #N", "Requires #N"
    (case-insensitive).  Returns a deduplicated list of positive integers.
    """
    if not description:
        return []
    matches = _DEP_PATTERN.findall(description)
    seen: set[int] = set()
    result: list[int] = []
    for m in matches:
        num = int(m)
        if num > 0 and num not in seen:
            seen.add(num)
            result.append(num)
    return result


def build_reverse_lookup(
    board: dict[str, Any],
) -> tuple[dict[int, str], dict[int, str]]:
    """Build reverse maps from the board snapshot.

    Returns:
        (issue_to_item, issue_to_column) where:
        - issue_to_item: {issue_number → item_id}
        - issue_to_column: {issue_number → column_name}
    """
    issue_numbers: dict[str, int] = board.get("issue_numbers", {})
    snapshot: dict[str, list[str]] = board.get("snapshot", {})

    # item_id → issue_number (forward) → invert
    issue_to_item: dict[int, str] = {}
    for item_id, num in issue_numbers.items():
        if num > 0:
            issue_to_item[num] = item_id

    # item_id → column (scan all columns)
    item_to_column: dict[str, str] = {}
    for column, items in snapshot.items():
        for item_id in items:
            item_to_column[item_id] = column

    # issue_number → column
    issue_to_column: dict[int, str] = {}
    for num, item_id in issue_to_item.items():
        col = item_to_column.get(item_id)
        if col:
            issue_to_column[num] = col

    return issue_to_item, issue_to_column


def _resolve_status(
    blocker_issue_number: int,
    issue_to_column: dict[int, str],
) -> DependencyStatus:
    """Determine the satisfaction status of a single dependency."""
    column = issue_to_column.get(blocker_issue_number)
    if column is None:
        return DependencyStatus.UNRESOLVABLE
    if column == "DONE":
        return DependencyStatus.SATISFIED
    return DependencyStatus.PENDING


def build_graph(
    board: dict[str, Any],
    *,
    self_issue_numbers: dict[str, int] | None = None,
) -> DependencyGraph:
    """Build the dependency graph from the current board snapshot.

    Parses every card's description for explicit dependency syntax and
    resolves each dependency's status against the board columns.

    ``self_issue_numbers`` is the ``board["issue_numbers"]`` map (item_id →
    issue_number).  If not provided, it's read from ``board``.
    """
    descriptions: dict[str, str] = board.get("descriptions", {})
    if self_issue_numbers is None:
        self_issue_numbers = board.get("issue_numbers", {})

    issue_to_item, issue_to_column = build_reverse_lookup(board)

    graph = DependencyGraph(
        issue_to_item=issue_to_item,
        issue_to_column=issue_to_column,
    )

    for item_id, desc in descriptions.items():
        own_issue = self_issue_numbers.get(item_id, 0)
        deps = parse_dependencies(desc)
        for blocker_num in deps:
            # Filter self-references
            if blocker_num == own_issue:
                continue
            status = _resolve_status(blocker_num, issue_to_column)
            dep = CardDependency(
                dependent_item_id=item_id,
                blocker_issue_number=blocker_num,
                source=DependencySource.EXPLICIT,
                status=status,
            )
            graph.dependencies.append(dep)
            graph.by_dependent.setdefault(item_id, []).append(dep)
            graph.by_blocker.setdefault(blocker_num, []).append(dep)

    graph.cycles = detect_cycles(graph, issue_to_item)
    return graph


def detect_cycles(
    graph: DependencyGraph,
    issue_to_item: dict[int, str],
) -> list[list[str]]:
    """Detect circular dependencies using Kahn's algorithm.

    Returns a list of cycles.  Each cycle is a list of item_ids that
    form a dependency loop.  Empty list if no cycles exist.
    """
    # Build adjacency list: item_id → set of item_ids it depends on
    # (only for PENDING dependencies — SATISFIED ones don't form active edges)
    #
    # Edge direction for Kahn's: blocker → dependent ("blocker enables
    # dependent").  in_degree[X] counts how many PENDING prerequisites X
    # has.  Nodes with in_degree 0 can proceed (nothing blocks them).
    # After processing a node, decrement in_degree for everything it
    # enables.  Nodes left unprocessed are in cycles.
    #
    # Previous version had edges in the wrong direction (dependent →
    # blocker), which caused nodes that were merely depended-on by a
    # cycled card (but not themselves in the cycle) to be incorrectly
    # flagged — e.g. if A↔B is a cycle and A also depends on C, C
    # would be stuck with nonzero in_degree despite having no deps.
    adj: dict[str, set[str]] = defaultdict(set)   # blocker → dependents it enables
    in_degree: dict[str, int] = defaultdict(int)   # count of pending prerequisites
    all_nodes: set[str] = set()

    for dep in graph.dependencies:
        if dep.status != DependencyStatus.PENDING:
            continue
        blocker_item = issue_to_item.get(dep.blocker_issue_number)
        if blocker_item is None:
            continue  # off-board — can't form a cycle
        dependent = dep.dependent_item_id
        all_nodes.add(dependent)
        all_nodes.add(blocker_item)
        if dependent not in adj[blocker_item]:
            adj[blocker_item].add(dependent)
            in_degree[dependent] = in_degree.get(dependent, 0) + 1
            in_degree.setdefault(blocker_item, 0)  # ensure blocker exists

    # Kahn's: start with nodes that have in-degree 0 (no pending prereqs)
    queue: deque[str] = deque(
        node for node in all_nodes if in_degree.get(node, 0) == 0
    )
    visited: set[str] = set()
    while queue:
        node = queue.popleft()
        visited.add(node)
        for neighbour in adj.get(node, set()):
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    # Nodes not visited are stuck — either in a cycle or downstream of one.
    # Filter to only true cycle members: a node is in a cycle if it has
    # both outgoing AND incoming edges within the stuck set.  Nodes that
    # merely depend on a cycled node (downstream) have incoming edges
    # from the cycle but no outgoing edges back into it.
    stuck = all_nodes - visited
    if not stuck:
        return []

    # Build reverse adjacency (dependent → blockers) within the stuck set
    # to check for outgoing dependency edges.
    has_outgoing_in_stuck: set[str] = set()
    has_incoming_in_stuck: set[str] = set()
    for node in stuck:
        for neighbour in adj.get(node, set()):
            if neighbour in stuck:
                has_outgoing_in_stuck.add(node)   # node enables neighbour
                has_incoming_in_stuck.add(neighbour)  # neighbour has prereq from node
    cycle_members = has_outgoing_in_stuck & has_incoming_in_stuck
    if not cycle_members:
        return []

    # Decompose into distinct cycles via connected components within
    # cycle_members (so disjoint cycles like A↔B and C↔D are reported
    # separately, not merged into one misleading list).
    remaining = set(cycle_members)
    components: list[list[str]] = []
    while remaining:
        start = next(iter(remaining))
        component: set[str] = set()
        bfs: deque[str] = deque([start])
        while bfs:
            node = bfs.popleft()
            if node not in remaining:
                continue
            remaining.discard(node)
            component.add(node)
            for neighbour in adj.get(node, set()):
                if neighbour in remaining:
                    bfs.append(neighbour)
        components.append(sorted(component))

    return components


async def resolve_off_board_dependencies(
    graph: DependencyGraph,
    github: Any,
    repo: str,
) -> None:
    """Resolve UNRESOLVABLE dependencies by checking if the blocker issue is
    closed on GitHub.

    Mutates ``graph.dependencies`` in place: UNRESOLVABLE deps whose referenced
    issue is closed are upgraded to SATISFIED.  This handles the common case
    where a blocker card was completed and removed from the project board but
    the dependent card still references it.

    Requires an async ``github.check_issue_state(repo, issue_number)`` method.
    Skips resolution if ``github`` is None or ``repo`` is empty.
    """
    if github is None or not repo:
        return
    check = getattr(github, "check_issue_state", None)
    if check is None:
        return
    # Collect unique off-board issue numbers to avoid duplicate API calls.
    off_board: set[int] = set()
    for dep in graph.dependencies:
        if dep.status == DependencyStatus.UNRESOLVABLE:
            off_board.add(dep.blocker_issue_number)
    if not off_board:
        return
    # Check each off-board issue (one REST call per unique issue number).
    resolved: dict[int, DependencyStatus] = {}
    for num in off_board:
        state = await check(repo, num)
        if state == "closed":
            resolved[num] = DependencyStatus.SATISFIED
            logger.info("dependency.off_board_check", issue=num, state="closed", satisfied=True)
        else:
            logger.info("dependency.off_board_check", issue=num, state=state, satisfied=False)
    if not resolved:
        return
    # Mutate: replace UNRESOLVABLE deps with SATISFIED where applicable.
    # CardDependency is frozen, so rebuild the list.
    new_deps: list[CardDependency] = []
    for dep in graph.dependencies:
        if dep.status == DependencyStatus.UNRESOLVABLE and dep.blocker_issue_number in resolved:
            dep = CardDependency(
                dependent_item_id=dep.dependent_item_id,
                blocker_issue_number=dep.blocker_issue_number,
                source=dep.source,
                status=resolved[dep.blocker_issue_number],
            )
        new_deps.append(dep)
    graph.dependencies = new_deps
    # Rebuild both indexes so all consumers see the updated statuses.
    graph.by_dependent.clear()
    graph.by_blocker.clear()
    for dep in graph.dependencies:
        graph.by_dependent.setdefault(dep.dependent_item_id, []).append(dep)
        graph.by_blocker.setdefault(dep.blocker_issue_number, []).append(dep)


def filter_eligible_todo(
    eligible: list[str],
    graph: DependencyGraph,
) -> list[str]:
    """Remove items whose dependencies are not all SATISFIED.

    Items with no dependencies pass through unchanged.
    """
    result: list[str] = []
    for item_id in eligible:
        deps = graph.by_dependent.get(item_id, [])
        if not deps:
            result.append(item_id)
            continue
        if all(d.status == DependencyStatus.SATISFIED for d in deps):
            result.append(item_id)
    return result
