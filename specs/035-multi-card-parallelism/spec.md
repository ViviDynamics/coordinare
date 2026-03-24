# Feature Specification: Multi-Card Parallelism

**Feature Branch**: `035-multi-card-parallelism`
**Created**: 2026-03-24
**Status**: Draft

## Overview

The coordinare currently processes one card at a time: `check_board` picks up a single TODO card, and the entire daemon state machine tracks that single card through dispatch, monitoring, review, and merge. This feature introduces configurable multi-card concurrency, allowing the coordinare to work on up to `max_concurrent_cards` cards simultaneously. Each card runs in an independent `CardSession` with its own performer stage, dispatch state, and lifecycle progression. The graph and daemon loop are refactored to manage a pool of active sessions rather than a single global card state.

**This is the largest and most complex feature in the coordinare roadmap. It is flagged as a multi-sprint effort requiring a phased implementation approach.**

## Clarifications

### Session 2026-03-24

- Q: Does each card get its own graph invocation? -> A: Yes. Each CardSession holds its own state slice and the graph is invoked per-session. The daemon loop iterates over active sessions.
- Q: Do cards share performer services? -> A: Yes. The `performer_services` registry and `lifecycle_sequence` are global (shared across sessions). Individual sessions reference them but do not own them.
- Q: How does `check_board` change? -> A: It returns multiple cards when under the concurrency limit. Each new card gets its own CardSession. Cards already in active sessions are skipped.
- Q: What about state persistence? -> A: StateStore must serialize/restore multiple active sessions. The existing single-card snapshot format must be migrated.
- Q: Is `max_concurrent_cards=1` identical to today's behavior? -> A: Yes, by design. The default is 1 for full backward compatibility.

## User Scenarios & Testing *(mandatory)*

### User Story 1 -- Configurable Concurrency Limit (Priority: P1)

Operators can set `max_concurrent_cards` in `config.yaml` (default 1). When set to N > 1, the coordinare picks up and processes up to N cards simultaneously. Each card progresses independently through the performer lifecycle.

**Why this priority**: The concurrency limit is the gating configuration. Without it, no parallelism is possible.

**Independent Test**: Configure `max_concurrent_cards=2`, place 3 cards in TODO. Verify that 2 cards are dispatched concurrently and the 3rd waits until one finishes.

**Acceptance Scenarios**:

1. **Given** `max_concurrent_cards=2` and 3 cards in TODO, **When** `check_board` runs, **Then** 2 cards are picked up and dispatched, the 3rd remains in TODO.
2. **Given** `max_concurrent_cards=1` (default), **When** `check_board` runs with 2 cards in TODO, **Then** only 1 card is dispatched.
3. **Given** `max_concurrent_cards=3` and 1 card in TODO, **When** `check_board` runs, **Then** 1 card is dispatched (no error for under-capacity).

---

### User Story 2 -- Independent Card Lifecycle (Priority: P1)

Each active card has its own `CardSession` holding per-card state: `performer_stage`, `agent_dispatch`, `workspace_path`, `performer_events`, `performer_metrics`, `card_tokens_total`, and other card-scoped fields. One card blocking or erroring does not affect other active cards.

**Why this priority**: Without session isolation, concurrent cards would corrupt each other's state. This is architecturally required for P1.

**Independent Test**: Dispatch 2 cards concurrently. Block one (performer returns "blocked"). Verify the other continues progressing through the lifecycle independently.

**Acceptance Scenarios**:

1. **Given** 2 cards are dispatched concurrently, **When** card A's performer returns "blocked", **Then** card B continues to be monitored and can reach "In Review" independently.
2. **Given** 2 cards are active, **When** card A advances from "implementing" to "reviewing", **Then** card B's `performer_stage` is unaffected.
3. **Given** a card session completes (card reaches Done), **When** `check_board` runs next, **Then** the completed session is removed and a new card can be picked up.

---

### User Story 3 -- Dashboard Shows All Active Cards (Priority: P2)

The web dashboard displays all active card sessions, showing each card's title, current performer stage, token usage, and elapsed time. The existing single-card view is replaced with a multi-card list.

**Why this priority**: Operators need visibility into all concurrent work, but the core orchestration (P1) must work first.

**Independent Test**: Render the dashboard with 2 active sessions. Verify both cards appear with their respective stage and token data.

**Acceptance Scenarios**:

1. **Given** 2 cards are active, **When** the dashboard renders, **Then** both cards are displayed with their titles and performer stages.
2. **Given** `max_concurrent_cards=1` (single-card mode), **When** the dashboard renders, **Then** the display is visually identical to the pre-035 dashboard.

---

### Edge Cases

- What if two cards need the same performer service simultaneously? (Services are stateless dispatchers; concurrent calls are fine. Each dispatch gets its own session_id.)
- What if StateStore fails to persist one session but succeeds for others? (Log the failure; do not abort healthy sessions.)
- What if a card is moved out of "In Progress" externally while a session is active? (Detect on next board poll; cancel the session gracefully.)
- What if `max_concurrent_cards` is changed at runtime via config hot-reload? (Apply on next check_board cycle; do not kill existing sessions.)
- What if the same card appears in TODO twice (duplicate board entry)? (Deduplicate by card ID; ignore the duplicate.)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A `CardSession` abstraction MUST encapsulate all per-card state: `current_card`, `performer_stage`, `agent_dispatch`, `agent_dispatch_at`, `workspace_path`, `workspace_branch`, `performer_events`, `performer_metrics`, `card_tokens_total`, `card_cost_estimate`, `card_budget_alert_sent`, `open_questions`, `card_clarifications`, `relay_feedback`, `system_error_count`, `system_error_last_at`, `system_error_reason`, `system_error_notified`, `commit_summary`, `agent_health_status`.
- **FR-002**: `CoordinareState` MUST replace single-card fields with an `active_sessions: dict[str, CardSession]` mapping (card ID -> session).
- **FR-003**: `check_board` MUST pick up additional cards (up to `max_concurrent_cards - len(active_sessions)`) on each poll cycle.
- **FR-004**: The daemon loop MUST invoke graph nodes for each active session independently. A failure in one session MUST NOT block or corrupt other sessions.
- **FR-005**: `max_concurrent_cards` MUST be configurable in `config.yaml` with a default of 1.
- **FR-006**: When `max_concurrent_cards=1`, runtime behavior MUST be identical to the pre-035 single-card mode.
- **FR-007**: `StateStore` MUST serialize and restore all active sessions across daemon restarts.
- **FR-008**: The dashboard MUST display all active sessions with per-card status.
- **FR-009**: Prometheus metrics MUST include a `coordinare_active_sessions` gauge showing the current number of active card sessions.
- **FR-010**: When a card session completes (Done or cancelled), it MUST be removed from `active_sessions` to free capacity.

### Key Entities

- **CardSession**: Per-card state container holding all fields currently on CoordinareState that are card-scoped.
- **active_sessions**: Mapping of card ID to CardSession on CoordinareState, replacing the flat single-card fields.
- **max_concurrent_cards**: Integer configuration controlling the session pool size.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With `max_concurrent_cards=3`, 3 cards progress through the full lifecycle concurrently, each reaching "In Review" independently.
- **SC-002**: With `max_concurrent_cards=1`, all existing tests pass without modification (backward compatibility).
- **SC-003**: The dashboard displays N active cards simultaneously with correct per-card data.
- **SC-004**: StateStore correctly persists and restores N active sessions across a daemon restart.
- **SC-005**: One card blocking does not delay dispatch or monitoring of other active cards.

## Assumptions

- Performer services are stateless and can handle concurrent dispatch calls with different session IDs. No service-level concurrency mutex is needed.
- The GitHub board can have multiple cards in "In Progress" simultaneously. The coordinare does not enforce single-card-in-progress at the board level.
- Graph node functions receive a session-scoped state slice, not the full global state. Nodes do not need to be aware of other active sessions.
- The daemon loop processes sessions sequentially within a single poll cycle (not truly parallel asyncio tasks per session). True per-session parallelism is a future optimization.
