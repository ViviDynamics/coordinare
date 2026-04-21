# Tasks: Card Assignment Filtering

**Input**: Design documents from `/specs/050-card-assignment-filtering/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2)
- Include exact file paths in descriptions

---

## Phase 1: Setup

**Purpose**: Shared infrastructure — both user stories depend on these

- [x] T001 Add `assignee_filter: str | None = Field(default=None)` to `CoordinareConfig` in `src/coordinare/config.py` — place after the `priority` field
- [x] T002 Add `assignees(first: 10) { nodes { login } }` to the `... on Issue` content fragment in `POLL_BOARD_QUERY` in `src/coordinare/services/github.py`
- [x] T003 Parse `item_assignees: dict[str, list[str]]` in `poll_board()` in `src/coordinare/services/github.py` — lowercase-normalize logins, use empty list for cards with no assignees, add `"item_assignees": item_assignees` to the return dict

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core filtering logic required by US1 (US2 builds on top of it)

- [x] T004 Apply assignee filter in `check_board()` in `src/coordinare/graph/nodes/check_board.py` — after the advocate-label filter (~line 386), before priority sort: read `config.assignee_filter`, normalize to lowercase, filter `eligible_todo` to only items where `filter_login in board.get("item_assignees", {}).get(item_id, [])`, log `check_board.assignee_filtered` at INFO with `skipped` count and `assignee_filter` value
- [x] T005 Write unit tests for assignee filter logic in `tests/unit/test_check_board.py` — four scenarios: (a) filter set + card assigned to filter login → dispatched, (b) filter set + card assigned to different login → skipped, (c) filter set + card with no assignees → skipped, (d) no filter set + any card → dispatched normally

**Checkpoint**: `check_board` filters TODO cards by assignee when `assignee_filter` is configured; all four test scenarios pass.

---

## Phase 3: User Story 1 — Coordinare only picks up assigned cards (Priority: P1)

**Goal**: When `assignee_filter` is set, only cards assigned to that login are dispatched.

**Independent Test**: Two TODO cards (one assigned to `coordinare-bot`, one to `human-engineer`), `assignee_filter: coordinare-bot` → only `coordinare-bot` card dispatched.

- [x] T006 [US1] Write unit tests for `assignee_filter` config field in `tests/unit/test_config.py` — verify: (a) `assignee_filter: null` default, (b) `assignee_filter: "my-bot"` parses to string, (c) `COORDINARE_ASSIGNEE_FILTER=my-bot` env var sets the field
- [x] T007 [US1] Write unit tests for `item_assignees` parsing in `tests/unit/test_github_service.py` — verify: (a) issue with assignees returns lowercase logins, (b) issue with no assignees returns empty list, (c) non-Issue content (DraftIssue) returns empty list

**Checkpoint**: `assignee_filter` config parses correctly; `item_assignees` populated from GitHub query; TODO cards filtered correctly; all unit tests pass.

---

## Phase 4: User Story 2 — Dashboard shows filter status (Priority: P2)

**Goal**: Dashboard idle state shows which assignee filter is active.

**Independent Test**: Set `assignee_filter: coordinare-bot`, open dashboard → idle state shows "Filter: coordinare-bot".

- [x] T008 [US2] Add `"assignee_filter": getattr(config, "assignee_filter", None)` to the SSE snapshot return dict in `DashboardStore.build_snapshot()` in `src/coordinare/dashboard.py`
- [x] T009 [US2] In `renderActivePerformers(s)` JS in `src/coordinare/dashboard.py` — when `activeSessions.length === 0`, append `s.assignee_filter ? ' &nbsp;&#183;&nbsp; Filter: ' + esc(s.assignee_filter) : ''` to the idle-state message
- [x] T010 [US2] Write unit tests for `assignee_filter` in snapshot in `tests/unit/test_dashboard.py` — verify: (a) snapshot includes `assignee_filter: "my-bot"` when configured, (b) snapshot includes `assignee_filter: null` when not configured

**Checkpoint**: Dashboard idle state shows filter hint when `assignee_filter` is set; hidden when not set; snapshot tests pass.

---

## Phase 5: Polish & Cross-Cutting Concerns

- [x] T011 Run `.venv/bin/pytest tests/unit/test_check_board.py tests/unit/test_config.py tests/unit/test_dashboard.py tests/unit/test_github_service.py -q` — all tests pass
- [x] T012 Run `.venv/bin/ruff check src/coordinare/config.py src/coordinare/services/github.py src/coordinare/graph/nodes/check_board.py src/coordinare/dashboard.py` — lint clean
- [x] T013 Run `.venv/bin/pytest --cov=src/coordinare --cov-report=term-missing -q` — coverage does not regress

---

## Dependencies & Execution Order

- **Phase 1 (Setup)**: No dependencies — config + query + parsing (T001–T003 can run in parallel across different files)
- **Phase 2 (Foundational)**: Depends on Phase 1 — filter logic uses `config.assignee_filter` and `board["item_assignees"]`
- **Phase 3 (US1)**: Depends on Phase 2 — unit tests validate the filtering behaviour
- **Phase 4 (US2)**: Depends on T001 (needs `assignee_filter` on config) — independent of US1 content
- **Phase 5 (Polish)**: Depends on all prior phases

## Implementation Strategy

1. Complete Phase 1 (T001–T003): config field + query extension + parser
2. Complete Phase 2 (T004–T005): filter logic + unit tests
3. **Validate**: Run `pytest tests/unit/test_check_board.py -q` — all 4 scenarios pass
4. Complete Phase 3 (T006–T007): config + github service unit tests
5. Complete Phase 4 (T008–T010): dashboard snapshot + JS + tests
6. Phase 5 (T011–T013): full test suite + lint + coverage check

## Notes

- All changes are in existing files — no new Python files needed
- `item_assignees` lives in the board dict (same level as `item_labels`) — NOT in `board_snapshot`
- Assignee comparison is case-insensitive (normalize both sides to lowercase)
- `assignee_filter` default is `None` (falsy) — when not set, no filtering occurs (backward-compatible)
- The `POLL_BOARD_QUERY` change requires verifying the GraphQL fragment only applies to Issue nodes (DraftIssue has no assignees — missing field is safe, returns empty list from parser)
