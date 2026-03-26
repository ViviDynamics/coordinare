# Tasks: Card Cancellation

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [X] T001 Add `card_cancelled` to `EventType` in `src/coordinare/models/notification.py`
- [X] T002 Create `src/coordinare/cancel.py` with `cancel_active_card(state, *, move_to_todo, cancel_timeout_seconds)` async helper

---

## Phase 2: US1 — Cancel via Dashboard API (P1)

- [X] T003 [US1] Add `POST /api/cancel` endpoint to `src/coordinare/dashboard.py`
- [X] T004 [P] [US1] Write unit tests: cancel while in progress → cancelled; cancel while idle → no_active_card

---

## Phase 3: US2 — Cancel via Board Column Detection (P2)

- [X] T005 [US2] In `src/coordinare/graph/nodes/check_board.py`: detect active card missing from known columns → call cancel_active_card
- [X] T006 [P] [US2] Write unit test: active card disappears from board → cancellation triggered

---

## Phase 4: Polish

- [X] T007 Run full test suite and linter
- [X] T008 Verify backward compatibility
