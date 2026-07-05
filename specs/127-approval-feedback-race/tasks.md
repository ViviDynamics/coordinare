# Tasks: Approval/Feedback Race — No Merge Over Unprocessed Feedback

**Input**: Design documents from `/specs/127-approval-feedback-race/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/review-routing.md, quickstart.md

**Tests**: TDD required (plan Constitution Check II) — every implementation task is preceded by a failing-test task pinned to the contract decision table.

**Organization**: Two user stories. US1 (precedence swap + deferral) is the MVP and is shippable alone; US2 (per-reviewer supersession) layers on top.

## Phase 1: Setup

- [x] T001 Confirm baseline: run `.venv/bin/pytest tests/unit -k "monitor_pr" -q` and record the passing count (regression reference for FR-006 invariants I1/I2)

## Phase 2: Foundational

- [x] T002 Create test scaffolding in `tests/unit/graph/nodes/test_monitor_pr_approval_race.py`: stub github service returning canned review batches (fields per contracts/review-routing.md Inputs), state factory with `human_reviewers`/`trusted_bot_reviewers`/`lifecycle_completed_at`/`processed_review_ids`, and a review-dict builder helper (id/author_login/state/submitted_at)

## Phase 3: User Story 1 — Feedback first, merge deferred (P1) 🎯 MVP

**Goal**: An approval coexisting with unprocessed actionable reviews defers the merge and relays the feedback; the approval is never consumed; approval-only and actionable-only batches are byte-identical to today.

**Independent Test**: contract rows 1–3 + invariants I1–I3 pass with supersession NOT yet implemented (raw batch = effective batch).

- [x] T003 [US1] Write failing tests in `tests/unit/graph/nodes/test_monitor_pr_approval_race.py` for contract row 1: human APPROVED + human CHANGES_REQUESTED in one batch → `phase == "relay_feedback"`, `pending_reviews` contains only the change request, `monitor_pr.merge_deferred` event emitted (assert via structlog capture or log call), and the approval review ID is NOT in `pending_reviews` (invariant I3)
- [x] T004 [US1] Write failing tests for row 1 variant: human APPROVED + trusted-bot COMMENTED → relay + deferral event (spec US1 scenario 2)
- [x] T005 [US1] Write regression tests for rows 2–3 and invariants I1/I2: approval-only batch → 090 base-gate consulted then `phase == "merging"`; actionable-only batch → `phase == "relay_feedback"`; empty effective batch → `phase == "monitoring_pr"`
- [x] T006 [US1] Write deferral round-trip test: cycle 1 defers (approval + CR); cycle 2 with CR's ID in `processed_review_ids` and approval still unprocessed → `phase == "merging"` with no re-approval (spec US1 scenario 3, FR-003)
- [x] T007 [US1] Implement the precedence swap in `src/coordinare/graph/nodes/monitor_pr.py` (evaluation block lines ~186–253): route `if actionable:` before `elif approved:`; keep the 090 `_evaluate_baseline_prevention_gate` call inside the approval branch unchanged; emit `monitor_pr.merge_deferred` `{card_id, approval_review_id, actionable_review_ids}` when an effective approval coexists with actionable reviews
- [x] T008 [US1] Run `.venv/bin/pytest tests/unit/graph/nodes/test_monitor_pr_approval_race.py tests/unit -k "monitor_pr" -q` — new tests green, zero regressions vs T001 count

**Checkpoint**: US1 shippable — the silent feedback drop is closed.

## Phase 4: User Story 2 — Latest review state per reviewer governs (P2)

**Goal**: Within one evaluation batch, a reviewer's later review supersedes their earlier one for both approval and deferral decisions; ties resolve to the actionable state.

**Independent Test**: contract supersession cases S1–S4 pass; US1 tests remain green (cross-reviewer batches are unaffected by grouping).

- [x] T009 [US2] Write failing tests in `tests/unit/graph/nodes/test_monitor_pr_approval_race.py` for S1 (CR then later APPROVED, same reviewer → merging), S2 (APPROVED then later CR, same reviewer → relay, no deferral event), S3 (timestamp tie → CR governs → relay), S4 (two reviewers → relay + deferral event), plus unparseable `submitted_at` on one side → actionable governs
- [x] T010 [US2] Implement `_latest_reviews_per_author(reviews) -> list[dict]` in `src/coordinare/graph/nodes/monitor_pr.py`: group post-filter reviews by `author_login` (empty login: no grouping), keep max by parsed `submitted_at` (reuse the module's `fromisoformat` idiom), conservative tie-break (actionable state beats APPROVED); wire it between the existing filters and the approval/actionable derivation
- [x] T011 [US2] Run `.venv/bin/pytest tests/unit/graph/nodes/test_monitor_pr_approval_race.py -q` — S1–S4 green, US1 tests still green

## Phase 5: Polish & Cross-Cutting

- [x] T012 [P] Run `.venv/bin/ruff check src/coordinare/graph/nodes/monitor_pr.py tests/unit/graph/nodes/test_monitor_pr_approval_race.py` — zero warnings
- [x] T013 Run the full unit suite `.venv/bin/pytest tests/unit -q` — no regressions anywhere (the evaluation block is shared code)
- [x] T014 Re-read contracts/review-routing.md against the implementation and tick each decision-table row/invariant against a named test (traceability pass)

## Dependencies

- T001 → T002 → US1 (T003–T008) → US2 (T009–T011) → Polish (T012–T014)
- US2 depends on US1's implementation landing first (same code block); the stories remain independently *testable* because US1's tests never construct same-reviewer multi-review batches.

## Parallel Example

- T003, T004, T005, T006 are [P]-eligible in principle (same file, so serialize edits in practice; they are independent test cases)
- T012 can run in parallel with T014.

## Implementation Strategy

MVP = Phase 3 (US1). The precedence swap alone closes the correctness hole (silent feedback drop). US2 refines fairness (a reviewer's own later approval isn't blocked by their superseded change request) and prevents over-deferral. Total scope: one node function + one pure helper + one test module.
