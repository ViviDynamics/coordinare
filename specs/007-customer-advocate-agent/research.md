# Research: Customer Advocate Agent (007)

**Feature**: 007-customer-advocate-agent
**Date**: 2026-02-24
**Status**: Complete — all NEEDS CLARIFICATION resolved

---

## R1: Issue Scanning via GitHub GraphQL

**Decision**: Use GitHub GraphQL `repository.issues` query to fetch open issues (most recently created first, up to 20), then filter in-memory for those lacking the `advocate-handled` or `needs-human` labels.

**Rationale**: GitHub's `issues` GraphQL field supports `filterBy: { labels: [...] }` to find issues *with* specific labels, but there is no native GraphQL filter to *exclude* labeled issues. Fetching open issues and filtering in-memory is simple and correct. At realistic scale (≤100 open issues), the in-memory filter is O(n) and negligible in cost. The 20-issue cap per cycle satisfies SC-006.

**Alternatives considered**:
- REST API `/repos/{owner}/{repo}/issues` with label exclusion: GitHub Issues REST API also lacks an "exclude label" filter parameter; the same in-memory filtering would be required, plus a separate HTTP client. No advantage over GraphQL given the existing `gql` client.
- GitHub Search API (`is:issue is:open -label:advocate-handled`): Supports label exclusion but has tighter rate limits (30 requests/minute for authenticated users vs. 5,000 points/hour for GraphQL) and is less predictable. Not needed at this scale.

---

## R2: Documentation File Fetching

**Decision**: Use a new GitHub GraphQL query on the `repository.object(expression: "{ref}:{path}")` field to fetch file content as a `Blob.text`. Add `get_file_content(path, ref)` to `GitHubService`.

**Rationale**: The GitHub GraphQL API exposes repository file contents via the `object` field with `ref:path` expression syntax (e.g., `"main:README.md"`). This reuses the existing `gql` client with zero new dependencies. Binary files return `null` for `Blob.text`, which naturally triggers `no_documentation_match` escalation per the spec edge case (FR-003, US3-AC3). Fetching fresh on every poll cycle means document changes are picked up without a coordinare restart.

**Alternatives considered**:
- GitHub Contents REST API (`GET /repos/{owner}/{repo}/contents/{path}`): Would require a separate aiohttp session and base64 decoding. Rejected — the GraphQL approach is simpler and reuses existing infrastructure.
- Pre-loading docs once at startup: Rejected — spec requires fresh fetch per poll cycle so document changes are reflected without restart.
- Caching doc content with a TTL: Rejected as premature optimisation; at ≤5 doc files per cycle, the GraphQL cost is ~5 points, well within budget.

---

## R3: Label Application (Label-First Approach)

**Decision**: Use GitHub GraphQL `addLabelsToLabelable` mutation. Cache label node IDs at startup via `repository.labels` query. On startup, call `ensure_labels_exist()` which creates missing labels via `createLabel` mutation if needed, then caches their IDs.

**Rationale**: The label-first approach (FR-010) requires applying labels before posting comments to prevent duplicate processing across overlapping poll cycles. Using the GraphQL mutation is consistent with the existing codebase (no REST). Label IDs must be fetched/cached because the GraphQL mutation requires node IDs, not label names. The startup creation pattern matches how the board workflow caches field/option IDs (spec 001).

**Alternatives considered**:
- GitHub Labels REST API (`POST /repos/{owner}/{repo}/issues/{number}/labels`): Would need a separate HTTP client. Rejected for the same reason as R2.
- Re-fetching label IDs on every poll cycle: Label IDs don't change after creation; startup caching is correct and reduces per-cycle overhead.

---

## R4: Graph Integration — Node Placement

**Decision**: Insert `advocate_scan` as a new LangGraph node immediately after `START`, before `check_board`. The graph becomes: `START → advocate_scan → check_board → [existing routing...]`.

**Rationale**: The advocate scan runs independently on every poll cycle before the board workflow. Sequential execution (advocate first, then board) is the simplest approach. The advocate completes within the 10s budget (SC-006) using parallel `asyncio.gather` for issue classification. The board workflow is unaffected by advocate results (FR-001a: board workflow skips issues with `advocate-handled` or `needs-human` labels, which are now pre-applied by the advocate).

**Alternatives considered**:
- Parallel START fork to both `advocate_scan` and `check_board`: LangGraph's `StateGraph` requires a reducer node to fan out, adding complexity. Sequential execution is simpler and correct — the advocate's label-first writes and the board's label-read don't race because they are sequential within one poll cycle.
- Running advocate logic inside `check_board`: Violates Single Responsibility (Constitution I). Rejected.
- Separate daemon loop for advocate: Over-engineering. Spec specifies "LangGraph node within the existing coordinare graph" (FR-015). Rejected.

---

## R5: ScoringProvider Protocol Design

**Decision**: Define `ScoringProviderProtocol` as an async `Protocol` with `async def score(issue_title, issue_body, doc_content) -> ScoringResult`. `ClaudeScorer` implements it using the existing `ClaudeService`. At startup, `AdvocateService` resolves the configured `scoring_models` list to provider instances. In V1, only `"claude"` is valid.

**Rationale**: The spec requires a pluggable multi-provider scoring interface (FR-005) with the final confidence score being the mean of all configured providers. A `Protocol`-based design allows future providers (OpenAI, GitHub Copilot) to be added by: (1) implementing the Protocol in a new file, (2) adding the provider name to `scoring_models` in config. No changes to the advocate node or existing providers are required.

**Alternatives considered**:
- Abstract base class (ABC): Heavier than Protocol for this use case. Protocol enables structural subtyping without inheritance coupling. Rejected.
- Single scoring function with `if provider == "claude"` dispatch: Less extensible; harder to test individual providers in isolation. Rejected.

---

## R6: Classification and Response Generation — Single vs. Multi-Call

**Decision**: In V1 (single `scoring_models: ["claude"]`), issue classification, response generation, and confidence scoring are combined in a single Claude API call per issue, returning a structured JSON payload: `{"classification", "confidence", "reasoning", "response_text", "source_documents"}`. This reduces API calls and latency.

**Rationale**: Per the spec assumption: "In V1, with `scoring_models: ["claude"]`, issue classification and response generation are handled in a single Claude API call per issue to minimise latency." For future multi-provider scoring, the response text would be generated by the primary provider (Claude) and confidence would be scored by each configured provider independently.

**Alternatives considered**:
- Separate classification call then generation call: Doubles API latency in V1. Rejected for V1; acceptable in future multi-provider mode where classification must precede scoring by other providers.
- Streaming responses: Not needed; response text is short (≤500 tokens for issue comments). Adds complexity with no benefit. Rejected.

---

## R7: In-Memory Response History

**Decision**: `AdvocateService` maintains a `set[str]` of processed issue IDs (`_processed_ids`) as instance state. This is the secondary guard against duplicate posts (primary guard is the GitHub label, per FR-010a).

**Rationale**: Simple, fast, and sufficient. The set is bounded by the number of open issues (≤100 at realistic scale). It resets on coordinare restart (acceptable per spec assumption: cross-session persistence is spec 003's responsibility).

**Alternatives considered**:
- SQLite in-memory database: Over-engineering for a set of IDs. Rejected.
- Redis or external cache: External dependency; out of scope. Rejected.

---

## R8: Sensitive Keyword Matching

**Decision**: Case-insensitive substring match — `any(kw.lower() in (title + " " + body).lower() for kw in keywords)` — applied before the Claude API call to short-circuit classification for sensitive issues.

**Rationale**: The spec specifies case-insensitive substring match (spec assumption). Short-circuiting on keyword match before calling Claude reduces API costs and latency for sensitive issues. The keyword match applies to issue title and body only (not documentation content), per the spec edge case.

**Alternatives considered**:
- Regex matching: More powerful but unnecessary for simple substring matching. Adds complexity and potential for misconfiguration. Rejected.
- NLP sentiment analysis: Out of scope per spec assumption. Rejected.

---

## R9: Concurrency Model for Parallel Issue Processing

**Decision**: Use `asyncio.gather` with a `asyncio.Semaphore(5)` to process up to 5 issues concurrently within each advocate scan cycle. Each issue's Claude API call runs asynchronously; label application and comment posting follow within the same issue coroutine.

**Rationale**: SC-006 requires ≤10s for 20 issues. Sequential processing at ~0.5s/call = 10s worst case (barely meets budget). With 5-concurrency: ~2s for 20 issues. The semaphore prevents overwhelming the Claude API or triggering rate limits while still meeting the performance budget. The `asyncio.gather(return_exceptions=True)` pattern ensures one failing issue doesn't abort the batch.

**Alternatives considered**:
- Fully sequential processing: Barely meets SC-006 in best case; fails under API latency variance. Rejected.
- Unbounded concurrency: Risk of hitting Claude API rate limits with a large issue backlog. Rejected.
- `asyncio.TaskGroup` (Python 3.11+): Equivalent to `gather` but cancels remaining tasks on first failure. The `return_exceptions=True` pattern is safer for batch processing where individual failures should not abort the cycle. Rejected.
