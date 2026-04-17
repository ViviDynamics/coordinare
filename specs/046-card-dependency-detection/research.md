# Research: Card Dependency Detection

## R1: Dependency Syntax Parsing Strategy

**Decision**: Regex-based parser applied to card description text, matching "Depends on #N", "Blocked by #N", "After #N", "Requires #N" (case-insensitive). Returns a list of issue numbers.

**Rationale**: Card descriptions are already fetched by `poll_board()` and stored in `board["descriptions"][item_id]`. Parsing is a pure string operation with no API cost. Regex is sufficient for the structured patterns; no NLP needed for explicit dependencies.

**Alternatives considered**:
- GitHub issue linking API (closedByPullRequestsReferences): Only tracks PR→issue "closes" relationships, not arbitrary card→card dependencies. Wrong semantic — we need "blocked by", not "closes".
- GitHub Project V2 custom fields: Would require schema changes on the GitHub side and expanding the GraphQL query to fetch custom field values. Heavier setup for the user; parsing from description is zero-config.
- Label-based dependencies (e.g., `blocked-by:90`): Requires labels to be managed per-dependency. Labels don't support parameterised values cleanly.

## R2: Dependency Graph Construction & Cycle Detection

**Decision**: Build a directed graph (adjacency list) from the parsed dependencies on each poll cycle. Run Kahn's algorithm (topological sort) to detect cycles. If the topological sort doesn't consume all nodes, the remaining nodes form one or more cycles.

**Rationale**: The graph is small (≤50 nodes, ≤100 edges) and rebuilt from scratch each cycle. Kahn's is O(V+E), trivially fast at this scale, and naturally produces the cycle membership set (nodes with remaining in-degree > 0 after the sort).

**Alternatives considered**:
- DFS-based cycle detection (Tarjan's): Also O(V+E) but more complex to implement. Kahn's is simpler for "are there cycles?" without needing the strongly-connected-component decomposition.
- Persistent graph with incremental updates: Unnecessary complexity. Rebuilding from scratch on each 30s cycle is cheaper than maintaining consistency under concurrent board edits.

## R3: Reverse Issue-Number Lookup

**Decision**: On each poll cycle, build a reverse map `{issue_number: (item_id, column)}` by inverting `board["issue_numbers"]` and scanning `board["snapshot"]` columns. Used to resolve "Depends on #90" to a board column status.

**Rationale**: The forward maps (`item_id → issue_number`, `column → [item_ids]`) are already in memory from `poll_board()`. Building the reverse is O(N) where N is the number of board items — trivial.

**Alternatives considered**:
- Extra GitHub API call per dependency (get issue state): Wastes API quota. The board already has the information.
- Caching the reverse map across cycles: Adds stale-data risk. Rebuilding is cheap enough to do fresh.

## R4: Assessor Implicit Dependency Detection

**Decision**: Inject a list of active card titles (TODO, IN_PROGRESS, IN_REVIEW) into the assessor's context alongside the card being evaluated. The assessor persona instructions are extended with a short directive: "If this card's work logically requires another active card to complete first, flag it as a dependency." The assessor's JSON output gains an optional `dependencies` field (list of issue numbers).

**Rationale**: The assessor already receives the card's full details and makes a sufficiency judgment. Adding active-card context is a small prompt extension. The Claude backend is well-suited to semantic comparison of card titles.

**Alternatives considered**:
- Embedding-based similarity search: Over-engineered for ≤50 cards. Prompt-based comparison is simpler and more interpretable.
- Separate "dependency detector" role: Adds a new lifecycle stage. The assessor already gates card readiness — dependency detection is a natural extension of that role.

## R5: Dashboard Rendering Strategy

**Decision**: Add a `blocked_by_dependencies` field to the SSE dashboard snapshot payload. The field is a list of `{issue_number, title, column, issue_url}` dicts. The dashboard JS renders these as a badge/pill list under the blocked card with clickable links.

**Rationale**: Follows the existing pattern for extending the dashboard (add field to snapshot dict → render in JS). No new endpoints needed — SSE pushes the data.

**Alternatives considered**:
- Separate REST endpoint for dependency data: Adds complexity. The SSE snapshot already carries all card state.
- WebSocket for real-time dependency graph visualisation: Scope creep for this spec. A simple list of blockers is sufficient.

## R6: Handling Closed/Missing Issues

**Decision**: When parsing "Depends on #N":
1. If #N maps to a board item in the DONE column → satisfied (skip).
2. If #N is not on the board, make a lightweight GitHub API call (`GET /repos/{owner}/{repo}/issues/{N}`) to check if the issue is closed. If closed → satisfied. If open but not on the board → unresolvable (block the card).
3. If the API call fails or returns 404 → unresolvable (block the card with diagnostic).

**Rationale**: Most dependencies reference cards on the same board (zero API cost). The API fallback only fires for off-board references, which should be rare. One REST call per off-board dependency per cycle is acceptable.

**Alternatives considered**:
- Treat all off-board references as satisfied: Dangerous — a legitimate dependency on an open issue not yet on the board would be silently skipped.
- Treat all off-board references as unresolvable: Too aggressive — an issue that was completed and removed from the board would permanently block dependents.
