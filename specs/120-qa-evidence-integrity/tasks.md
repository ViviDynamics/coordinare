# Tasks: QA Evidence Integrity, Toolchain Availability & Visual-Evidence Capture

**Input**: Design documents from `/specs/120-qa-evidence-integrity/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/qa-report.md, quickstart.md

**Tests**: REQUIRED — Constitution II (Testing Discipline) is NON-NEGOTIABLE; TDD (Red→Green) per story.

**Organization**: Grouped by user story (US1/US2/US3) so each is independently implementable & testable.

## Path Conventions

Single repo: coordinare daemon under `src/coordinare/`, performer package under
`agent/performer/src/performer/`, tests under `tests/unit/` and `tests/integration/`.

---

## Phase 1: Setup (Shared)

- [x] T001 Confirm clean baseline on branch `120-qa-evidence-integrity`: run `.venv/bin/pytest -q` and `.venv/bin/ruff check src agent/performer/src` and record the green starting point (no code changes).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared helpers both US1 layers depend on. Must complete before US1 implementation.

- [x] T002 Add a pure, side-effect-free QA-substantiation helper module `src/coordinare/services/qa_verdict.py` exposing `classify_qa_verdict(status: str, report: dict | None, env_cache_health_failed: bool) -> Literal["advance","hold","bounce"]` implementing the decision table in `specs/120-qa-evidence-integrity/contracts/qa-report.md` (keys on `criteria_checked>0 AND criteria_passed==0`, and on `visual_validation_required` with no `visual_evidence` having truthy `path_or_url`; "env signal" = truthy `report.environment_error` OR `env_cache_health_failed`). Reads `report` defensively (`report or {}`). No I/O, no logging of values.
- [x] T003 [P] Write failing unit tests `tests/unit/services/test_qa_verdict.py` covering every row of the contract decision table (advance / hold / bounce), including: legitimate pass (≥1 criteria + evidence) → advance; `criteria_checked==0` no-scope → advance; visual-required-but-no-evidence → hold/bounce by env signal; missing/`None` report on `qa_passed` → unsubstantiated. Assert helper is pure (no exceptions on partial dicts).

---

## Phase 3: User Story 1 — Gate rejects an unsubstantiated pass (Priority: P1) 🎯 MVP

**Goal**: A `qa_passed` with 0/N criteria (or missing required visual evidence) can never advance —
HOLD (env signal) or BOUNCE (otherwise). Performer stops emitting the false advisory-pass; coordinare
enforces the gate regardless of performer build (defense in depth).

**Independent Test**: Synthetic terminal statuses through the coordinare monitor route to
advance/hold/bounce exactly per the decision table; performer advisory-pass with `criteria_passed==0`
returns `qa_env_blocked`.

### Tests (write first — must fail)

- [x] T004 [P] [US1] Failing unit test `tests/unit/performer/test_qa_advisory_pass.py`: given a QA output where all failures are environmental and `criteria_passed==0` with non-empty `executed_checks`, assert `_perform_qa(...)`/terminal builder returns `PerformerResponse.status == "qa_env_blocked"`; with `criteria_passed>=1` assert advisory `qa_passed` is retained. (Mirror existing performer QA test fixtures.)
- [x] T005 [P] [US1] Failing integration test `tests/integration/test_monitor_qa_gate.py`: drive `monitor_performer` with the five quickstart US1 statuses and assert: no advance on unsubstantiated; `mark_runtime_health_failed` + notify on HOLD; bounce path on no-env-signal; legitimate pass advances; `criteria_checked==0` advances.

### Implementation

- [x] T006 [US1] Fix the performer env-limited advisory-pass branch in `agent/performer/src/performer/main.py` (~L2731): when `env_limited` and `criteria_passed == 0`, classify as `qa_env_blocked` (set `perf.state`/return `status="qa_env_blocked"` with a names-only reason) instead of `qa_passed`. Retain advisory `qa_passed` only when `criteria_passed >= 1`. Keep the existing `_qa_unsubstantiated_pass` zero-evidence path unchanged.
- [x] T007 [US1] Wire the coordinare gate into `src/coordinare/graph/nodes/monitor_performer.py`: before the `marker in TERMINAL_SUCCESS_STATES` advance for QA, call `classify_qa_verdict(...)` from T002 using `status`, `status.get("report")`, and `status.get("env_cache_health_failed")`. On `"hold"`, reuse the existing `qa_env_blocked` block (mark_runtime_health_failed + `env_health_hold_reason` + re-dispatch hold) — refactor that block into a small local helper if needed so both the explicit `qa_env_blocked` marker and a downgraded `qa_passed` share one path. On `"bounce"`, route to the existing `qa_failed`/fix-feedback path. On `"advance"`, proceed unchanged.
- [x] T008 [US1] Record the downgrade decision in `monitor_performer` with a structlog event naming the cause (`zero_criteria_passed` / `missing_visual_evidence` / `environment_error`) and the chosen route — names/ids/counts/reasons only, no values (FR-007/FR-019).
- [x] T009 [US1] Verify the operator-facing PR-comment/notification rendering for a downgraded verdict reads ENVIRONMENT-BLOCKED/UNVERIFIED or FAILED (never PASSED); adjust the QA comment builder if a downgraded `qa_passed` would otherwise render "PASSED" (FR-003/UX consistency). Add/extend a test asserting the rendered verdict text.

**Checkpoint**: US1 green in isolation — false passes blocked, legitimate passes unaffected.

---

## Phase 4: User Story 2 — Toolchain on the QA performer's PATH (Priority: P1)

**Goal**: The QA performer's command shell exposes the project runtime (Ruby/rbenv) + services like
bootstrap; activation failure surfaces as an environment error (feeding US1), never a silent pass;
each dispatch emits an attach/activation observability record.

**Independent Test**: `_activate_env_cache` against a fixture cache returns a `cache_env` whose PATH
contains the resolved toolchain even with `_DEVENV_SOURCED=1` set; an advertised-but-unresolvable
toolchain flags failure; observability event is emitted.

### Tests (write first — must fail)

- [x] T010 [P] [US2] Failing unit test `tests/unit/performer/test_activate_env_cache.py`: fixture cache dir with an `activate.sh` that prepends `$DEVENV/.toolchain/bin` to PATH; call `_activate_env_cache` with `os.environ` containing `_DEVENV_SOURCED=1`; assert returned `cache_env["PATH"]` contains the resolved `<cache>/.toolchain/bin` (proves `DEVENV` exported + guard cleared). Add a case where the advertised toolchain binary is absent → activation reports failure.
- [x] T011 [P] [US2] Failing unit test asserting `_activate_env_cache` emits a structlog observability event with `env_cache_path` present (bool), `activation_succeeded` (bool), `toolchain_resolved` (bool), `stage` — and carries no secret values.

### Implementation

- [x] T012 [US2] In `agent/performer/src/performer/workspace.py` `_activate_env_cache` (~L235): build the sourcing subprocess env with `DEVENV=<env_cache_path>` exported and `_DEVENV_SOURCED` cleared (mirror the existing `_start_env_cache_services` fix at ~L109), so `$DEVENV`-relative paths in `activate.sh` resolve regardless of inherited profile state.
- [x] T013 [US2] Add a post-activation toolchain-resolution check in `workspace.py`: derive the expected toolchain from what the cache advertises (e.g. `activate.sh` referencing rbenv ⇒ expect `ruby` resolvable on the produced PATH); if advertised-but-unresolvable, mark activation failed and propagate a signal the QA path turns into `environment_error` / `env_cache_health_failed`. A cache advertising no toolchain asserts nothing (non-Ruby projects unaffected).
- [x] T014 [US2] Emit the per-activation observability event (T011 contract) from `workspace.py`, names/reasons only.
- [x] T015 [US2] In `agent/performer/src/performer/main.py` QA path: when activation/toolchain resolution failed (from T013), ensure the QA report carries `environment_error` (and/or `PerformerResponse.env_cache_health_failed=True`) so US1's gate routes it to HOLD rather than a substantive verdict (FR-011).

**Checkpoint**: US2 green — Ruby resolvable in the QA shell; activation failure becomes an honest env error.

---

## Phase 5: User Story 3 — Visual evidence, both paths (Priority: P2)

**Goal**: QA performer may capture with any in-image browser tooling (primary); the post-QA docker
node runs only with boot-proof and never reports empty success (backstop); no screenshot ⇒ no pass
(enforced via US1).

**Independent Test**: persona text permits any browser tooling; `qa_screenshots` records honest
"not reachable"/"docker unavailable" instead of a silent empty success; visual-required-no-evidence
cannot advance (US1 case 4, already covered).

### Tests (write first — must fail)

- [x] T016 [P] [US3] Failing unit test `tests/unit/services/test_qa_persona_visual.py`: assert the qa persona built by `persona_service` permits any in-image browser tooling (Chromium/Playwright) for in-turn capture and mandates no single tool, while still requiring `app_boot_check` + ≥1 `visual_evidence` for `visual_validation_required`.
- [x] T017 [P] [US3] Failing unit test `tests/unit/graph/test_qa_screenshots_guard.py`: (a) no boot-proof / `app_boot_check` absent → node does not produce a silent empty-success; it records "not reachable". (b) docker unavailable → recorded honestly. (c) reachable + boot-proof → capture attempted.

### Implementation

- [x] T018 [US3] Update the qa persona in `src/coordinare/services/persona_service.py`: state the performer may use whatever browser tooling is already present in the image (Chromium/Playwright) to boot the app and capture ≥1 screenshot — "however is easiest", no single mandated tool. Keep the existing `app_boot_check` + `visual_evidence` requirements.
- [x] T019 [US3] Harden `src/coordinare/graph/nodes/qa_screenshots.py`: gate the run on boot-proof from the QA report (`app_boot_check` exit_code 0); when docker is unavailable or the app is unreachable, record that fact (status/log) instead of silently setting `qa_screenshots=[]` as if successful (FR-014/FR-015).

**Checkpoint**: US3 green — honest backstop + permissive in-performer capture; enforcement via US1.

---

## Phase 6: Polish & Cross-Cutting

- [x] T020 [P] Run `.venv/bin/ruff check` on all changed files (`src/coordinare/services/qa_verdict.py`, `monitor_performer.py`, `qa_screenshots.py`, `persona_service.py`, `agent/performer/.../main.py`, `workspace.py`) and fix all findings.
- [x] T021 [P] Secret-invariant audit: grep new structlog events/comments in the changed files for value-bearing keys; assert only names/ids/counts/reasons are emitted (FR-019). Add a focused test if a leak-prone field exists.
- [x] T022 Full regression: `.venv/bin/pytest -q` green, coverage not decreased; update any QA fixtures that asserted the old advisory-pass behavior.
- [x] T023 Adversarial review before merge (diverse-lens finders + refute-verify per project discipline) focused on: US1 not regressing legitimate passes, US2 not breaking activation for non-Ruby/other stages, US3 backstop honesty. Resolve findings inline (no follow-ups).
- [x] T024 Mark completed tasks `[x]` in this file and write a short PR description referencing spec 120 + the three FRs groups; rebase on `main` before merge.

---

## Dependencies & Order

- **Setup (T001)** → **Foundational (T002–T003)** → **US1 (T004–T009)**.
- **US2 (T010–T015)** depends only on Setup; can run in parallel with US1 (different files: `workspace.py`/performer-`main.py` QA-env vs `qa_verdict.py`/`monitor_performer.py`). Note T006 and T015 both touch performer `main.py` → sequence them (US1 T006 first, then US2 T015).
- **US3 (T016–T019)** depends on Setup; persona/node code is independent of US1/US2, but its *runtime* validation needs US2 (app must boot). Enforcement leans on US1.
- **Polish (T020–T024)** last.

## Parallel Execution Examples

- After T003: run T004, T005 (US1 tests) in parallel with T010, T011 (US2 tests) and T016, T017 (US3 tests) — all different files.
- T012/T013/T014 (US2, all `workspace.py`) are sequential; T018 (persona) and T019 (node) are parallel to them.
- T020, T021 parallel in Polish.

## Implementation Strategy

- **MVP = US1** (the safety gate). Ship/verify it first even within this combined spec — it closes the
  false-PASS hole independent of US2/US3.
- Then **US2** (unblocks real verification), then **US3** (proves UI changes), then live re-verify on
  the website symphony per quickstart.

## Task Count

24 tasks — Setup 1, Foundational 2, US1 6, US2 6, US3 4, Polish 5. Test tasks: T003, T004, T005, T010,
T011, T016, T017 (+ assertions in T009, T021).
