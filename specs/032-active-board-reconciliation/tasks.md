# Tasks: Active Board Reconciliation

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: US1 — Poll-Based Column Mismatch Detection (P1)

- [X] T001 [US1] Add `PHASE_TO_EXPECTED_COLUMN` mapping and `_find_card_column()` / `_reconcile_board_mismatch()` helpers to `src/coordinare/graph/nodes/monitor_performer.py`
- [X] T002 [US1] Add reconciliation check at top of `monitor_performer()` before status poll — compare actual vs expected column, take corrective action on mismatch
- [X] T003 [P] [US1] Write unit tests: consistent column → no action; backward move (TODO) → idle; forward move (DONE) → idle/complete; moved to BLOCKED → blocked; card not found → idle

---

## Phase 2: Polish

- [X] T004 Run full test suite and linter
- [X] T005 Verify backward compatibility (no board_snapshot → no reconciliation)
