# Tasks: Live Requirement Sync

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [X] T001 Add `RequirementChangePolicy` Literal type and `requirement_change_policy` field to `src/coordinare/config.py`
- [X] T002 Add `requirements_changed` and `requirements_changed_details` fields to `CoordinareState` in `src/coordinare/graph/state.py`

---

## Phase 2: US1 — Detect Requirement Changes (P1)

- [X] T003 [US1] In `monitor_performer`: re-fetch issue details, compare description against current_card, set requirements_changed flag and log warning
- [X] T004 [P] [US1] Write unit tests: description changed → warning logged + flag set; description unchanged → no flag; API failure → no crash

---

## Phase 3: US2 — Configurable Response (P2)

- [X] T005 [US2] In `monitor_performer`: on `re-dispatch` policy, update card with new description and reset to dispatching
- [X] T006 [P] [US2] Write unit tests: ignore policy → silent; warn → log; re-dispatch → new dispatch with updated card

---

## Phase 4: Polish

- [X] T007 Run full test suite and linter
- [X] T008 Verify backward compatibility (default warn policy)
