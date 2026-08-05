---
description: "Task list for Onboarding config hardening (Spec 132, issue #180)"
---

# Tasks: Onboarding config hardening

**Input**: Design documents from `/specs/132-onboarding-config-hardening/`
**Prerequisites**: plan.md, spec.md, research.md, quickstart.md
**Tests**: Requested — Constitution II mandates coverage; test tasks are included per story.
**Branch**: `132-onboarding-config-hardening`

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: US1 / US2 / US3 (maps to spec.md user stories)

## Path Conventions

Single project: `src/coordinare/`, `tests/` at repo root; example config and wrappers at repo root / `bin/`.

## Scope guardrails (apply to every task)

- Keep `agent/performer/uv.lock` **unstaged and entirely out of this feature** — never `git add` it.
- Keep `.claude/skills/work-issue-speckit/SKILL.md` **uncommitted** (placement TBD by the team).
- No personal-account (spec 133) changes — do **not** touch `config.example.yaml:57` `github_org` semantics.
- No push, PR, board move, or remote change without explicit approval.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create the single shared test module the three stories add to.

- [x] T001 Create the test module scaffold `tests/unit/test_132_onboarding_config_hardening.py` with `from __future__ import annotations`, imports (`pathlib`, `pytest`, `coordinare.config`), and a module docstring referencing spec 132 / issue #180.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: None required. This feature has no shared blocking infrastructure — the three
user stories touch disjoint files (`config.example.yaml`, `bin/performer`,
`src/coordinare/daemon.py`) and are independently implementable/testable after Phase 1.

**Checkpoint**: Foundation ready — user stories can proceed (in parallel if staffed).

---

## Phase 3: User Story 1 - First run launches the performer with no script edits (Priority: P1) 🎯 MVP

**Goal**: The shipped `agent_executable` default works on a fresh clone; no checked-in
performer script carries a hardcoded personal path.

**Independent Test**: On a clean clone with no system-installed performer, the example
default launches the performer with zero `bin/` edits; `grep -rn "/Users/" bin/` is empty.

### Tests for User Story 1

- [x] T002 [US1] In `tests/unit/test_132_onboarding_config_hardening.py`, add a test asserting the shipped `config.example.yaml` `agent_executable` value is `bin/run-performer`, that the file exists and is executable, and that `bin/performer` exists, contains no `/Users/` (or other home) absolute path, and references `run-performer` (delegation). Verify it FAILS before implementation.

### Implementation for User Story 1

- [x] T003 [US1] In `config.example.yaml` (~L85), change the active `agent_executable: "/usr/local/bin/performer"` to `agent_executable: "bin/run-performer"`; keep the surrounding comment accurate (portable wrapper; launch from repo root).
- [x] T004 [US1] In `config.example.yaml` (~L909), change the commented `# agent_executable: "/usr/local/bin/performer"` reference to match (`# agent_executable: "bin/run-performer"`).
- [x] T005 [US1] Rewrite `bin/performer` as a minimal compatibility wrapper: resolve its own directory and `exec` the sibling `bin/run-performer` with `"$@"` — no duplicated launch logic, no `.venv`/python invocation, no home path. Replaces the pre-existing working-tree edit.

**Checkpoint**: US1 fully functional and independently testable.

---

## Phase 4: User Story 2 - Copying the example config validates cleanly without secrets (Priority: P2)

**Goal**: A fresh copy of `config.example.yaml` validates with no Slack/SMTP secrets set.

**Independent Test**: With no notification secrets in the env, loading the example config
through validation succeeds; `channels`/`routing` are empty; commented references remain.

### Tests for User Story 2

- [x] T006 [US2] In `tests/unit/test_132_onboarding_config_hardening.py`, add a test that loads `config.example.yaml` with notification-secret env vars unset and asserts config validation succeeds and `notifications.channels == []` and `notifications.routing == []`. Verify it FAILS before implementation.

### Implementation for User Story 2

- [x] T007 [US2] In `config.example.yaml` (~L738-770), set `notifications.channels: []` and `notifications.routing: []` as the shipped default, and move the current `slack-ops` and `email-team` channel definitions (and any routing entries) into an adjacent **commented** reference block so the setup path stays discoverable (FR-005).

**Checkpoint**: US1 and US2 both work independently.

---

## Phase 5: User Story 3 - A paused symphony is obvious at startup (Priority: P3)

**Goal**: A symphony with `enabled: false` announces itself with one clear startup line, not
a per-cycle warning.

**Independent Test**: With one symphony `enabled: false`, startup logs show exactly one
`symphony '<name>' is paused (enabled: false)` line; it does not recur across poll cycles.

### Tests for User Story 3

- [x] T008 [US3] In `tests/unit/test_132_onboarding_config_hardening.py`, add a test (using structlog capture / caplog) asserting that a symphony configured with `enabled: false` produces exactly one clear startup "paused (enabled: false)" line naming the symphony, and that repeated cycles do not re-emit it. Cover both multi-symphony and legacy single-symphony shapes. Verify it FAILS before implementation.

### Implementation for User Story 3

- [x] T009 [US3] In `src/coordinare/daemon.py` (~L2958-2964, multi-symphony loop), replace the per-cycle `logger.warning("symphony.disabled_skip", …)` with a one-time, unambiguous startup line `symphony '<name>' is paused (enabled: false)`, deduped so it is not re-emitted every cycle (e.g. track already-announced paused symphonies on the daemon, or emit at the startup summary where symphonies are first loaded).
- [x] T010 [US3] In `src/coordinare/daemon.py` (legacy single-symphony branch, ~L2972+), ensure a single disabled symphony gets the same clear, deduped startup pause line.

**Checkpoint**: All three stories independently functional.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [x] T011 [P] Audit and update onboarding docs (`README.md` and any live quickstart/setup docs) for stale performer-path or notification-default references so they match the corrected example (FR-008). Do **not** modify archived `specs/**` artifacts.
- [x] T012 Run the `quickstart.md` verification steps (US1/US2/US3) and confirm SC-001…SC-006 hold. *(Verified via the automated tests in `tests/unit/test_132_onboarding_config_hardening.py`, which encode each acceptance check; a manual end-to-end daemon launch against a fresh config was not performed in this environment.)*
- [x] T013 Run `make test-all` and `make lint`; fix any failures; confirm coverage does not regress.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies — creates the shared test file.
- **Foundational (Phase 2)**: none.
- **User Stories (Phase 3–5)**: depend only on Phase 1 (shared test file). Independent of each other — can proceed in parallel or in priority order P1 → P2 → P3.
- **Polish (Phase 6)**: after the stories being delivered are complete.

### User Story Dependencies

- US1, US2, US3 are mutually independent (disjoint files). Each is independently testable.

### Within Each User Story

- Test task first (write, confirm it FAILS) → implementation task(s).

### Parallel Opportunities

- T002, T006, T008 are all in the same test file (`tests/unit/test_132_onboarding_config_hardening.py`) — **not** marked [P]; add them sequentially to avoid edit conflicts.
- T003/T004 (config.example.yaml) and T005 (bin/performer) are different files → parallelizable within US1.
- T009/T010 touch the same file (`daemon.py`) → serialize.

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1 (Setup) → Phase 3 (US1) → validate US1 independently (the hard-blocker fix). Ship-worthy on its own.

### Incremental Delivery

1. US1 (performer launch) → 2. US2 (notifications off) → 3. US3 (paused-symphony clarity) → Polish. Each adds value without breaking prior stories.

---

## Notes

- Commit after each story (or logical task group) with Conventional Commits messages; include the `specs/132-onboarding-config-hardening/` artifacts with the code.
- Verify each test FAILS before implementing its story.
- Excluded from every commit in this PR: `agent/performer/uv.lock`, `.claude/skills/work-issue-speckit/SKILL.md`.
- Total: 13 tasks — US1: 4 (1 test + 3 impl), US2: 2 (1 test + 1 impl), US3: 3 (1 test + 2 impl), Setup: 1, Polish: 3.
