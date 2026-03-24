# Tasks: Card Prioritization

**Input**: Design documents from `/specs/025-card-prioritization/`

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [X] T001 Add `PriorityConfig(BaseModel)` to `src/coordinare/config.py` with `field_name: str | None = None` and `priority_order: list[str] = Field(default_factory=list)`; add `priority: PriorityConfig = Field(default_factory=PriorityConfig)` to `ProjectConfiguration`
- [X] T002 Write config parsing tests in `tests/unit/test_config.py`: PriorityConfig defaults; config with priority_field set; config with priority_order list; missing priority key loads default

---

## Phase 2: Foundational

- [X] T003 Verify `poll_board` in `src/coordinare/services/github.py` returns custom field values for project items; if not, extend the GraphQL query to include the configured priority field and return values as `item_field_values: dict[str, dict[str, str]]` (mapping item_id → field_name → value) in the board dict
- [X] T004 Write unit test for `poll_board` field value extraction (mock GraphQL response)

---

## Phase 3: US1 — Respect Priority Field on Cards (P1) 🎯 MVP

**Goal**: Cards with higher priority (lower sort value) are selected first from TODO.

- [X] T005 [US1] Implement `_sort_by_priority(item_ids, item_field_values, field_name, priority_order) -> list[str]` helper in `src/coordinare/graph/nodes/check_board.py`: sort by field value using `priority_order` index if set, else lexicographic; null values sort last; stable sort preserves board position on ties
- [X] T006 [US1] Modify `check_board` in `src/coordinare/graph/nodes/check_board.py`: after advocate-label filtering, if `state.get("config")` has a priority field configured, read item field values from `board.get("item_field_values", {})`, call `_sort_by_priority`, and use the sorted list for card selection
- [X] T007 [P] [US1] Write unit tests in `tests/unit/graph/nodes/test_check_board.py`: three cards with P0/P1/P2 → P0 selected; card without priority sorts last; tied priority preserves board order; no priority config → board position order (backward compat)

---

## Phase 4: US2 — Configurable Priority Strategy (P2)

**Goal**: Operator defines the priority field name and optional semantic value ordering.

- [X] T008 [US2] Verify `PriorityConfig.priority_order` is wired through `_sort_by_priority`: when set, values rank by list index; unlisted values sort after listed ones
- [X] T009 [P] [US2] Write unit tests: custom priority_order ["Critical","High","Low"] → Critical selected over High; field not in project logs warning and falls back; numeric field values sorted correctly

---

## Phase 5: Polish

- [X] T010 Run full test suite and linter
- [X] T011 Verify backward compatibility: no priority config → identical behavior to pre-025
- [X] T012 Log warning when configured priority field is not found on any card

---

## Dependencies

- **Phase 1**: Start immediately
- **Phase 2**: Depends on Phase 1 (config model must exist)
- **US1 (Phase 3)**: Depends on Phase 2 (poll_board must return field values)
- **US2 (Phase 4)**: Depends on US1 (sort helper must exist)
- **Polish (Phase 5)**: Depends on all

## Implementation Strategy

MVP: Phases 1-3. Priority-aware card selection working with lexicographic sort.
Then Phase 4 adds custom ordering. Small, focused change — 2 source files + tests.
