# Tasks: Architect Performer

**Input**: Design documents from `/specs/020-architect-performer/`
**Prerequisites**: plan.md (required), spec.md (required for user stories)

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Config extensions and model additions that all user stories depend on

- [X] T001 Add `PLAN_FILE_PATH: str = "docs/coordinare-architecture.md"` and `PLAN_PAYLOAD_MAX_BYTES: int = 32_768` to `agent/performer/src/performer/config.py`
- [X] T002 Add `"plan_committed"` to the status literals in `agent/performer/src/performer/models.py`; add `plan_path: str | None = None` and `role: str = "implementing"` fields to the `Performance` dataclass
- [X] T003 Write unit tests in `agent/performer/tests/unit/test_models.py` (or extend existing): verify `plan_committed` is a valid status; verify `Performance` defaults for `plan_path` and `role`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Git helper and protocol support that MUST complete before user stories

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T004 Implement `commit_file(stand, path, content, message)` async helper in `agent/performer/src/performer/workspace.py`: write content to file, `git add`, `git diff --cached --quiet` to check for changes, `git commit` if changed, `git push origin <branch>`; overwrite if file exists (FR-009)
- [X] T005 Write unit tests for `commit_file` in `agent/performer/tests/unit/test_workspace.py` (extend existing): test new file is committed and pushed; test existing file is overwritten; test no-op when content is identical (idempotent); test parent directories are created

**Checkpoint**: Git commit helper ready. `plan_committed` status available in models.

---

## Phase 3: User Story 1 — Produce an Architecture Plan (Priority: P1) 🎯 MVP

**Goal**: Architect performer analyses codebase and commits a structured plan to the feature branch. Returns `plan_committed` with the file path.

**Independent Test**: Dispatch the architect performer with a card and repo, verify a plan file is committed containing all required sections, and status is `plan_committed`.

### Implementation for User Story 1

- [X] T006 [US1] Modify `agent/performer/src/performer/main.py`: in the `handle_status` flow where `backend_status.state == "done"`, add an architect branch gated on `perf.role == "architecting"` that calls `commit_file(perf.stand, settings.PLAN_FILE_PATH, backend_status.output, "chore: add architecture plan")`, sets `perf.plan_path` and `perf.state = "plan_committed"`, and returns `PerformerResponse(status="plan_committed", plan_path=plan_path, session_id=perf.session_id)` (FR-003, FR-005)
- [X] T007 [US1] Modify `agent/performer/src/performer/main.py`: in the `dispatch` action handler, read `role` from the dispatch payload and set `perf.role = payload.get("role", "implementing")` so the architect branch can gate on it
- [X] T008 [US1] Add architecture plan path injection to `src/coordinare/graph/nodes/dispatch_performer.py`: when building a dispatch payload for any role after the architect, check if the card has a `plan_path` (set by the `plan_committed` status); include its branch-relative path as `architecture_plan_path` in the payload (FR-007); downstream performers read the plan content directly from the branch
- [X] T009 [P] [US1] Write unit tests in `agent/performer/tests/unit/test_main.py` (extend): test architect role produces `plan_committed` status; test plan file is committed to the correct path; test `plan_path` is included in the response; test implementer role still produces `pr_opened` (no regression); verify the committed plan content contains the required section headings (Overview, Data Model, API Contracts, Component Breakdown, Implementation Approach) per FR-004
- [X] T010 [P] [US1] Write unit tests in `tests/unit/graph/nodes/test_dispatch_performer.py` (extend): test that `architecture_plan` field is present in dispatch payload when a plan file exists on the card; test payload size guard (inline vs path reference)
- [X] T011 [US1] Run full test suite: `.venv/bin/pytest tests/unit/ -q` and `cd agent/performer && .venv/bin/pytest tests/unit/ -q` — confirm no regressions

**Checkpoint**: US1 complete. Architect performer produces and commits a plan. Downstream roles receive the plan content. SC-001 and SC-002 verified.

---

## Phase 4: User Story 2 — Block on Insufficient Context (Priority: P2)

**Goal**: Architect performer returns `blocked` with specific questions when the card context is insufficient to produce a plan.

**Independent Test**: Dispatch the architect with an ambiguous card, verify it returns `blocked` with at least one concrete question.

### Implementation for User Story 2

- [X] T012 [US2] Verify that the existing `blocked` status handling in `agent/performer/src/performer/main.py` works for the architect role — the backend returns `blocked` with questions when it cannot produce a plan; no new code needed if the existing blocked path is role-agnostic
- [X] T013 [US2] Write unit test in `agent/performer/tests/unit/test_main.py` (extend): test architect role backend returns `blocked` with questions → performer returns `blocked` status with questions array (SC-004)
- [X] T014 [US2] Write unit test: verify `relay_feedback` action resumes the architect session with the human's answers, and the backend can then produce a plan

**Checkpoint**: US2 complete. Architect blocks on ambiguity and resumes after feedback. SC-004 verified.

---

## Phase 5: User Story 3 — Iterative Plan Refinement via Feedback (Priority: P3)

**Goal**: When architectural concerns are routed back from downstream roles, the architect overwrites the existing plan and the lifecycle re-runs affected roles.

**Independent Test**: Dispatch the architect with relay_feedback containing an architecture concern, verify the plan file is overwritten on the branch.

### Implementation for User Story 3

- [X] T015 [US3] Verify that `commit_file` overwrites an existing plan file (covered by T004/T005 — the helper already overwrites); confirm `relay_feedback` is forwarded to the architect backend so it can incorporate review comments
- [X] T016 [US3] Write unit test in `agent/performer/tests/unit/test_main.py` (extend): test architect re-dispatch with `relay_feedback` → backend incorporates feedback → plan file is overwritten → returns `plan_committed` (SC-003)
- [X] T017 [US3] Write integration-style test verifying the full cycle: architect commits plan → implementer runs → reviewer flags architecture concern → `classify_human_feedback` routes to architect → architect updates plan → lifecycle re-runs from implementer

**Checkpoint**: US3 complete. SC-003 verified — plan is overwritten, not duplicated.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T018 Run full test suite: `.venv/bin/pytest tests/unit/ -q` and `cd agent/performer && .venv/bin/pytest tests/unit/ -q` — confirm all pass and coverage does not decrease
- [X] T019 Run linter: `.venv/bin/ruff check agent/performer/src/performer/main.py agent/performer/src/performer/models.py agent/performer/src/performer/workspace.py agent/performer/src/performer/config.py` — zero warnings
- [X] T020 Verify backward compatibility: existing implementer performer produces `pr_opened` when `role != "architecting"` — no behavioral change for non-architect roles
- [X] T021 Verify `plan_committed` is in `TERMINAL_SUCCESS_STATES` in `src/coordinare/graph/nodes/monitor_performer.py` (already added in 019 — confirm it's present)

---

## Edge Cases (scoping decision)

The following edge cases from spec.md are **in-scope** and covered by existing tasks:

- **Greenfield project (no existing code)**: The AI backend handles this; the performer just commits whatever it produces
- **Plan file already exists (re-run)**: T004/T005 — `commit_file` overwrites by design (FR-009)

The following are **deferred** to future work:

- **Plan file too large for payload**: T008 handles the size guard; reading from branch is the fallback
- **Commit fails due to merge conflict**: Existing performer error handling catches git failures
- **Architect plan contradicts assessor clarifications**: Not addressable by code — depends on AI backend quality

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup (Phase 1) — BLOCKS user stories
- **US1 (Phase 3)**: Depends on Foundational (Phase 2) — core MVP
- **US2 (Phase 4)**: Depends on Foundational (Phase 2) — can parallel with US1
- **US3 (Phase 5)**: Depends on US1 (Phase 3) — needs plan commit working
- **Polish (Phase 6)**: Depends on all user stories

### Parallel Opportunities

- T009 and T010 can run in parallel (different test files)
- US1 and US2 can proceed in parallel after Foundational

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (config + model additions)
2. Complete Phase 2: Foundational (commit_file helper)
3. Complete Phase 3: User Story 1 (architect role in main.py + coordinare payload injection)
4. **STOP and VALIDATE**: Verify plan file is committed and downstream roles receive it

### Notes

- Changes span two codebases: `agent/performer/` (performer side) and `src/coordinare/` (coordinare side)
- The performer side is the primary focus; coordinare changes are minimal (payload injection in dispatch_performer)
- `plan_committed` terminal state is already handled by 019's `monitor_performer` — no coordinare lifecycle changes needed
- All performer changes are behavioral variants within existing files, not new modules
