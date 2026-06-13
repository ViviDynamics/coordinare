---
description: "Task list for Implementer Local Test Gate (spec 089)"
---

# Tasks: Implementer Local Test Gate

**Input**: Design documents from `/specs/089-implementer-local-test-gate/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (all present)

**Tests**: INCLUDED. Constitution II (Testing Discipline) is NON-NEGOTIABLE — every behavioural task is preceded by a failing unit test. Env signals and `run_command` are mocked; no real subprocess in unit tests.

**Organization**: Grouped by user story. US1 + US2 are co-equal P1 (the gate is unsafe to ship without US2's env-blocked classification). US3 (P2) bounds and escalates the loop.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on incomplete tasks)
- **[Story]**: US1 / US2 / US3; Setup/Foundational/Polish carry no story label

## Path Conventions

Single project. Performer subpackage: `agent/performer/src/performer/`, tests `agent/performer/tests/unit/`. Coordinare: `src/coordinare/`, tests `tests/unit/`. Run: `.venv/bin/pytest`, `.venv/bin/ruff check`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish the SC-005 regression baseline before any change.

- [X] T001 Capture the disabled-by-default baseline: run `.venv/bin/pytest agent/performer/tests/unit/ tests/unit/ -q` and confirm green, so SC-005 ("byte-identical to pre-089") has a known-good reference.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Vocabulary (status + flag), config model, and persisted counter wiring that ALL stories depend on.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Tests (write first, must FAIL)

- [X] T002 [P] Test in tests/unit/ that a pre-089 `PersistedSession` snapshot (schema v7) loads with `local_fix_counter == {}` (v8 migration) and that the field round-trips through save/load.
- [X] T003 [P] Test in tests/unit/ for `LocalTestGateConfig`: defaults (`enabled=False`, `timeout_seconds=600`, `max_fix_attempts=2`), bounds rejected (`timeout_seconds` outside 60–7200, `max_fix_attempts` outside 0–20), and `extra="forbid"`.
- [X] T003a [P] Config-delivery test in tests/unit/ (dispatch) + agent/performer/tests/unit/ (protocol): (a) `dispatch_performer` injects `card_context["local_test_gate"] == {"enabled": ..., "timeout_seconds": ...}` for the `implementing` role when the gate is configured, and injects nothing (or `enabled=False`) when unconfigured; (b) the performer `Score` model (`agent/performer/src/performer/models.py`) preserves `local_test_gate` through `Score(**msg.payload)` (main.py:1218) rather than dropping it under `extra="ignore"`. (Guards the C1 cross-boundary gap — without this the gate no-ops in prod regardless of config.)

### Implementation

- [X] T004 [P] Add `"env_blocked"` to the terminal-status literal/sets used by `PerformerResponse.status` and the loop break-conditions, and add `local_test_failed: bool = False` to `PerformerResponse`, in agent/performer/src/performer/protocol.py.
- [X] T005 [P] Add `LocalTestGateConfig` (BaseModel, `extra="forbid"`, `enabled=False`, `timeout_seconds=Field(600, ge=60, le=7200)`, `max_fix_attempts=Field(2, ge=0, le=20)`) as a sibling of `CIGateConfig` and nest it on the same symphony/persona scope surface, in src/coordinare/config.py. (Makes T003 pass.)
- [X] T005a [P] Declare a `local_test_gate: dict | None = None` field on the performer `Score` model in agent/performer/src/performer/models.py (line ~98; sibling of `orchestration: dict | None`) so the dispatched gate config survives the `Score(**msg.payload)` construction (main.py:1218) instead of being silently dropped by the model's `extra="ignore"` (models.py:135). (Part of the C1 fix; makes T003a(b) pass.)
- [X] T008a Inject the gate config into the dispatch payload for the `implementing` role in src/coordinare/graph/nodes/dispatch_performer.py: resolve `LocalTestGateConfig` from the same scope as `CIGateConfig` and set `card_context["local_test_gate"] = {"enabled": <bool>, "timeout_seconds": <int>}` (omit / `enabled=False` when unconfigured). Mirror the existing per-role `card_context` injections (~lines 815–890). `max_fix_attempts` stays coordinare-only (consumed by T023, never sent to the performer). (Part of the C1 fix; makes T003a(a) pass.)
- [X] T006 Add `local_fix_counter: dict[str, int] = Field(default_factory=dict)` to `PersistedSession` and bump `CURRENT_SCHEMA_VERSION` to v8 (current is v7; v1–v7 load with `{}`), in src/coordinare/state_store.py. (Makes T002 pass.)
- [X] T007 Add `local_fix_counter` to the live session dict and the persisted-fields tuple in src/coordinare/session.py (mirror `bounce_counter`).
- [X] T008 Hydrate `local_fix_counter` from the snapshot and persist it in src/coordinare/daemon.py (mirror `bounce_counter` at ~lines 133 and ~637).

**Checkpoint**: status vocabulary, config, and persisted counter exist; T002/T003 green. User stories can begin.

---

## Phase 3: User Story 1 - Implementer catches its own test failures before push (Priority: P1) 🎯 MVP

**Goal**: After lint passes and before push, the implementer runs the detected `test_command`. Green (or no command / standalone) → push as today. Code-reason red → no push, `changes_requested` carrying the failing output.

**Independent Test**: With the gate enabled on a repo with a detectable `test_command`, hand the implementer a change that breaks a test → no PR opened, failing output surfaced as `changes_requested`.

### Tests (write first, must FAIL)

- [X] T009 [P] [US1] Helper test in agent/performer/tests/unit/: `_run_test_check` returns pass-through (`passed=True, command=None`) when the coordinare import raises `ImportError` (standalone), and skips (`passed=True, command=None`) when `detect().test_command is None`.
- [X] T010 [P] [US1] Helper test in agent/performer/tests/unit/: green run → `passed=True, command=<cmd>, duration>0`; non-zero exit with NO env signal → `passed=False, env_blocked=False, output=<tail>`.
- [X] T011 [P] [US1] Done-path test in agent/performer/tests/unit/: gate enabled, lint passes → `_run_test_check` runs after lint and before push; green → push + open PR (existing path); code-fail → no push, `status="changes_requested"` with failing tail and `local_test_failed=True`.

### Implementation

- [X] T012 [US1] Add the `LocalTestResult` model and the `async def _run_test_check(stand_path, *, timeout_seconds=600, label="performer") -> LocalTestResult` helper (mirror `_run_ci_check` ~line 100: `ImportError`→pass-through, `test_command is None`→skip, run via `run_command`, green/code-fail branches, output tail) in agent/performer/src/performer/main.py. (Makes T009/T010 pass.)
- [X] T013 [US1] Insert the gate in the implementer done-path after the lint check (~line 2815) and before push (~line 2818): read the delivered config via `perf.score.local_test_gate` (the field added in T005a) — when absent or `enabled` is falsy, skip the gate entirely (no `_run_test_check` call, no behavioural diff → SC-005); when enabled, call `_run_test_check(..., timeout_seconds=<delivered>)` (FR-010). Green/skip → existing push+PR path unchanged; code-fail → return `changes_requested` with output tail + `local_test_failed=True`, no push. (Makes T011 pass.)

**Checkpoint**: US1 functional — known-red code is never pushed; failing output surfaced locally.

---

## Phase 4: User Story 2 - Broken env-cache classified as env-blocked, not a code defect (Priority: P1)

**Goal**: A local test failure coinciding with a spec-088 env-cache signal becomes `env_blocked` (routed to a same-stage hold like `qa_env_blocked`), consuming 0 self-fix attempts and never re-dispatching the agent to fix fine code.

**Independent Test**: Simulate a recorded services-start / env-cache-health failure coinciding with a red test → outcome is env-blocked, `local_fix_counter` NOT incremented, agent NOT re-dispatched as `changes_requested`.

### Tests (write first, must FAIL)

- [X] T014 [P] [US2] Helper test in agent/performer/tests/unit/: code-fail + `consume_services_start_failure()` returns text → `env_blocked=True, env_reason` set; code-fail + `consume_env_cache_health_failure()` True → `env_blocked=True`; timeout + env signal → `env_blocked=True`; timeout, no signal → `passed=False, env_blocked=False`; green run + env signal present → `passed=True` (signal not consulted, push proceeds).
- [X] T015 [P] [US2] Done-path test in agent/performer/tests/unit/: helper returns `passed=False, env_blocked=True` → `PerformerResponse(status="env_blocked", reason=env_reason)`, no push, no `changes_requested`.
- [X] T016 [P] [US2] Coordinare test in tests/unit/: an `env_blocked` marker holds the card on the SAME stage — calls `mark_runtime_health_failed`, sets `env_health_hold_reason`, sets `phase="dispatching"`, does NOT advance, releases the slot, and leaves `local_fix_counter` unchanged.

### Implementation

- [X] T017 [US2] Add the env-blocked branch to `_run_test_check`: on the failure branch only and after the run completes, call `consume_services_start_failure()` / `consume_env_cache_health_failure()`; if either fired, return `env_blocked=True` with `env_reason`, in agent/performer/src/performer/main.py. (Makes T014 pass.)
- [X] T018 [US2] Done-path: when `_run_test_check` returns `env_blocked`, return `PerformerResponse(status="env_blocked", reason=env_reason)` (no push); add `"env_blocked"` to the poll-loop terminal-status tuples (~lines 3007, 3111) in agent/performer/src/performer/main.py. (Makes T015 pass.)
- [X] T019 [US2] Add `"env_blocked"` to `_terminal_markers` (~line 2060) and a route mirroring `qa_env_blocked` (~lines 2076–2102) placed before the terminal-success block — hold stage, `mark_runtime_health_failed`, `env_health_hold_reason`, `phase="dispatching"`, return without advancing — in src/coordinare/graph/nodes/monitor_performer.py. (Makes T016 pass.)

**Checkpoint**: US1 + US2 both work — env failures never masquerade as code defects.

---

## Phase 5: User Story 3 - Bounded self-fix loop with escalation (Priority: P2)

**Goal**: Code-reason failures increment a per-head `local_fix_counter` and re-dispatch while ≤ `max_fix_attempts`; once the budget is exhausted the card escalates to blocked with the failing output, never pushing. The counter is independent of spec-075's `bounce_counter`.

**Independent Test**: With small `max_fix_attempts=N`, hand the implementer an unfixable test failure → re-dispatch exactly N times, then escalate to blocked carrying the test output, never pushing.

### Tests (write first, must FAIL)

- [X] T020 [P] [US3] Coordinare test in tests/unit/: on implementer `changes_requested` with `local_test_failed=True`, `local_fix_counter[head]` increments and the implementer is re-dispatched while the count ≤ `max_fix_attempts`; a new head SHA resets the count.
- [X] T021 [P] [US3] Coordinare test in tests/unit/: when the count exceeds `max_fix_attempts`, the card routes to **blocked** with the failing test output as the reason — no re-dispatch, no push.
- [X] T022 [P] [US3] Coordinare test in tests/unit/: `local_fix_counter` and `bounce_counter` move independently — incrementing one never reads or writes the other (SC-004).

### Implementation

- [X] T023 [US3] On the implementer `changes_requested` path where `status.local_test_failed` is set: increment `local_fix_counter[head_sha]`, re-dispatch while `<= max_fix_attempts`, else route the card to **blocked** with the test output as reason (no push), in src/coordinare/graph/nodes/monitor_performer.py. (Makes T020/T021/T022 pass.)
- [X] T024 [US3] Amend `DEFAULT_INSTRUCTIONS["implementer"]` in src/coordinare/services/persona_service.py (~line 196) to direct the agent to run the detected `test_command` and fix failures within its own turn before declaring done (FR-011, in-session self-fix as the primary layer; the coordinare gate is the backstop). Keep the change additive so existing persona-override behaviour and `get_effective_instructions` fallback are unaffected.

**Checkpoint**: loop converges or fails loudly; budgets are separate.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T025 [P] Verify FR-013 observability: one structured event per decision (pass / code-fail-redispatch / env-blocked / escalate) carrying detected command, duration, attempt count, and classification — across `_run_test_check` (main.py) and the monitor routes (monitor_performer.py).
- [X] T026 [P] Regression + lint: `.venv/bin/pytest agent/performer/tests/unit/ tests/unit/ -q` green with config unset (SC-005 byte-identical), and `.venv/bin/ruff check agent/performer/src/performer/main.py agent/performer/src/performer/protocol.py src/coordinare/graph/nodes/dispatch_performer.py src/coordinare/services/persona_service.py src/coordinare/` clean.
- [X] T027 Run the quickstart.md end-to-end checks and confirm the SC mapping (SC-001..SC-005).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (P1)**: none — run first for the baseline.
- **Foundational (P2)**: depends on Setup; BLOCKS all stories (status, config, counter must exist).
- **US1 (P3)** and **US2 (P4)**: both depend on Foundational. US2's helper branch (T017) and done-path (T018) extend US1's helper (T012) and done-path (T013) — so within `main.py`, US1 lands before US2. US2's coordinare route (T019) is independent of US1.
- **US3 (P5)**: depends on Foundational (counter) and reads the `local_test_failed` flag from US1's done-path (T013); its coordinare logic (T023) sits beside US2's route (T019) in the same file.
- **Polish (P6)**: depends on all stories.

### Config-delivery (C1) chain

The gate is coordinare-configured but executes in the performer, which only sees fields delivered on the dispatch payload (`card_context` flattens to top-level `msg.payload` keys → `Score(**msg.payload)` at main.py:1218). The `Score` model's `extra="ignore"` (models.py:135) drops undeclared keys — so all three legs are required or the gate silently no-ops in prod (passing unit tests notwithstanding):

- **T005a** declares `Score.local_test_gate` (performer can receive it) →
- **T008a** injects `card_context["local_test_gate"]` for `implementing` (coordinare sends it) →
- **T013** reads `perf.score.local_test_gate` for `enabled` + `timeout_seconds` (performer honours it).

T003a is the failing test that guards this end to end.

### Critical sequencing (same-file, must be sequential)

- `agent/performer/src/performer/main.py`: T012 → T013 → T017 → T018.
- `src/coordinare/graph/nodes/monitor_performer.py`: T019 → T023.
- `src/coordinare/state_store.py` T006 → `session.py` T007 → `daemon.py` T008.
- C1 delivery: T005a + T008a (foundational) precede T013.

### Parallel Opportunities

- Foundational tests T002, T003, T003a in parallel; impl T004, T005, T005a in parallel (T006→T007→T008 sequential; T008a independent, beside the other dispatch injections).
- US1 tests T009, T010, T011 in parallel (then T012→T013).
- US2 tests T014, T015, T016 in parallel.
- US3 tests T020, T021, T022 in parallel.

---

## Implementation Strategy

### MVP (US1 + US2)

US1 and US2 are co-equal P1: the gate must not ship without env-blocked classification (it would reproduce the spec-088 misattribution). MVP = Setup + Foundational + US1 + US2.

1. Phase 1 baseline.
2. Phase 2 Foundational.
3. Phase 3 US1 → validate: red code never pushed.
4. Phase 4 US2 → validate: env failure → env-blocked, 0 attempts consumed.
5. **STOP and VALIDATE** the MVP.

### Incremental

Add US3 (bounded loop + escalation + in-session prompt) → validate budget independence and escalation → Polish.

---

## Notes

- [P] = different files, no dependency on incomplete tasks.
- Disabled-by-default (`enabled=False`) must remain byte-identical to pre-089 (SC-005) — assert no `_run_test_check` call and no counter mutation when disabled.
- Env-blocked path must never increment `local_fix_counter` (FR-006, SC-003).
- Escalation to blocked never pushes (SC-001).
- Commit after each task or logical group. Run `speckit.analyze` before `speckit.implement`.
