# Tasks: Performer Event Stream

**Input**: Design documents from `/specs/014-performer-event-stream/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, contracts/ ✓, quickstart.md ✓

> **Key context**: A codebase audit during planning confirmed that the vast majority
> of this feature is already implemented and tested (98%/94% coverage). All three
> backends, the drain protocol, coordinare accumulation, and the dashboard activity
> feed are complete. The only remaining implementation is **secret redaction** (US4 /
> FR-010) and targeted coverage improvements.

**Organization**: Tasks are grouped by user story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no shared dependencies)
- **[Story]**: Which user story this task belongs to (US1–US4)

---

## Phase 1: Setup

**Purpose**: No new dependencies or scaffolding required — all infrastructure exists.
This phase is a no-op for 014; implementation begins at Phase 2.

*(No tasks — feature infrastructure already in place)*

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The one foundational element that all secrets-related tests depend on.

**⚠️ CRITICAL**: US4 implementation tasks must complete before US4 tests can be written.

- [X] T001 Add `_redact_secrets(text: str) -> str` module-level function with 7 compiled `re.Pattern` objects to `agent/performer/src/performer/models.py` (patterns: `github_pat_`, `ghp_`, `gho_`, `ghs_`, `sk-ant-`, `Bearer `, `AKIA`)

**Checkpoint**: Redaction function exists and importable — US4 story work can begin.

---

## Phase 3: User Story 4 — Secrets Never Surfaced in the Activity Feed (Priority: P1)

**Goal**: Any known secret pattern appearing in a `BackendEvent.detail` field is replaced with `[REDACTED]` before the event is stored or transmitted, ensuring credentials never reach the dashboard or structured logs.

**Independent Test**: Create a `BackendEvent` with a GitHub PAT in the `detail` field. Assert that `event.detail == "[REDACTED]"`. Repeat for a tool_use event with a Bearer token in `detail`. Confirm `text` field is NOT redacted.

- [X] T002 [US4] Add `model_validator(mode="before")` to `BackendEvent` in `agent/performer/src/performer/models.py` that calls `_redact_secrets()` on `data["detail"]` before storing; import `model_validator` from pydantic
- [X] T003 [US4] Write unit tests for `_redact_secrets()` in `agent/performer/tests/unit/test_models.py`: each of the 7 patterns matches and is replaced; non-matching strings pass through unchanged; function is idempotent; already-redacted `[REDACTED]` is not double-processed
- [X] T004 [US4] Write unit tests for `BackendEvent` redaction via model validator in `agent/performer/tests/unit/test_models.py`: constructing `BackendEvent(type=..., text=..., detail="ghp_abc...")` stores `[REDACTED]` in `detail`; `text` field is NOT redacted even if it contains a secret-like string; all 7 token types covered

**Checkpoint**: Secret redaction is implemented, validated, and covered. No credential can reach the event buffer unredacted.

---

## Phase 4: User Story 1 — Live Agent Activity in Dashboard (Priority: P1)

**Goal**: Operator sees a live activity feed in the dashboard during agent execution. *(Already implemented — this phase adds targeted coverage for the dashboard snapshot path.)*

**Independent Test**: Run the existing test suite: `cd agent/performer && .venv/bin/pytest tests/ -q`. Then run the coordinare tests: `.venv/bin/pytest tests/ -q`. Confirm activity feed HTML renders correctly.

- [X] T005 [P] [US1] Write unit tests for `build_state_snapshot()` performers-card path in `tests/unit/test_dashboard.py`: snapshot dict includes `performer_events` key; snapshot returns empty list when state has no events; snapshot returns list of event dicts when state has events
- [X] T006 [P] [US1] Write unit test for performers-card visibility logic in `tests/unit/test_dashboard.py`: verify the SSE `/events` endpoint returns `performer_events` in the JSON payload when the daemon state has events

**Checkpoint**: Dashboard activity feed coverage improved; `dashboard.py` coverage moves toward 90%.

---

## Phase 5: User Story 2 — Events Flow from opencode Backend (Priority: P1)

**Goal**: opencode backend captures SSE events and emits typed `BackendEvent` objects. *(Already fully implemented and tested at 97% coverage — verify no regressions after redaction addition.)*

**Independent Test**: `cd agent/performer && .venv/bin/pytest tests/unit/backends/test_opencode.py -q` — all tests pass.

- [X] T007 [US2] Run `cd agent/performer && .venv/bin/pytest tests/unit/backends/test_opencode.py -v` and confirm all opencode drain_events tests still pass after the model_validator redaction addition in T002

**Checkpoint**: opencode backend unaffected by redaction change. 97%+ coverage maintained.

---

## Phase 6: User Story 3 — Events Flow from Claude Code Backend (Priority: P2)

**Goal**: ClaudeCodeBackend launches `claude --output-format stream-json`, parses its event stream, and delivers typed events to the activity feed. *(Already fully implemented at 100% coverage — verify no regressions.)*

**Independent Test**: `cd agent/performer && .venv/bin/pytest tests/unit/backends/test_claude_code.py -q` — all tests pass.

- [X] T008 [US3] Run `cd agent/performer && .venv/bin/pytest tests/unit/backends/test_claude_code.py -v` and confirm all ClaudeCodeBackend tests still pass after redaction addition; verify at least one test covers a `detail` field that could contain a secret (tool name / tool id)

**Checkpoint**: ClaudeCodeBackend unaffected by redaction change. 100% coverage maintained.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T009 [P] Run full performer test suite and confirm coverage remains at or above 90%: `cd agent/performer && .venv/bin/pytest tests/ --cov=performer --cov-report=term-missing -q`
- [X] T010 [P] Run full coordinare test suite and confirm coverage remains at or above 90%: `.venv/bin/pytest tests/ --cov=coordinare --cov-report=term-missing -q`
- [X] T011 [P] Run ruff lint on changed files: `.venv/bin/ruff check agent/performer/src/performer/models.py tests/unit/test_dashboard.py agent/performer/tests/unit/test_models.py`

---

## Dependencies & Execution Order

### Phase Dependencies

```
Phase 2 (Foundational — T001: _redact_secrets function)
    └── Phase 3 (US4 — T002: model_validator, T003–T004: tests)
            ├── Phase 4 (US1 — T005–T006: dashboard coverage)  [parallel]
            ├── Phase 5 (US2 — T007: opencode regression check) [parallel]
            └── Phase 6 (US3 — T008: claude_code regression)   [parallel]
                        └── Phase 7 (Polish — T009–T011)
```

### User Story Dependencies

- **US4 (P1 — Secrets)**: Depends on T001. The core remaining implementation task.
- **US1 (P1 — Dashboard)**: Depends on US4 completing (redaction must be in place before coverage tests include secret-bearing events)
- **US2 (P1 — opencode)**: Depends on US4 completing (regression check post-redaction)
- **US3 (P2 — ClaudeCode)**: Depends on US4 completing (regression check post-redaction)
- **Polish**: Depends on all stories complete

### Within Phase 3 (US4)

- T002 (model_validator) must follow T001 (`_redact_secrets` must exist before the validator calls it)
- T003 and T004 can run in parallel (both in `test_models.py` but different test functions)

---

## Parallel Opportunities

### After Phase 2 completes (T001), Phase 3 tasks run sequentially, then Phases 4–6 can run in parallel:

```bash
# After T001 + T002 + T003 + T004 complete (Phase 3):

# Launch in parallel:
Task T005: dashboard snapshot tests
Task T006: dashboard SSE endpoint test
Task T007: opencode regression check
Task T008: claude_code regression check
```

### Phase 7 tasks all parallel:

```bash
Task T009: performer coverage check
Task T010: coordinare coverage check
Task T011: ruff lint
```

---

## Implementation Strategy

### MVP (minimum to ship the only missing requirement)

1. Complete Phase 2: T001 — `_redact_secrets()` function
2. Complete Phase 3: T002–T004 — `model_validator` + tests
3. **STOP AND VALIDATE**: `BackendEvent.detail` redaction works; 292 tests still pass
4. This alone satisfies FR-010 (the only unimplemented functional requirement)

### Full Delivery

1. MVP (Phases 2–3)
2. Phase 4 — dashboard coverage (US1)
3. Phases 5–6 — regression verification (US2, US3) — run in parallel with Phase 4
4. Phase 7 — Polish

---

## Notes

- [P] tasks can run in parallel (different files, no completion dependencies)
- T007 and T008 are validation tasks (run tests, not write code) — they confirm the redaction addition didn't break existing backends
- All test files already exist; T003–T006 ADD new test functions to existing files
- `re` module is already imported in `models.py` — no new import needed for patterns
- `model_validator` import from pydantic IS a new import (currently only `BaseModel`, `Field`, `field_validator` are imported)
