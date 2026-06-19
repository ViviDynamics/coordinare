# Tasks: Restart-Time Board Reconciliation for Restored Sessions

**Feature**: `094-restart-session-reconcile` | **Spec**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md)
**Approach**: TDD (Constitution II; research R2 — the #158 reproduction test drives the exact corrected-field set).

## Conventions

- Primary source: `src/coordinare/daemon.py` (extend `_reconcile_with_board`, daemon.py:805; reuse `_infer_phase_from_board_column` daemon.py:795, `_retire_active_session`, `github_service.poll_board()`).
- Tests: `tests/unit/test_daemon_restart_reconcile.py` (one module; board poll mocked, deterministic).
- Run tests: `.venv/bin/pytest tests/unit/test_daemon_restart_reconcile.py -v` · Lint: `.venv/bin/ruff check <files>`.
- **[P]** = parallelizable (different file / no incomplete-task dependency). Tasks touching `daemon.py` are sequential (same file).

---

## Phase 1: Setup

- [X] T001 Confirm injection points and behavior baseline in `src/coordinare/daemon.py`: read `_reconcile_with_board` (805), `_infer_phase_from_board_column` (795), `_retire_active_session` import (26), the per-session restore loop (667-702), `_compute_eligibility` (327), and the single call site (2242); note in a scratch comment which currently handle only the top-level `active_card_id` so the extension target is unambiguous.
- [X] T002 [P] Create the test module skeleton `tests/unit/test_daemon_restart_reconcile.py` with shared fixtures/helpers: a builder for a `WorkflowSnapshot` carrying an `active_sessions` dict, and a fake `github_service` whose `poll_board()` returns a configurable `{"snapshot": {COLUMN: [card_ids]}}` (mirror existing daemon-reconciliation test fixtures).

---

## Phase 2: Foundational (blocking prerequisites)

**Purpose**: Establish the per-session reconciliation seam that every user story builds on, and pin research R2 with the reproduction test FIRST.

- [X] T003 [US1] Write the FAILING reproduction test `test_blocked_session_for_todo_card_becomes_dispatch_eligible` in `tests/unit/test_daemon_restart_reconcile.py`: restore a snapshot whose `active_sessions[card]` has `phase="blocked"` (and the #158 idle variant) while `poll_board()` reports the card in `TODO`; assert that after reconciliation the session is dispatch-eligible (not skipped `blocked_column`, not stuck idle). This test defines the exact corrected-field set (phase and/or seeded `current_card` column). MUST fail before T005.
- [X] T004 Extract a focused helper `_reconcile_session_with_board(card_id, session, board_snapshot) -> ReconcileOutcome` in `src/coordinare/daemon.py` (signature/skeleton only; raises or returns no-op) so `_reconcile_with_board` and tests have a single per-session seam. Keeps the method single-responsibility (Constitution I).

**Checkpoint**: Reproduction test exists and fails; the per-session seam is in place.

---

## Phase 3: User Story 1 — A re-opened card resumes instead of wedging (P1)

**Goal**: Board is source of truth at restore for every session; a card moved off BLOCKED resumes, a truly-blocked card stays blocked.
**Independent test**: snapshot session `blocked`/board `TODO` → eligible; board `BLOCKED` → stays blocked.

- [X] T005 [US1] Implement `_reconcile_session_with_board` in `src/coordinare/daemon.py`: look up the card's column in `board_snapshot`, infer phase via `_infer_phase_from_board_column`, and when it diverges from the persisted phase (and the session is not a preserved in-flight session — see US2) correct the session's phase (and refresh its `current_card` column) so it matches the board. Make T003 pass with the minimal correction.
- [X] T006 [US1] Wire `_reconcile_with_board` (daemon.py:805) to iterate `snapshot.active_sessions` calling `_reconcile_session_with_board` for each, in addition to the existing top-level focus reconciliation. Apply corrections to `self._state["active_sessions"]`. Include a test asserting **multiple diverging sessions are corrected independently** in one pass (edge case: "multiple cards corrected at once"), and a test for **card advanced past the persisted stage** (board `IN_REVIEW` vs snapshot `implementing` → moved forward to `monitoring_pr`, not replayed).
- [X] T007 [US1] Write `test_truly_blocked_card_stays_blocked` in `tests/unit/test_daemon_restart_reconcile.py`: `poll_board()` reports the card in `BLOCKED` → session keeps `phase="blocked"` and is NOT made dispatch-eligible (FR-005, SC-005). Implement the guard in `_reconcile_session_with_board` if needed to pass.

**Checkpoint**: US1 independently testable — wedged-but-moved cards resume; genuinely-blocked cards do not.

---

## Phase 4: User Story 2 — In-flight work survives reconciliation (P1)

**Goal**: Reconciliation is surgical — `monitoring_pr` / `monitoring_performer` sessions keep their PR/performer context; no stage demotion.
**Independent test**: snapshot with a PR-monitoring session and a live-performer session → both retain context after restart.

- [X] T008 [US2] Write `test_monitoring_pr_session_preserved` and `test_monitoring_performer_session_preserved` in `tests/unit/test_daemon_restart_reconcile.py`: a `monitoring_pr` session (with PR reference) and a `monitoring_performer` session (with live session id) whose board column is consistent are left fully intact — PR reference / performer linkage / stage unchanged (FR-004, SC-003). MUST fail before T009.
- [X] T009 [US2] Add the in-flight preservation guard to `_reconcile_session_with_board` in `src/coordinare/daemon.py`: when the session is legitimately `monitoring_pr` (PR ref present) or `monitoring_performer` (live session present) and the board column is consistent with that phase, skip correction entirely and preserve all in-flight fields. Make T008 pass.

**Checkpoint**: US2 independently testable — the fix cannot strand a live PR or orphan a performer.

---

## Phase 5: User Story 3 — The correction is diagnosable from logs (P2)

**Goal**: Every correction emits one secret-free structured event.
**Independent test**: trigger a correction → exactly one `restart_reconcile.session_corrected` event with ids/columns/phases only.

- [X] T010 [US3] Write `test_correction_emits_secretfree_event` in `tests/unit/test_daemon_restart_reconcile.py`: capture structlog output for a corrected session; assert exactly one `restart_reconcile.session_corrected` event with `card_id`, `prior_phase`, `prior_column`, `board_column`, `corrected_phase`, `symphony`, and assert NO secret-shaped values appear (FR-006/FR-007, SC-004). MUST fail before T011.
- [X] T011 [US3] Emit the `restart_reconcile.session_corrected` structlog event from `_reconcile_session_with_board` (or its caller) in `src/coordinare/daemon.py` on each applied correction, fields per data-model.md (identifiers/columns/phases only). Make T010 pass.

**Checkpoint**: US3 independently testable — corrections are attributable from logs without a state-file autopsy.

---

## Phase 6: Polish & Cross-Cutting Concerns (edge cases + hardening)

- [X] T012 Write `test_done_or_absent_card_session_retired` in `tests/unit/test_daemon_restart_reconcile.py`: on a SUCCESSFUL board read where the card is DONE or absent, the session is retired (`_retire_active_session`) and the top-level focus is cleared if it pointed there (FR-010). Implement retirement + focus-consistency in `src/coordinare/daemon.py` to pass.
- [X] T013 Write `test_board_poll_failure_preserves_all_sessions` in `tests/unit/test_daemon_restart_reconcile.py`: `poll_board()` raises → NO session modified or deleted, daemon startup proceeds, `board_reconciliation_skipped` logged (FR-009). Confirm the existing try/except covers the per-session pass; adjust in `src/coordinare/daemon.py` if needed.
- [X] T014 Write `test_reconcile_is_convergent_noop_on_consistent_state` in `tests/unit/test_daemon_restart_reconcile.py`: reconcile once, then again on the corrected state → zero corrections / zero `session_corrected` events (FR-008, SC-006).
- [X] T015 [P] Reconcile top-level/session consistency (FR-010): ensure after the per-session pass the top-level `active_card_id`/`phase` are consistent with the corrected/retired session set in `src/coordinare/daemon.py`; add/extend a test asserting the focus does not dangle on a retired or corrected-away card.
- [X] T016 Run the full daemon reconciliation + snapshot-persistence regression suites together (`tests/unit/test_daemon_*` and `tests/unit/test_daemon_snapshot_persistence.py`) to confirm no cross-phase regression; `.venv/bin/ruff check` all edited files.
- [X] T017 Walk quickstart.md US1/US2/US3 + edge scenarios against the implemented behavior; confirm SC-001..SC-006 each have a covering test, and that the reconciliation event carries keys/columns/phases only (FR-007, SC-004).
- [X] T018 [P] Verify no persisted-schema change leaked (`state_store.py` `CURRENT_SCHEMA_VERSION` unchanged) and that no new external dependency was added (Constitution I; plan "no new deps").

---

## Dependencies & Execution Order

- **Setup (T001-T002)** → **Foundational (T003-T004)** → **US1 (T005-T007)** → **US2 (T008-T009)** → **US3 (T010-T011)** → **Polish (T012-T018)**.
- T003 (reproduction test) MUST precede T005 (implementation) — TDD red→green.
- T004 (seam) blocks T005/T009/T011 (all extend `_reconcile_session_with_board`).
- US2 (T009) refines the same helper US1 (T005) creates → US1 before US2.
- Most implementation tasks touch `src/coordinare/daemon.py` → **sequential, not parallel**. Test-writing tasks in the single module are also sequential, except where marked [P] (separate files: T002 skeleton, T018 schema/dep check).

## Parallel Opportunities

- T002 [P] (new test file) can start alongside T001.
- T018 [P] (schema/dep verification — separate files) can run alongside T016/T017.
- Within `daemon.py`, tasks are serialized by the shared file.

## Implementation Strategy (MVP first)

- **MVP = US1 (T001-T007)**: closes the #158 wedge (the core value, SC-001/SC-002/SC-005). Independently shippable.
- **US2 (T008-T009)** is the safety guardrail — strongly recommended in the same PR so the fix cannot regress in-flight work.
- **US3 (T010-T011)** + Polish add observability and edge-case hardening.
