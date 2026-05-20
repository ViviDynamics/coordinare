# Tasks: Unify Card Pickup Paths

**Input**: Design documents from `/specs/066-unify-card-pickup/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/current_card-derivation.md, quickstart.md

**Tests**: Included — FR-002, FR-007, FR-008 require test work as part of the refactor; existing test suites are the regression contract.

**Organization**: Tasks grouped by the two user stories from spec.md (both P1). US1 is the operator-visible no-surprise unification; US2 is the contributor-facing single-site change guarantee.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Different files, no dependencies on incomplete tasks
- **[Story]**: US1 or US2
- All paths absolute or repo-root-relative

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish the derivation function and the test fixture invariant before any branch deletion. This is shared infrastructure both user stories rely on.

- [X] T001 Add `_rederive_current_card(state)` helper in `src/coordinare/graph/state.py` per `specs/066-unify-card-pickup/contracts/current_card-derivation.md`. Export it for use by `check_board.py` and `daemon.py`.
- [X] T002 [P] Document `CoordinareState.current_card` as a derived mirror in the `state.py` docstring (FR-010); reference the contract file.
- [X] T003 [P] Add `tests/unit/graph/nodes/conftest.py` with a fixture that asserts the I3 invariant (`state["current_card"] == active_sessions[active_card_id]["current_card"]` xor both `None`) at end-of-test for any test that touches `CoordinareState`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: v1 snapshot rehydration + sole-entry-point wiring. Both user stories depend on this. No pickup-branch deletion happens here yet.

**⚠️ CRITICAL**: No US1/US2 work proceeds until Phase 2 completes.

- [X] T004 Implement v1 → v2 snapshot synthesis in `src/coordinare/daemon.py::_restore_from_snapshot`. When `active_sessions` is absent but `current_card` is set, synthesize a single-entry session keyed on `current_card.id`, set `active_card_id`, then call `_rederive_current_card` (FR-005).
- [X] T005 Emit `state_store.v1_snapshot_rehydrated` structlog event when the synthesis path fires (per `quickstart.md` signals table).
- [X] T006 Wire `_rederive_current_card` invocation at the three sites required by the contract: end of `check_board.py` node, end of each per-session step in `daemon._invoke_multi_session`, end of `_restore_from_snapshot`. No branch deletion yet — the call is additive at this phase.
- [X] T007 [P] V1-snapshot synthesis case landed in `tests/unit/test_daemon_coverage.py` (the daemon owns `_restore_from_snapshot`; `state_store.py` only serializes), asserting the resulting `active_sessions` shape matches what a fresh pickup of the same card would produce (FR-007).
- [X] T008 [P] Added a unit test in `tests/unit/graph/nodes/test_check_board.py` asserting that after any `check_board` invocation, the I3 invariant holds (uses the conftest fixture from T003).

**Checkpoint**: Derivation is centralized and v1 snapshots rehydrate. The legacy single-card branch still runs; nothing is removed yet.

---

## Phase 3: User Story 1 — Operator Configures Concurrency Without Behavioural Surprise (Priority: P1) 🎯 MVP

**Goal**: `max_concurrent_cards=1` runs through the unified multi-card path with no behaviour change visible to operators.

**Independent Test**: Daemon with `max_concurrent_cards=1` against a board with one IN_PROGRESS and two TODO cards: only one card has `phase ∈ NON_PASSIVE_PHASES`; `active_sessions` has exactly one entry; un-block resets `feedback_cycle_count` and emits `dispatcher.feedback_cycle_reset`; dashboard SSE payload identical to pre-066.

### Tests for User Story 1

- [X] T009 [P] [US1] Parameterise `tests/unit/graph/nodes/test_check_board_multicard*.py` across `max_concurrent_cards=1` and `max_concurrent_cards=3` so every multi-card scenario also asserts the N=1 case (FR-002, FR-008). _(Three parameterised parity tests added at end of `tests/unit/graph/nodes/test_check_board.py`: un-block reset, fresh pickup no-reset, unified_pickup log fires once — all green at N∈{1,3}.)_
- [X] T010 [P] [US1] Add a test in `tests/unit/graph/nodes/test_check_board.py` for un-block detection at `max_concurrent_cards=1` asserting `dispatcher.feedback_cycle_reset` fires with `{card_id, prior_count, total_feedback_cycles, triage_blocks}` (FR-003).
- [X] T011 [P] [US1] Add a test asserting v1 snapshot rehydration produces the same `active_sessions` shape as a fresh pickup of the same card (FR-007), exercising the end-to-end restore path. _(Covered by T007 in `tests/unit/test_daemon_coverage.py::test_restore_from_snapshot_v1_synthesizes_active_sessions`.)_
- [X] T012 [P] [US1] Add/update a test in `tests/e2e/test_dashboard_browser.py` (or equivalent SSE-payload assertion) confirming top-level `current_card`/`performer_stage`/`feedback_cycle_count` remain on the SSE payload unchanged (FR-006). _(Implemented as a unit-level `build_snapshot` regression in `tests/unit/test_dashboard.py::test_build_snapshot_preserves_fields_under_session_mirror`.)_

### Implementation for User Story 1

- [X] T013 [US1] In `src/coordinare/graph/nodes/check_board.py`, extract a status-parameterised pickup helper that handles TODO, IN_PROGRESS, and IN_REVIEW per `data-model.md`'s Status-aware Phase Derivation Table. Call sites are the surviving multi-card branch only — no behaviour change yet.
- [X] T014 [US1] Delete the single-card IN_PROGRESS re-adoption block (`check_board.py:292–315` per `research.md` §1); all IN_PROGRESS re-adopts now flow through the multi-card path with N=1. Verify the I3 invariant test (T008) still passes.
- [X] T015 [US1] Delete the single-card `dispatching | monitoring_performer | blocked` early-return (`check_board.py:476–520`); multi-card phase derivation already covers these (Fix 5 lesson; `research.md` §1).
- [X] T016 [US1] Port the US4 un-block reset side-effect from the single-card branch into the multi-card pickup path (`check_board.py:1029+`). The reset fires when a card transitions out of BLOCKED, regardless of `max_concurrent_cards` (FR-003).
- [X] T017 [US1] Delete the single-card TODO pickup + un-block detection block (`check_board.py:590–707`); the multi-card path at L1029+ now owns both (depends on T016).
- [X] T018 [US1] Migrate the Q&A-answer `previous_status` mutation at `check_board.py:775–776` to write on the session entry; the end-of-cycle `_rederive_current_card` propagates to the mirror (FR-011).
- [X] T019 [US1] In `src/coordinare/daemon.py`, make `_invoke_multi_session` the sole graph entry path. Remove any `if config.max_concurrent_cards == 1:` shortcuts (FR-004). Verify single-session inputs (N=1) flow through unchanged.
- [X] T020 [US1] Emit `check_board.unified_pickup` structlog event once per cycle when the unified path runs (per `quickstart.md` signals table).

**Checkpoint**: `max_concurrent_cards=1` and `max_concurrent_cards=3` exercise the same pickup code. Existing single-card and multi-card test suites both pass. SSE payload unchanged.

---

## Phase 4: User Story 2 — Future Pickup Changes Land Once (Priority: P1)

**Goal**: No `if max_cards > 1` (or equivalent) branching at the pickup site. A future contributor making a pickup change writes it once.

**Independent Test**: `grep -n "if max_cards > 1\|max_concurrent_cards == 1\|single.card.mode" src/coordinare/graph/nodes/check_board.py` returns zero pickup-time branches (SC-001). The pre-poll throttle gated on N>1 (line 174 per `research.md` §1) is a legitimate optimisation and may remain.

### Tests for User Story 2

- [X] T021 [P] [US2] Add a static-check test in `tests/unit/graph/nodes/test_check_board.py` that reads `check_board.py` source and asserts SC-001 (zero pickup-time branches on `max_concurrent_cards`).

### Implementation for User Story 2

- [X] T022 [US2] Read-site migration: `src/coordinare/graph/nodes/dispatch_performer.py`. Replace `state["current_card"]` *writes* (lines 168, 236, 632, 718 per `research.md` §2) with session-entry writes; reads can remain via the mirror. Run the per-node unit suite.
- [X] T023 [US2] Read-site migration: `src/coordinare/graph/nodes/monitor_performer.py` (lines 90, 105, 124, 1125, 1534). Writes migrate to session; reads keep using the mirror.
- [X] T024 [US2] Read-site migration: `src/coordinare/graph/nodes/monitor_pr.py` (lines 83, 128).
- [X] T025 [US2] Read-site migration: `src/coordinare/graph/nodes/merge_pr.py` (line 77).
- [X] T026 [US2] Read-site migration: `src/coordinare/graph/nodes/assess_card.py` (line 170) and `src/coordinare/graph/nodes/classify_human_feedback.py` (line 340).
- [X] T027 [US2] Read-site migration: `src/coordinare/daemon.py` (lines 393, 499, 773, 1075 per `research.md` §2). Writes go to the session entry; the daemon's per-step `_rederive_current_card` call (T006) propagates.
- [X] T028 [P] [US2] Read-site audit: `src/coordinare/services/persona_service.py` and `src/coordinare/services/pr_checks_service.py`. Migrate any direct writes; reads via the mirror are fine.
- [X] T029 [P] [US2] Write-site migration: `src/coordinare/dashboard.py`. Any direct write to `state["current_card"]` MUST move to the session entry (FR-010). The SSE payload top-level `current_card` MUST remain in the outgoing payload (FR-006) — produced by reading the derived mirror. Internal reads MAY continue via the mirror; no read-site migration required.
- [X] T030 [US2] Verify SC-001 by running the static-check test from T021. Fail-fast: if any pickup-time `max_concurrent_cards` branch slipped back in, T030 surfaces it.

**Checkpoint**: All write-sites for `current_card` are gone outside `_rederive_current_card`. Single pickup path. SC-001 passes.

---

## Phase 5: Polish & Cross-Cutting Concerns

- [X] T031 [P] Run `.venv/bin/pytest tests/unit tests/integration` and confirm green (FR-008).
- [X] T032 [P] Benchmark a board scan at `max_concurrent_cards=1` against a pre-066 baseline; assert ≤ +50ms (SC-002). Capture wall-clock if no `pytest-benchmark` setup exists; commit the script under `scripts/bench_066.py` if it doesn't exist already. _(Script at `scripts/bench_066.py`; local run reports median ~0.07ms / p95 ~0.11ms at N=1 with 20 TODO cards — far within the 50ms SC-002 budget.)_
- [ ] T033 [P] Manual quickstart validation per `specs/066-unify-card-pickup/quickstart.md`: run one TODO card end-to-end at N=1 to IN_REVIEW, then two concurrent at N=3, confirming `dispatcher.feedback_cycle_reset` appears in both modes (FR-003). _(Operator action required — cannot be executed from this environment.)_
- [X] T034 Update `CLAUDE.md` Active Technologies / Recent Changes section to reflect the refactor landing (no new tech; just the change note).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately. T001 blocks T006.
- **Foundational (Phase 2)**: Depends on Phase 1. T004 blocks T007. T006 blocks T008 and the entire US1 phase.
- **User Story 1 (Phase 3)**: Depends on Phase 2 complete. T013 blocks T014–T018. T016 blocks T017.
- **User Story 2 (Phase 4)**: Depends on US1 (T019 in particular — single entry point). Read-site migrations (T022–T029) can run in parallel.
- **Polish (Phase 5)**: Depends on all prior phases.

### Within Each User Story

- US1 ordering: T013 (extract helper) → T014/T015 (delete single-card branches) → T016 (port US4) → T017 (delete TODO single-card) → T018 (Q&A mutation) → T019 (sole entry point) → T020 (log signal).
- US2 ordering: T021 (test gate) first; T022–T029 are file-local, can parallelize; T030 verifies at end.

### Parallel Opportunities

- T002 and T003 can parallelize with T001.
- T007 and T008 parallelize after T004/T006.
- US1 tests (T009–T012) parallelize.
- US2 read-site migrations (T022–T029) parallelize per file.
- Polish tasks (T031–T033) parallelize.

---

## Parallel Example: US2 read-site migrations

```bash
# After US1 lands, fan out read-site migrations:
Task: "T022 Migrate dispatch_performer.py writes to session"
Task: "T023 Migrate monitor_performer.py writes to session"
Task: "T024 Migrate monitor_pr.py writes to session"
Task: "T025 Migrate merge_pr.py writes to session"
Task: "T026 Migrate assess_card.py + classify_human_feedback.py writes"
Task: "T028 Audit persona_service.py + pr_checks_service.py"
Task: "T029 Audit dashboard.py SSE writes"
```

---

## Implementation Strategy

### MVP (US1 only)

1. Phase 1 → Phase 2 → Phase 3.
2. STOP and run full unit + integration suites. If green and SC-001 already holds at the pickup site (likely after T017+T019), demo and merge.
3. US2 (read-site migration) can be a follow-up commit on the same branch.

### Single-pass (recommended given wide scope)

1. Phase 1 → 2 → 3 → 4 → 5 sequentially.
2. One PR, atomic refactor. This matches the operator's "fully complete" directive in the Scope Decision Log.

---

## Notes

- The legacy `check_board.py` line numbers in research.md §1 / §2 are pre-refactor anchors. As the refactor proceeds, line numbers will shift — use the surrounding context (function names, comments) to locate the right block, not the raw line numbers.
- I3 invariant fixture (T003) is the load-bearing safety net; if a write-site migration slips, T003's assertion will fail at end-of-test.
- The pre-poll throttle gated on `N>1` at `check_board.py:174` is *not* pickup logic and is explicitly exempt from SC-001's grep.
- Commit after each phase checkpoint. Do not bundle Phase 3 and Phase 4 into a single commit — the diff is large enough to warrant separation for review.
