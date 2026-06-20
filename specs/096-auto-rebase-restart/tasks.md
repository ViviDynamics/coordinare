# Tasks: Auto-Rebase Restart Resilience

**Input**: Design documents from `/specs/096-auto-rebase-restart/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/rebase-triggers.md, quickstart.md

**Tests**: INCLUDED — Constitution II (Testing Discipline) is non-negotiable; this feature is TDD (failing test before implementation).

**Organization**: by user story (US1 P1 restart-heal → US2 P2 proactive-conflict → US3 P3 observability). The merge-time spec-047 path is reused unchanged.

## Path Conventions

Single project: `src/coordinare/`, `tests/` at repo root. Reused unchanged: `src/coordinare/services/rebase.py`.

---

## Phase 1: Setup

- [X] T001 Confirm injection points and current behavior by reading: `src/coordinare/graph/nodes/check_board.py` (the rebase block ~L440-491, the `prev_main is None → initialize without rebase` branch ~L461-463), `src/coordinare/daemon.py` (the top-level state-keys list ~L290 and the preflight rebase ~L1176-1217), `src/coordinare/state_store.py` (`WorkflowSnapshot` top-level fields, `PersistedSession`, `CURRENT_SCHEMA_VERSION`), `src/coordinare/session.py` (`_SESSION_FIELDS`, `CardSession`, `create_session_from_card`), and `src/coordinare/services/rebase.py` (`run_rebase_round`, `detect_stale_branches`, `rebase_branch`, `RebaseOutcome`). Note the exact lines each change lands.

---

## Phase 2: Foundational (persistence — blocks US1 & US2)

- [X] T002 Write FAILING contract test `tests/contract/test_state_persistence_v10_to_v11.py`: `CURRENT_SCHEMA_VERSION == 11`; a v10-shaped snapshot (no `last_known_main_sha`, no per-card `last_rebase_attempt`) loads with both defaulting to None; a populated `last_known_main_sha` + per-card `last_rebase_attempt` round-trip through `model_dump`/`model_validate` (FR-010). MUST fail before T003/T004.
- [X] T003 Add top-level persisted `last_known_main_sha: str | None = None` to `WorkflowSnapshot` in `src/coordinare/state_store.py`; bump `CURRENT_SCHEMA_VERSION` 10→11 with the doc-comment note; `MIN_SUPPORTED_SCHEMA_VERSION` unchanged; update the JSON schema enum in `specs/003-state-persistence/contracts/workflow-snapshot.schema.json` (add 11) and the v10 schema_version pin in `tests/contract/test_state_persistence.py`.
- [X] T004 [P] Add per-card `last_rebase_attempt: dict | None = None` to `PersistedSession` in `src/coordinare/state_store.py` (shape `{main_sha, head_sha, outcome}`); add `"last_rebase_attempt"` to `_SESSION_FIELDS` + `CardSession` + `create_session_from_card(...=None)` in `src/coordinare/session.py`; add it to the optional-in-initial set in `tests/unit/test_session.py`.
- [X] T005 Persist + restore `last_known_main_sha` in `src/coordinare/daemon.py`: include it when building `WorkflowSnapshot` and seed `state["last_known_main_sha"]` from the snapshot on restore (so it is present before the first `check_board` cycle); persist/restore the per-card `last_rebase_attempt` in `_persist_active_sessions` + the restore loop (sanitize like the 095 `env_blocked` field: keep only string-valued `main_sha`/`head_sha`/`outcome`, else None). Make T002 pass.

**Checkpoint**: snapshot round-trips both new fields; old snapshots load with defaults.

---

## Phase 3: User Story 1 — Restart-drift heal (Priority: P1) 🎯 MVP

**Goal**: after a restart where main advanced while down, the existing edge fires and stale branches rebase. **Independent test**: persisted baseline = old main, live = new main → `check_board` calls `run_rebase_round` on cycle 1.

- [X] T006 [US1] Write FAILING test in `tests/unit/graph/nodes/test_check_board_rebase.py`: with `state["last_known_main_sha"]` seeded to an OLD sha (restored from snapshot) and live main a DIFFERENT sha, the first `check_board` cycle invokes `run_rebase_round` (assert via a patched `run_rebase_round`) and updates `last_known_main_sha` to the new value. MUST fail before T007.
- [X] T007 [US1] Write FAILING test in the same file: when `last_known_main_sha` is None (fresh/post-upgrade) and live main exists, cycle 1 sets the baseline AND (per FR-003 hook) does not crash — a clean in-flight branch yields no rebase. (Guards the first-run path.)
- [X] T008 [US1] Modify the `check_board` rebase block in `src/coordinare/graph/nodes/check_board.py` so the first-cycle path uses the restored persisted baseline instead of silently re-baselining: when a persisted `last_known_main_sha` is present and differs from live main, take the existing `run_rebase_round` drift path (`reason=main_moved`/`restart_drift`). Make T006/T007 pass.
- [X] T009 [US1] Write + make pass a test in `tests/unit/test_daemon*.py` (e.g. `test_daemon_coverage.py`): `last_known_main_sha` survives a persist→restore round-trip via the daemon (snapshot write then `_restore_from_snapshot`).

**Checkpoint**: US1 independently testable — a restart after a merge triggers the rebase round.

---

## Phase 4: User Story 2 — Proactive conflicting-branch rebase (Priority: P2)

**Goal**: a CONFLICTING/BEHIND in-flight PR is rebased even with no observed edge; no churn/thrash; per-card isolation. **Independent test**: baseline == live main but a PR reports CONFLICTING → rebase initiated.

- [X] T010 [US2] Write FAILING tests in `tests/unit/graph/nodes/test_check_board_rebase.py`: (a) baseline == live main but an in-flight PR mergeability is CONFLICTING → `check_board` initiates a rebase for that card (`reason=proactive_conflict`); (b) PR mergeability CURRENT → no rebase; (c) PR mergeability UNKNOWN → deferred (no rebase, re-checked next cycle); (d) card with no open PR → skipped. MUST fail before T012.
- [X] T011 [US2] Write FAILING anti-thrash + isolation tests in `tests/unit/services/test_rebase_thrash.py`: a card whose `last_rebase_attempt` `(main_sha, head_sha)` matches the current head+main with outcome BLOCKED/FAILED is NOT re-attempted; once head or main changes it IS re-attempted; and a per-card FAILED rebase does not prevent other cards from being processed (FR-006/FR-007). MUST fail before T012.
- [X] T012 [US2] Implement the proactive trigger in `src/coordinare/graph/nodes/check_board.py`: independent of the main-moved edge, read each in-flight PR's mergeability via `github_service`; for CONFLICTING/BEHIND-and-not-thrash-guarded cards, run the rebase (via `run_rebase_round`/`rebase_branch`); defer on UNKNOWN; write the per-card `last_rebase_attempt` marker after each non-SKIPPED attempt; reuse `prepare_conflict_resolution` for BLOCKED jobs (mirror the existing path). Make T010/T011 pass.
- [X] T013 [US2] Add the thrash-eligibility helper (pure function) it depends on — e.g. `should_attempt_rebase(session, current_main_sha, head_sha) -> bool` in `src/coordinare/services/rebase.py` — and unit-test it directly in `tests/unit/services/test_rebase_thrash.py` (covers match→skip, head-changed→attempt, main-changed→attempt, no-prior→attempt).

**Checkpoint**: US2 independently testable — a conflicting branch heals with no edge, and steady state does no extra work.

---

## Phase 5: User Story 3 — Observability (Priority: P3)

**Goal**: every triggered rebase emits a secret-free `rebase.triggered` record. **Independent test**: trigger a rebase, assert the record fields + absence of secrets.

- [X] T014 [US3] Write FAILING test in `tests/unit/graph/nodes/test_check_board_rebase.py` (structlog capture): a triggered rebase emits `rebase.triggered` with `reason`, `card_id`, `branch`, `prev_main_sha`, `current_main_sha`, `outcome`; assert no secret-shaped values (token/password) appear in the record. MUST fail before T015.
- [X] T015 [US3] Emit the `rebase.triggered` structured record at each trigger site in `src/coordinare/graph/nodes/check_board.py` (reasons: `restart_drift` | `proactive_conflict` | `main_moved`), carrying only SHAs/branch/card-id/outcome (FR-008/FR-009). Keep the existing `RebaseRound` Slack/dashboard summary. Make T014 pass.

---

## Phase 6: Polish & Cross-Cutting

- [X] T016 [P] Regression: run the existing rebase + merge_pr + check_board suites together (`tests/unit/services/test_rebase*.py`, `tests/unit/graph/nodes/test_merge_pr*.py`, `tests/unit/graph/nodes/test_check_board*.py`) and confirm the merge-time 047 path is byte-identical in behavior (FR-011/SC-006).
- [X] T017 [P] Run the full snapshot-persistence suite (`tests/contract/test_state_persistence*.py`) to confirm the v11 bump round-trips v1–v10 snapshots.
- [X] T018 Walk `quickstart.md` scenarios A–F and confirm each SC (SC-001…SC-006) has a covering test; verify observability/state carry no secret values (FR-009) and no new external dependency was added.
- [X] T019 Full regression (`.venv/bin/pytest tests/ -q`) + `.venv/bin/ruff check` on all edited files; fix any nits.
- [X] T020 A couple of adversarial review rounds (diverse-lens finders + refute-verify) on the trigger logic before merge — focus: thrash/edge correctness, the first-cycle/None path, per-card isolation, UNKNOWN-defer, secret-free records, and that 047 is untouched.

---

## Dependencies & Execution Order

- **Phase 1 (Setup)** → **Phase 2 (Foundational persistence)** blocks everything (the triggers read/write the persisted fields).
- **US1 (P1)** depends on Phase 2 (needs the restored baseline). MVP = Setup + Foundational + US1.
- **US2 (P2)** depends on Phase 2 (needs the per-card marker) and is independent of US1 (different trigger condition); may proceed after Phase 2.
- **US3 (P3)** rides on the US1/US2 trigger sites.
- **Polish** last.

## Parallel Opportunities

- T004 [P] (per-card marker) parallel with T003 (top-level field) — different model areas.
- Within US2, T011/T013 (thrash unit) parallel with T010 (check_board mergeability) until T012 wires them.
- T016 / T017 [P] (independent regression suites).

## Implementation Strategy

MVP-first: ship **US1** (restart-drift heal) — it alone fixes the observed incident class. Then **US2** (proactive conflict) for robustness to missed edges / pre-baseline staleness, then **US3** (observability). Each phase is an independently testable increment; the merge-time 047 path is never modified.
