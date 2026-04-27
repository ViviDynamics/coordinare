# Research: Async Multi-Card Orchestration Eligibility

## R1: Async fanout primitive for session invocation

**Decision**: Use `asyncio.TaskGroup` (or `asyncio.gather` fallback) to invoke eligible sessions concurrently within `_invoke_multi_session`.

**Rationale**: Provides true in-cycle concurrency while keeping orchestration in-process and testable.

**Alternatives considered**:
- Keep sequential loop: rejected; does not meet feature intent.
- Spawn external worker process per session: rejected; out of scope for this feature.

## R2: Eligibility source of truth

**Decision**: Derive eligibility from board snapshot columns + dependency graph resolution from existing dependency services.

**Rationale**: Avoids duplicate rule engines and keeps scheduling aligned with established dependency semantics.

**Alternatives considered**:
- Add ad-hoc eligibility flags on sessions: rejected; risks stale state and drift.

## R3: Skip reason model

**Decision**: Store per-cycle skip reasons as a lightweight map keyed by card ID and expose it in dashboard snapshot.

**Rationale**: Gives immediate observability without new persistence complexity.

**Alternatives considered**:
- Log-only diagnostics: rejected; not visible in dashboard.

## R4: Failure isolation strategy

**Decision**: Treat each async session tick as independent; collect exceptions per session and continue merging successful results.

**Rationale**: A single flaky card should not throttle all other eligible cards.

**Alternatives considered**:
- Fail-fast whole cycle: rejected; harms throughput and resilience.

## R5: Compatibility strategy

**Decision**: Keep explicit single-card behavior path unchanged when `max_concurrent_cards=1`.

**Rationale**: Avoid regressions for teams not using parallel mode.

## R6: Rebase trigger model for merged PRs

**Decision**: On detected `main` SHA advancement, trigger a rebase round for active sessions that have open PR branches and are not currently in BLOCKED/dependency-blocked ineligibility.

**Rationale**: Keeps in-flight branches current after squash/merge and prevents stale QA/review outcomes.

## R7: QA main-freshness gate

**Decision**: Extend QA output contract with an explicit branch-freshness check result tied to latest known `main` SHA.

**Rationale**: Prevents QA pass on outdated branches and guarantees in-flight work after merges is validated against newest base.
