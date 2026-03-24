# Implementation Plan: Multi-Card Parallelism

**Branch**: `035-multi-card-parallelism` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Refactor the coordinare from single-card to multi-card concurrent processing. Introduce a `CardSession` abstraction for per-card state, replace flat card fields on `CoordinareState` with `active_sessions`, update `check_board` and the daemon loop for multi-session management, and extend StateStore and dashboard. **Multi-sprint effort** in three phases. ~8 modified files, ~25 new tests. No new dependencies.

## Technical Context

**Language/Version**: Python 3.12+ | **Dependencies**: LangGraph, pydantic-settings, structlog, asyncio, FastAPI (all existing)
**Storage**: StateStore JSON -- schema extended for multi-session snapshots
**Testing**: pytest + pytest-asyncio | **Scale**: ~8 modified files, ~25 new tests; **multi-sprint**

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | CardSession encapsulation; nodes stay role-agnostic |
| II. Testing Discipline | PASS | Session isolation, limits, migration each independently testable |
| III. User Experience | PASS | Dashboard gains multi-card view; single-card mode unchanged |
| IV. Performance by Design | PASS | Sequential session processing; true parallelism deferred |
| V. Clarity Before Action | PASS | No open clarifications |

## Source Code (files changed)

```text
src/coordinare/config.py               # MODIFIED -- max_concurrent_cards
src/coordinare/session.py              # NEW -- CardSession dataclass
src/coordinare/graph/state.py          # MODIFIED -- active_sessions replaces flat card fields
src/coordinare/graph/nodes/check_board.py        # MODIFIED -- multi-card pickup
src/coordinare/graph/nodes/dispatch_performer.py # MODIFIED -- session-scoped state
src/coordinare/graph/nodes/monitor_performer.py  # MODIFIED -- session-scoped state
src/coordinare/daemon.py               # MODIFIED -- per-session graph invocation
src/coordinare/state_store.py          # MODIFIED -- multi-session serialization
src/coordinare/dashboard.py            # MODIFIED -- multi-card display
src/coordinare/metrics.py              # MODIFIED -- active_sessions gauge
```

## Phased Implementation Plan

**Too large for a single pass.** Three phases, each independently mergeable. `max_concurrent_cards=1` maintains backward compatibility at every phase boundary.

### Phase 1 -- CardSession Abstraction (Sprint 1)

Introduce `CardSession` dataclass holding all per-card state (performer_stage, agent_dispatch, workspace_path, performer_events, open_questions, system_error_count, etc.). Add `active_sessions: dict[str, CardSession]` to CoordinareState. Keep flat fields as a deprecated compatibility shim that reads/writes the single session. All graph nodes receive a session view. With `max_concurrent_cards=1` hard-locked, behavior is identical to today. ~10 new tests.

### Phase 2 -- Multi-Card Pickup and Daemon Loop (Sprint 2)

Enable `max_concurrent_cards > 1`. Update `check_board` to fill sessions up to the limit (skip cards already in active sessions). Daemon loop iterates over `active_sessions`, invoking graph nodes per session with error isolation. Completed sessions are removed to free capacity. Update StateStore to serialize/restore multiple sessions with migration from old single-card format. ~10 new tests.

### Phase 3 -- Dashboard and Observability (Sprint 3)

Replace single-card dashboard panel with a session list (title, stage, elapsed, tokens per card). Add `coordinare_active_sessions` Prometheus gauge. Verify visual parity with pre-035 when `max_concurrent_cards=1`. ~5 new tests.

## Complexity Tracking

Highest complexity in the coordinare roadmap. Phased delivery mitigates risk.

| Change | Scope | Justification |
|--------|-------|---------------|
| CardSession dataclass | ~40 LOC | Clean per-card encapsulation |
| State migration | ~60 LOC | Phased to isolate risk |
| check_board multi-card | ~30 LOC | Bounded by concurrency limit; dedup by ID |
| Daemon loop refactor | ~50 LOC | Per-session error isolation |
| Dashboard multi-card | ~30 LOC | Reuses existing template patterns |
| Compatibility shim | ~20 LOC | Deprecated Phase 1; removed after Phase 2 |

**Total**: ~230 LOC new/modified | **Tests**: ~25 | **Effort**: 3 sprints
