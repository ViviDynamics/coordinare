# Tasks: Cost & Token Tracking

**Input**: Design documents from `/specs/034-cost-token-tracking/`
**Prerequisites**: plan.md (required), spec.md (required for user stories)

**Tests**: Included — spec calls for ~12 unit tests.

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

**Purpose**: Config model, state fields, Prometheus metrics

- [x] T001 Add CostTrackingConfig model to src/coordinare/config.py
- [x] T002 [P] Add card_tokens_total, card_cost_estimate, card_budget_alert_sent to CoordinareState in src/coordinare/graph/state.py
- [x] T003 [P] Add coordinare_card_tokens_total Counter and coordinare_card_cost_estimate_dollars Gauge to src/coordinare/metrics.py

**Checkpoint**: Config, state, and metrics ready

---

## Phase 2: User Story 1 — Accumulate Token Counts per Card (Priority: P1) 🎯 MVP

**Goal**: Accumulate tokens_processed from performer status into card_tokens_total, recalculate cost, reset on new card.

**Independent Test**: Mock check_status with tokens_processed values, verify accumulation and reset.

### Tests for User Story 1

- [x] T004 [P] [US1] Add test: tokens accumulate across multiple poll cycles in tests/unit/graph/nodes/test_monitor_performer.py
- [x] T005 [P] [US1] Add test: no tokens_processed field leaves total unchanged in tests/unit/graph/nodes/test_monitor_performer.py
- [x] T006 [P] [US1] Add test: negative tokens_processed clamped to 0 in tests/unit/graph/nodes/test_monitor_performer.py
- [x] T007 [P] [US1] Add test: card_tokens_total resets on new card dispatch in tests/unit/graph/nodes/test_dispatch_performer.py

### Implementation for User Story 1

- [x] T008 [US1] Accumulate tokens_processed into card_tokens_total in monitor_performer in src/coordinare/graph/nodes/monitor_performer.py
- [x] T009 [US1] Recalculate card_cost_estimate after each accumulation in src/coordinare/graph/nodes/monitor_performer.py
- [x] T010 [US1] Increment Prometheus counter with role label on accumulation in src/coordinare/graph/nodes/monitor_performer.py
- [x] T011 [US1] Reset card_tokens_total, card_cost_estimate, card_budget_alert_sent on new card dispatch in src/coordinare/graph/nodes/dispatch_performer.py

**Checkpoint**: Token accumulation working and tested

---

## Phase 3: User Story 2 — Dashboard Cost Display (Priority: P2)

**Goal**: Show token usage and estimated cost on the dashboard.

### Tests for User Story 2

- [x] T012 [P] [US2] Add test: dashboard displays formatted token count and dollar cost in tests/unit/test_dashboard.py

### Implementation for User Story 2

- [x] T013 [US2] Add Token Usage panel to dashboard HTML and SSE state in src/coordinare/dashboard.py

**Checkpoint**: Dashboard shows cost

---

## Phase 4: User Story 3 — Cost Budget Alerts (Priority: P3)

**Goal**: Notify when per-card cost exceeds configured budget, exactly once per card.

### Tests for User Story 3

- [x] T014 [P] [US3] Add test: budget exceeded dispatches notification exactly once in tests/unit/graph/nodes/test_monitor_performer.py
- [x] T015 [P] [US3] Add test: no budget configured skips check in tests/unit/graph/nodes/test_monitor_performer.py

### Implementation for User Story 3

- [x] T016 [US3] Add budget check and notification dispatch after cost recalculation in src/coordinare/graph/nodes/monitor_performer.py

**Checkpoint**: Budget alerts working

---

## Phase 5: Polish

- [x] T017 Run full test suite and lint check
- [x] T018 Mark spec 034 tasks as complete

---

## Dependencies & Execution Order

- **Phase 1**: No dependencies
- **Phase 2 (US1)**: Depends on Phase 1
- **Phase 3 (US2)**: Depends on Phase 1; independent of Phase 2
- **Phase 4 (US3)**: Depends on Phase 2 (accumulation logic)
- **Phase 5**: Depends on all above

## Notes

- Total tasks: 18
- US1: 8 (4 tests + 4 implementation)
- US2: 2 (1 test + 1 implementation)
- US3: 3 (2 tests + 1 implementation)
- Setup: 3, Polish: 2
