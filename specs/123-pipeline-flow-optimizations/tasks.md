# Tasks: Coordinare Pipeline Flow Optimizations

**Input**: Design documents from `specs/123-pipeline-flow-optimizations/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, quickstart.md ✓

**Organization**: Tasks are grouped by user story to enable independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to
- Exact file paths are included in each description

---

## Phase 1: Setup

**Purpose**: No new project initialization needed — this feature extends existing modules only.

- [X] T001 Verify branch `123-pipeline-flow-optimizations` is checked out and clean

---

## Phase 2: Foundational — PersistedSession State Model

**Purpose**: Add three new fields to `PersistedSession` with defaults and legacy migration. ALL subsequent phases depend on these fields being present.

**⚠️ CRITICAL**: No US3/US4 work can begin until this phase is complete. US1/US2/US6/US7 are persona/logic changes independent of this phase.

- [X] T002 Add `content_feedback_cycles: int = 0`, `transient_error_cycles: int = 0`, and `open_questions: list[dict] = Field(default_factory=list)` fields to `PersistedSession` in `src/coordinare/graph/state.py`
- [X] T003 Add `model_validator(mode="before")` to `PersistedSession` in `src/coordinare/graph/state.py` that migrates legacy `feedback_cycle_count` value into `content_feedback_cycles` when the new field is absent or 0
- [X] T004 Write unit tests for the three new fields (defaults) and the legacy migration in `tests/unit/graph/state/test_persisted_session.py`

**Checkpoint**: `PersistedSession` has all new fields. `pytest tests/unit/graph/state/` passes.

---

## Phase 3: US1 — Tech_writer doc-change gate (Priority: P1) 🎯 MVP

**Goal**: Skip the documenting stage when the PR diff contains no `docs/` file changes.

**Independent Test**: Given a PR whose diff has no `docs/` paths, when QA passes and coordinare evaluates the documenting stage, then tech_writer is NOT dispatched, the stage is logged as `stage_skipped: reason=no_doc_changes`, and the card advances.

- [X] T005 [US1] Add helper `_should_skip_documenting(changed_files: list[str]) -> bool` (returns `True` when no path starts with `docs/`) in `src/coordinare/graph/nodes/dispatch_performer.py`
- [X] T006 [US1] In the documenting-stage dispatch path in `src/coordinare/graph/nodes/dispatch_performer.py`, call `_should_skip_documenting(changed_files)` before dispatching; if True, emit a structured log event `stage_skipped: reason=no_doc_changes` and advance card to the next stage without dispatching
- [X] T007 [US1] Write unit tests covering: (a) `_should_skip_documenting` returns True for non-doc diffs, (b) False when a `docs/` path is present, (c) documenting stage advances without dispatch when helper returns True in `tests/unit/graph/nodes/test_dispatch_performer.py`

---

## Phase 4: US2 — QA persona: acceptance-criteria only (Priority: P2)

**Goal**: Remove linting/test-coverage instructions from the QA persona so QA focuses exclusively on acceptance criteria verification and visual evidence.

**Independent Test**: Given a PR with a linting warning reviewer did not flag, when QA runs, QA does NOT return `changes_requested` for the lint issue.

- [X] T008 [P] [US2] Edit the QA performer persona in `src/coordinare/services/persona_service.py` (around lines 425-522): remove all instructions to run linters, check test coverage percentages, or report code style issues; retain acceptance-criteria verification and screenshot/visual-evidence capture instructions
- [X] T009 [P] [US2] Write a smoke test in `tests/unit/services/test_persona_service.py` asserting: (a) QA persona does NOT contain linting/coverage substrings (e.g., `"lint"`, `"coverage"`, `"code style"`) — FR-004; AND (b) QA persona DOES contain acceptance-criteria language (e.g., `"acceptance criteria"`) and visual-evidence language (e.g., `"screenshot"`) — FR-005

---

## Phase 5: US3 — Split bounce budget (Priority: P3)

**Prerequisites**: Phase 2 (T002-T004) complete — new state fields must exist.

**Goal**: Route `changes_requested` increments to `content_feedback_cycles` and infra-failure increments to `transient_error_cycles`, each with independent exhaustion checks.

**Independent Test**: Given a card with `env_blocked` 3 times and `changes_requested` 2 times, bounce budget evaluation does NOT block on content grounds (`content_feedback_cycles` = 2/5).

- [X] T010 [US3] Refactor `_feedback_cycle_exhausted()` (or equivalent) in `src/coordinare/graph/nodes/monitor_performer.py` to branch on failure type: `changes_requested` → increment `session.content_feedback_cycles` and check against `config.max_feedback_cycles` (default 5); `env_blocked`/`system_error`/`unknown` → increment `session.transient_error_cycles` and check against limit 3; ensure legacy `feedback_cycle_count` field is still written for backward compat if needed
- [X] T011 [US3] Write unit tests for split budget logic: content exhaustion at 5 cycles → `blocked`/`feedback_cycle_limit`; infra exhaustion at 3 cycles → spec-095 ENV_BLOCKED path; mixed counters don't cross-trigger in `tests/unit/graph/nodes/test_monitor_performer.py`

---

## Phase 6: US4 — Assessor Q&A carryover (Priority: P4)

**Prerequisites**: Phase 2 (T002-T004) complete — `open_questions` field must exist.

**Goal**: Persist answered `open_questions` from assessor result; inject as `prior_clarifications` on assessor re-dispatch.

**Independent Test**: Given a card with persisted `open_questions`, when assessor is re-dispatched, `prior_clarifications` in the card context payload contains the prior Q&A.

- [X] T012 [US4] In `src/coordinare/graph/nodes/monitor_performer.py`, after a successful assessor performer result, extract `open_questions` from the performer report JSON and write to `session.open_questions`
- [X] T013 [US4] In `src/coordinare/graph/nodes/dispatch_performer.py`, when building the card context payload for an assessor dispatch, inject `prior_clarifications` from `session.open_questions` if the list is non-empty; omit the key entirely on first dispatch (empty list)
- [X] T014 [US4] Write unit tests: (a) `open_questions` persisted from assessor result, (b) `prior_clarifications` injected on re-dispatch when non-empty, (c) `prior_clarifications` absent on first dispatch in `tests/unit/graph/nodes/test_dispatch_performer.py` and `tests/unit/graph/nodes/test_monitor_performer.py`

---

## Phase 7: US5 — Multi-concern feedback routes through assessor (Priority: P5)

**Goal**: When reviewer `changes_requested` feedback spans 2+ distinct concern categories, route card to assessing before implementing.

**Independent Test**: Given reviewer feedback classified as `[architecture, implementation]`, coordinare routes card to `assessing` not `implementing`.

- [X] T015 [US5] In `src/coordinare/graph/nodes/dispatch_performer.py`, after a reviewer returns `changes_requested`, read the concern categories from the card's already-classified review comments (the `check_board` node writes category labels onto `session.review_comments` or equivalent card-state field after `_classify_with_ai()` completes — no new AI call at dispatch time); if `len(set(categories)) >= 2`, route to `assessing` with the feedback injected as context; otherwise route to `implementing` as today
- [X] T016 [US5] Write unit tests: (a) 2+ distinct categories → route to assessing, (b) single category → route to implementing, (c) no classifiable categories → route to implementing (safe default) in `tests/unit/graph/nodes/test_dispatch_performer.py`

---

## Phase 8: US6 — Dedup comments before AI classification (Priority: P6)

**Goal**: Filter `processed_comment_ids` before calling `_classify_with_ai()` so already-classified comments are never re-classified.

**Independent Test**: Given 3 already-processed comments and 2 new ones, `_classify_with_ai()` is called exactly 2 times.

- [X] T017 [P] [US6] In `src/coordinare/graph/nodes/check_board.py`, move the `processed_issue_comment_ids` dedup filter to before the `_classify_with_ai()` call; if the filtered list is empty after dedup, skip the AI call entirely and complete the classification step as a no-op
- [X] T018 [P] [US6] Write unit tests: (a) 3 processed + 2 new → `_classify_with_ai` called 2 times, (b) all processed → called 0 times, (c) all new → called N times (no regression) in `tests/unit/graph/nodes/test_check_board.py`

---

## Phase 9: US7 — Closer as lightweight thread-resolution verifier (Priority: P7)

**Goal**: Rewrite the closing_review persona to thread-resolution-only — no diff re-review, no linting.

**Independent Test**: Given all reviewer threads resolved and CI passing, closer returns `approved` without inspecting code quality.

- [X] T019 [P] [US7] Rewrite the closing_review performer persona in `src/coordinare/services/persona_service.py` (around lines 524-574): remove instructions to re-examine code quality, re-run linters, or repeat diff review; retain only: verify open reviewer threads are resolved, confirm CI is passing, return `approved` or `changes_requested` citing unresolved threads
- [X] T020 [P] [US7] Write a smoke test asserting the closer persona string does NOT contain `"lint"`, `"code quality"`, `"diff review"` substrings and DOES contain thread-resolution language in `tests/unit/services/test_persona_service.py`

---

## Final Phase: Polish & Cross-Cutting Concerns

- [X] T021 Run full test suite `.venv/bin/pytest tests/unit/` and confirm all tests pass with no regressions
- [X] T022 Run `.venv/bin/ruff check src/coordinare/graph/nodes/dispatch_performer.py src/coordinare/graph/nodes/monitor_performer.py src/coordinare/graph/nodes/check_board.py src/coordinare/graph/state.py src/coordinare/services/persona_service.py` and fix any lint errors
- [X] T023 Verify `coordinare.state.json` backward-compatibility by running the legacy migration test (`pytest tests/unit/graph/state/test_persisted_session.py -v`) on a state fixture that has `feedback_cycle_count` but no `content_feedback_cycles`

---

## Dependencies

All US phases are independently parallelizable EXCEPT:
- **US3 (Phase 5) and US4 (Phase 6)** require Phase 2 complete first (new state fields)
- **US2, US6, US7** (T008-T009, T017-T020) are fully independent of Phase 2 and can run any time
- **US1 (Phase 3)** is independent of Phase 2

Recommended parallel execution:
- Batch A (no prerequisites): US1 + US2 + US6 + US7 → T005-T009, T017-T020
- Batch B (after Phase 2): US3 + US4 + US5 → T010-T016

## Implementation Strategy

**MVP**: Phase 2 + US1 (T001-T007) — state model + doc gate. Delivers the highest-frequency win immediately.

**Full delivery order**: Phase 1 → Phase 2 → [Phase 3 + Phase 4 + Phase 8 + Phase 9 in parallel] → [Phase 5 + Phase 6] → Phase 7 → Final Phase

## Parallel Execution Examples

```bash
# After Phase 2 completes — run US2/US6/US7 in parallel:
# Agent A: T008-T009 (persona_service.py QA edit + test)
# Agent B: T017-T018 (check_board.py dedup + test)
# Agent C: T019-T020 (persona_service.py closer edit + test)
# Agent D: T010-T011 (monitor_performer.py split budget + test)
```

---

## Implementation Notes / Deviations (2026-07-01)

All 23 tasks complete. Deviations from the task text, made to match the actual codebase (the task/quickstart file+symbol references were written against an imagined structure):

- **PersistedSession lives in `src/coordinare/state_store.py`**, not `graph/state.py`. State-model changes (T002–T004) landed there (fields + `model_validator` legacy migration + `CURRENT_SCHEMA_VERSION` 11→12). The three fields also round-trip through `CardSession`/`_SESSION_FIELDS` (`session.py`), the flat `CoordinareState` (`graph/state.py`), and daemon persist/restore (`daemon.py`). T004 tests are in `tests/unit/test_state_store.py`; the v11→v12 migration contract test is `tests/contract/test_state_persistence_v11_to_v12.py`. The JSON schema (`specs/003-state-persistence/contracts/workflow-snapshot.schema.json`) was bumped to allow v12 + the new fields.
- **US4 field renamed to `assessor_open_questions`** (not `open_questions`): `PersistedSession.open_questions: list[str]` already exists (~40 sites: blocked-card diagnostics + dashboard + daemon). Overloading it would break that surface, so US4 uses a distinct `assessor_open_questions: list[dict]` field (confirmed with the user). The dispatch payload key is still `prior_clarifications` (per contract). Assessor questions surface on the assessor *blocked-for-clarification* path (`status["questions"]`), so T012 persists there (monitor_performer), scoped to `stage == "assessing"`.
- **US3 counters:** `content_feedback_cycles` is now the persisted source of truth in `_feedback_cycle_exhausted` (reads `max()` of it and the non-persisted legacy `feedback_cycle_count`, writes both in lock-step). `transient_error_cycles` (limit 3) is incremented per infra-failure *episode* in `handle_system_error` (distinct from the per-dispatch `system_error_count` retry budget); at the limit the card surfaces as ENV_BLOCKED (`pattern_id=transient_error_budget_exhausted`). Both reset on operator un-block (check_board).
- **US5 routing** lives in `monitor_performer.py` (the reviewer `changes_requested` path), not `dispatch_performer.py` — concern categories are not persisted, so it classifies the reviewer comments at that point via the existing keyword classifier `classify_feedback_concerns` (NO new AI call, FR-012). Gated on `stage == "reviewing"` and `assessing` being in the lifecycle.
- **US6** lives in `route_issue_comments.py` (the real owner of `processed_issue_comment_ids`), not `check_board.py`; the quickstart's `check_board._process_review_comments` / `_classify_with_ai` do not exist. Dedup now pre-filters before the AI classifier and skips the call entirely when all comments are already processed.
- **FR-003 (content-hash doc dedup) deferred** per the checklist gate: the existing `utils/doc_dedup.py` does markdown section-overlap merging, not content-hash-vs-last-commit dedup — not straightforward, so US1 ships FR-001/FR-002 (the `docs/` path gate) only.
- **Bundled fix:** the uncommitted spec-120 QA-persona edit had left `tests/unit/services/test_qa_persona_visual.py::test_qa_persona_permits_any_browser_tooling` failing (it asserted the old "any tooling" wording). Updated that test to the current prescriptive-Playwright reality.
