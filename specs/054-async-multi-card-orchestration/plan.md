# Implementation Plan: Async Multi-Card Orchestration Eligibility

**Branch**: `054-async-multi-card-orchestration` | **Date**: 2026-04-24 | **Spec**: [spec.md](spec.md)  
**Input**: Feature specification from `/specs/054-async-multi-card-orchestration/spec.md`

## Summary

Coordinare currently processes multi-card sessions sequentially in `_invoke_multi_session` (daemon.py:333) — all active sessions in a `for` loop with one `ainvoke` per session before the next starts. This feature makes session invocation truly concurrent within a cycle by:

1. Deriving per-session eligibility from board column status and dependency graph state before dispatch.
2. Fanning out only eligible sessions concurrently with `asyncio.gather` + per-session failure isolation.
3. Recording `session_skip_reasons` in state/snapshot for operator observability.
4. Ensuring post-merge rebase dispatch (already in check_board.py) targets eligible open-PR sessions correctly.
5. Extending the QA performer contract with an explicit branch-freshness check against latest `main` SHA.

No new external dependencies are required. All changes are additive to existing state fields or behavioral changes to `_invoke_multi_session` and the QA contract.

## Technical Context

**Language/Version**: Python 3.12+  
**Primary Dependencies**: `asyncio` (stdlib), `structlog` (existing), `pydantic` (existing), `LangGraph ≥ 0.2` (existing) — no new dependencies  
**Storage**: In-memory only; `session_skip_reasons` dict added to `CoordinareState`; resets each cycle  
**Testing**: `pytest` + `pytest-asyncio`; existing test fixtures in `tests/unit/test_daemon_coverage.py`, `tests/unit/graph/nodes/test_check_board.py`, `tests/unit/graph/nodes/test_monitor_performer.py`, `agent/performer/tests/unit/test_main.py`  
**Target Platform**: Linux daemon process (single-node, in-process)  
**Performance Goals**: All eligible sessions in a given cycle MUST be invoked concurrently (no sequential-only fallback when `max_concurrent_cards > 1`). Overhead from eligibility pre-filter must be negligible (O(n) over active sessions).  
**Constraints**:
- `max_concurrent_cards=1` path MUST remain behaviorally identical to current behavior.
- Failure isolation: one session crash MUST NOT cancel or delay other in-flight tasks.
- Board cache (`_board_cache`) and dependency graph MUST be populated before eligibility computation; no extra GitHub API calls per-session.
- Async fanout scoped to a single daemon process — no distributed worker redesign.

**Scale/Scope**: Targeted at ≤ 20 concurrent active sessions (bounded by `max_concurrent_cards ≤ 20`, config.py:478).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Status |
|-----------|------|--------|
| I. Code Quality | All new code must be readable, typed, single-responsibility, lint-clean | ✅ No violations — changes are localized to `daemon.py`, `graph/state.py`, `dashboard.py`, `check_board.py`, and QA performer |
| II. Testing Discipline | Unit/integration tests for eligibility, fanout, skip reasons, freshness gate, rebase dispatch | ✅ Full test coverage required per tasks.md |
| III. UX Consistency | Skip reasons exposed in dashboard snapshot following existing snapshot patterns | ✅ Additive dashboard change; follows existing observability patterns |
| IV. Performance by Design | Concurrent session invocation must not regress throughput; eligibility filter is O(n) | ✅ `asyncio.gather` provides true parallelism; filter adds negligible overhead |
| V. Clarity Before Action | No `NEEDS CLARIFICATION` tags remain; all research complete | ✅ All unknowns resolved in research.md |

**Post-design re-check**: ✅ data-model.md and contracts/ only add fields; no breaking schema changes. Constitution gates all pass.

## Project Structure

### Documentation (this feature)

```text
specs/054-async-multi-card-orchestration/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
│   ├── session-eligibility-state.md
│   └── qa-freshness-contract.md
└── tasks.md             # Phase 2 output (/speckit.tasks command)
```

### Source Code (repository root)

```text
src/coordinare/
├── daemon.py                         # _invoke_multi_session() refactor: eligibility filter + asyncio.gather fanout
├── graph/
│   ├── state.py                      # Add session_skip_reasons: dict[str, dict] to CoordinareState
│   └── nodes/
│       └── check_board.py            # Rebase dispatch: ensure eligible open-PR sessions targeted correctly
├── services/
│   └── dependency.py                 # No changes — dependency graph already provides resolution status
└── dashboard.py                      # Expose session_skip_reasons in snapshot build path

agent/performer/src/performer/
└── main.py                           # QA role: add branch freshness check (git merge-base vs main SHA)

tests/unit/
├── test_daemon_coverage.py           # Eligibility, fanout, failure isolation, skip reasons
├── graph/nodes/
│   ├── test_check_board.py           # Rebase dispatch targeting, no-op when no open PRs
│   └── test_monitor_performer.py     # QA freshness failure path; freshness feeds remediation loop
└── test_dashboard.py                 # Skip reason payload in snapshot

agent/performer/tests/unit/
└── test_main.py                      # QA freshness: behind-main fail, up-to-date pass, indeterminate blocker
```

**Structure Decision**: Single-project layout (Option 1). All coordinare changes stay in `src/coordinare/`; QA performer changes in `agent/performer/src/performer/`. No new packages or modules required.

## Phase 0: Research

Research is complete. See [research.md](research.md).

**Key decisions**:
- **R1**: `asyncio.gather` for session fanout (true in-cycle concurrency, in-process, testable)
- **R2**: Eligibility derived from existing board snapshot + dependency graph — no duplicate rule engines
- **R3**: Skip reasons stored as `session_skip_reasons: dict[str, dict]` in `CoordinareState`, cleared each cycle
- **R4**: Per-session failure isolation via exception collection; successful results always merged
- **R5**: `max_concurrent_cards=1` path unchanged
- **R6**: Post-merge rebase dispatch already in `check_board.py` (lines 116–182); this feature ensures only eligible open-PR sessions are targeted
- **R7**: QA freshness check extends performer output contract with `qa_freshness_check` field

## Phase 1: Design

Design is complete. See [data-model.md](data-model.md), [quickstart.md](quickstart.md), [contracts/](contracts/).

### Implementation Details

#### 1. Session Eligibility Derivation (daemon.py)

**Where**: `_invoke_multi_session()` at daemon.py:299, before the session loop.

**Logic**:
```python
def _compute_eligibility(
    card_id: str,
    session: dict,
    board_snapshot: dict[str, list[str]],
    dep_graph: DependencyGraph | None,
) -> SessionEligibility:
    card = session.get("current_card") or {}
    card_id_from_session = card.get("content_id") or card.get("id")
    if not card:
        return SessionEligibility(card_id=card_id, eligible=False, reason="missing_card")
    # Check BLOCKED column
    blocked_cards = board_snapshot.get("BLOCKED", [])
    if card_id_from_session in blocked_cards:
        return SessionEligibility(card_id=card_id, eligible=False, reason="blocked_column")
    # Check dependency blockers
    if dep_graph is not None:
        deps = dep_graph.by_dependent.get(card_id_from_session, [])
        unresolved = [d for d in deps if d.status != "SATISFIED"]
        if unresolved:
            return SessionEligibility(
                card_id=card_id, eligible=False, reason="dependency_blocked",
                blockers=[d.blocker_issue_number for d in unresolved]
            )
    return SessionEligibility(card_id=card_id, eligible=True, reason="eligible")
```

Eligibility is computed using the cached board snapshot (`state["_board_cache"]` / `state["board_snapshot"]`) and dependency graph already built each cycle by `check_board`.

#### 2. Async Fanout (daemon.py)

**Where**: Replace sequential `for` loop in `_invoke_multi_session()` with `asyncio.gather`.

```python
async def _invoke_one_session(card_id, session, eligibilities) -> AsyncSessionTickResult:
    elig = eligibilities[card_id]
    if not elig.eligible:
        return AsyncSessionTickResult(card_id=card_id, ok=True, session_state=session, skipped=True)
    pre_session = dict(session)
    session_to_state(session, self._state)
    t0 = perf_counter()
    try:
        updated_state = await self._graph.ainvoke(self._state)
        updated_session = state_to_session(updated_state)
        return AsyncSessionTickResult(
            card_id=card_id, ok=True,
            session_state=updated_session,
            duration_ms=int((perf_counter() - t0) * 1000),
        )
    except Exception as exc:
        return AsyncSessionTickResult(
            card_id=card_id, ok=False, error=str(exc),
            session_state=pre_session,
            duration_ms=int((perf_counter() - t0) * 1000),
        )

# In _invoke_multi_session:
results = await asyncio.gather(*[
    _invoke_one_session(cid, sess, eligibilities)
    for cid, sess in list(active_sessions.items())
])
# Merge results; collect skip reasons
```

**Note on shared state**: Each `_invoke_one_session` call must operate on an isolated state copy to avoid cross-session mutations. `session_to_state` / `state_to_session` already isolates session-scoped fields; the flat `self._state` non-session fields (board_snapshot, config, etc.) are read-only within a cycle.

#### 3. `session_skip_reasons` State Field (graph/state.py)

Add to `CoordinareState`:
```python
session_skip_reasons: dict[str, dict]  # card_id -> {reason, detail, blockers}
```

Default: `{}`. Populated by `_invoke_multi_session` after eligibility computation; cleared at cycle start.

Dashboard snapshot exposes this map for operator visibility.

#### 4. Post-Merge Rebase Dispatch (check_board.py)

The existing `run_rebase_round()` call at check_board.py:164 already dispatches for `active_sessions`. The feature requirement (FR-010, FR-011) is that this is failure-isolated per branch and only targets **eligible open-PR sessions**. 

Review needed:
- Confirm `run_rebase_round` skips sessions in `phase="monitoring_performer"` (performer actively using branch) — already gated at `rebase.py:140`.
- Confirm sessions in BLOCKED column or dependency-blocked are also excluded from rebase dispatch (these may have stale branches — rebasing them is low-priority but not harmful; current behavior already skips based on phase).
- Add test for "no eligible open-PR sessions → no rebase dispatch" (SC-006, US3 scenario 3).

No code change required if existing rebase logic correctly excludes non-open-PR sessions; test gap is the primary deliverable for T015-T017.

#### 5. QA Freshness Check (agent/performer/src/performer/main.py)

**Contract extension** (additive to `PerformerStatusResponse.report`):

The QA role must include `qa_freshness_check` in its `status=qa_passed/qa_failed` response report:
```json
{
  "qa_freshness_check": {
    "latest_main_sha": "<sha coordinare passed>",
    "branch_head_sha": "<current HEAD>",
    "up_to_date": true,
    "detail": "branch includes latest main"
  }
}
```

**Implementation**: Coordinare passes `latest_main_sha` from `state["last_known_main_sha"]` to the QA performer dispatch payload. The QA performer runs `git merge-base --is-ancestor <latest_main_sha> HEAD` to determine freshness. If the check fails, QA must report `status=qa_failed` with a freshness-specific failure entry.

**Handling in coordinare** (monitor_performer.py): When QA reports `qa_failed` with a freshness failure, the existing feedback-cycle loop handles remediation. If branch freshness cannot be determined (API/git error), QA reports `status=qa_failed` with `detail="freshness_check_indeterminate"`.

### State Snapshot Impact

`session_skip_reasons` map added to the state snapshot output in `dashboard.py`. No existing fields modified. Dashboard renders skip reason per active session card in the active performers view.

## Complexity Tracking

No constitution violations. All changes are additive or localized behavioral changes to existing functions.
