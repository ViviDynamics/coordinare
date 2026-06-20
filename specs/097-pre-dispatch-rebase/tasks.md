# Tasks: Pre-Dispatch Rebase Guard

**Input**: Design documents from `/specs/097-pre-dispatch-rebase/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/pre-dispatch-guard.md, quickstart.md

**Tests**: INCLUDED — Constitution II (TDD, non-negotiable).

**Organization**: by user story (US1 P1 rebase-before-dispatch → US2 P2 no-clobber/defer → US3 P3 thrash/isolation/observability). Reuses spec-096's machinery (`check_mergeability`, `should_attempt_rebase`, `run_rebase_round`, `last_rebase_attempt`) — **no new persisted state, no schema change.**

## Path Conventions

Single project: `src/coordinare/`, `tests/`. Reused unchanged: `services/rebase.py` (rebase ops), `services/github.py` (`check_mergeability`), 096's persisted fields.

---

## Phase 1: Setup

- [X] T001 Confirm the injection point and reuse surface by reading: `src/coordinare/graph/nodes/dispatch_performer.py` (the mutex + `check_inflight` + multi-PR block ~L284-330, and the final `return await _dispatch_performer_body(state)` ~L330), `src/coordinare/services/rebase.py` (`run_rebase_round`, `rebase_branch`, `should_attempt_rebase`, `prepare_conflict_resolution`, `fetch_main_sha`), and `src/coordinare/services/github.py` (`check_mergeability` return shape: `mergeable_raw`/`merge_state_status`/`head_ref_oid`). Note the exact spot the guard runs (after `check_inflight`, before the body) and how `pr_url`/`current_card` are already in scope.

---

## Phase 2: Foundational (pure decision helper — blocks all stories)

- [X] T002 Write FAILING unit test `tests/unit/services/test_pre_dispatch_decision.py` for a pure classifier `classify_pre_dispatch(mergeable_raw, merge_state_status, head_sha, session, current_main_sha) -> Literal["proceed","rebase","defer","blocked_thrash"]`: UNKNOWN/empty-head → `defer`; MERGEABLE/CLEAN → `proceed`; CONFLICTING or BEHIND with no/clearing marker → `rebase`; CONFLICTING/BEHIND already BLOCKED/FAILED against the same (main, head) → `blocked_thrash` (reuses `should_attempt_rebase`). MUST fail before T003.
- [X] T003 Implement `classify_pre_dispatch(...)` in `src/coordinare/services/rebase.py` (pure; delegates the thrash check to `should_attempt_rebase`). Make T002 pass.

**Checkpoint**: the decision logic is unit-tested in isolation, independent of the dispatch node.

---

## Phase 3: User Story 1 — Rebase before dispatch (Priority: P1) 🎯 MVP

**Goal**: a performer is never dispatched onto a conflicting base. **Independent test**: in-flight card with a CONFLICTING open-PR branch → rebase runs before `_dispatch_performer_body`; clean → dispatch on rebased head; conflict → block, no dispatch.

- [X] T004 [US1] Write FAILING tests in `tests/unit/graph/nodes/test_dispatch_performer_rebase.py`: (a) CONFLICTING branch + clean rebase → `run_rebase_round` called AND `_dispatch_performer_body` still runs (dispatch proceeds on rebased head); (b) CONFLICTING + rebase BLOCKED → `prepare_conflict_resolution` called AND `_dispatch_performer_body` NOT called (held/blocked return); (c) MERGEABLE/current → no rebase, `_dispatch_performer_body` runs as today. MUST fail before T005.
- [X] T005 [US1] Implement the guard in `src/coordinare/graph/nodes/dispatch_performer.py` immediately before `return await _dispatch_performer_body(state)`: only when the card has an open PR (`pr_url`/`pr_node_id`) and auto-rebase is enabled, call `check_mergeability` + `classify_pre_dispatch`; on `rebase` run `run_rebase_round({card_id: session}, current_main, ...)` (CLEAN/PERFORMER_RESOLVED → fall through to the body; BLOCKED → `prepare_conflict_resolution` + return held/blocked without the body); on `proceed` fall through unchanged. Source `current_main` via `fetch_main_sha` (per-cycle cache) / `last_known_main_sha`. Make T004 pass.
- [X] T006 [US1] Write + pass a test that the guard is a NO-OP when the card has no open PR (first implementer run that will create the branch) — `check_mergeability` not called, `_dispatch_performer_body` runs (FR-006).

**Checkpoint**: US1 independently testable — the livelock is closed (conflicting base never receives a fresh performer).

---

## Phase 4: User Story 2 — No clobber / defer (Priority: P2)

**Goal**: never rebase under a live performer; defer on unknown. **Independent test**: guard runs only post-`check_inflight`; UNKNOWN mergeability → defer (no rebase, no dispatch).

- [X] T007 [US2] Write FAILING test in `tests/unit/graph/nodes/test_dispatch_performer_rebase.py`: when `check_inflight` advises `refuse` (a performer is already in flight), the guard does NOT run `check_mergeability`/`run_rebase_round` (the existing early-return at the in-flight guard is preserved). MUST fail before T008 (or assert current behavior is preserved).
- [X] T008 [US2] Write + pass test: mergeability `UNKNOWN` (or empty `head_ref_oid`) → guard returns the state without dispatching and without rebasing (defer one cycle, FR-005); `_dispatch_performer_body` NOT called.
- [X] T009 [US2] Verify (test) the guard sits AFTER the in-flight guard so FR-004 holds by construction; document the ordering in a code comment in `dispatch_performer.py`.

**Checkpoint**: US2 independently testable — no rebase under a running performer; unknown defers.

---

## Phase 5: User Story 3 — No thrash, isolation, observability (Priority: P3)

**Goal**: no re-rebase of a blocked branch; one card's failure doesn't block others; secret-free record.

- [X] T010 [US3] Write FAILING tests in `tests/unit/graph/nodes/test_dispatch_performer_rebase.py`: (a) a branch with a BLOCKED `last_rebase_attempt` matching current (main, head) → `classify_pre_dispatch`→`blocked_thrash` → no `run_rebase_round`, no dispatch onto the conflicting base; (b) `run_rebase_round` raising for the card is caught (guard does not crash dispatch; returns a safe state). MUST fail before T011.
- [X] T011 [US3] Implement anti-thrash + isolation in the guard: honor `blocked_thrash` (skip rebase, do not dispatch onto the known-conflicting base); wrap the rebase in try/except so a failure is logged and does not crash `dispatch_performer`; write the `last_rebase_attempt` marker after each attempt. Make T010 pass.
- [X] T012 [US3] Write + pass a structlog-capture test: a guard rebase emits `rebase.triggered` with `reason="pre_dispatch"`, `card_id`, `branch`, `prev_main_sha`, `current_main_sha`, `outcome`, and no secret values (FR-010). Emit the record in `dispatch_performer.py`.

---

## Phase 6: Polish & Cross-Cutting

- [X] T013 [P] Regression: run the dispatch + rebase + check_board(096) suites together (`tests/unit/graph/nodes/test_dispatch_performer*.py`, `tests/unit/services/test_rebase*.py`, `tests/unit/graph/nodes/test_check_board_rebase.py`) — confirm 096's check_board sweep and the 047 merge-time path are unchanged (FR-011/SC-006).
- [X] T014 [P] Confirm no persistence change: schema stays v11, no new `state_store`/`session` fields (reuses 096's). Run `tests/contract/test_state_persistence*.py`.
- [X] T015 Walk `quickstart.md` scenarios A–G; confirm each SC (SC-001…SC-006) has a covering test; verify observability/state carry no secret values and no new external dependency was added.
- [X] T016 Full regression (`.venv/bin/pytest tests/ -q`) + `.venv/bin/ruff check` on all edited files; fix any nits.
- [X] T017 A couple of adversarial review rounds (diverse-lens finders + refute-verify) on the guard before merge — focus: the conflict→no-dispatch path, defer-on-unknown, the no-clobber ordering vs check_inflight, anti-thrash reuse, per-card isolation, secret-free records, and that 047/096 are untouched.

---

## Dependencies & Execution Order

- **Phase 1 (Setup)** → **Phase 2 (decision helper)** blocks the stories (US tasks call `classify_pre_dispatch`).
- **US1 (P1)** = MVP (closes the livelock). Depends on Phase 2.
- **US2 (P2)** builds on the US1 guard (defer/ordering); proceed after US1.
- **US3 (P3)** adds thrash/isolation/observability to the US1/US2 guard.
- **Polish** last.

## Parallel Opportunities

- T013 / T014 [P] (independent regression suites).
- Within US1, T004 (node tests) authored in parallel with T006 (no-PR test) before T005 wires them.

## Implementation Strategy

MVP-first: ship **US1** (rebase-before-dispatch) — it alone closes the livelock that needed a manual operator rebase. Then **US2** (no-clobber/defer) and **US3** (thrash/isolation/observability). Each phase is an independently testable increment; 047's merge-time path and 096's check_board triggers are never modified; no persisted-state change.
