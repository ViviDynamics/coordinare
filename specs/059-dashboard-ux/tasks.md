# Tasks: Dashboard UX Redesign (059)

**Input**: Design documents from `/specs/059-dashboard-ux/`
**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/sse-snapshot-schema.md ✅, quickstart.md ✅

**Organization**: Tasks are grouped by user story (P1–P4) to enable independent implementation and testing. All changes are confined to `src/coordinare/dashboard.py` and `tests/unit/test_dashboard.py`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US4)

---

## Phase 1: Setup

**Purpose**: Confirm the working environment and baseline before making any changes.

- [x] T001 Verify baseline test coverage passes at ≥90% — run `.venv/bin/pytest --cov=coordinare --cov-report=term-missing --cov-fail-under=90` and record the starting percentage
- [x] T002 [P] Verify baseline lint is clean — run `.venv/bin/ruff check src/coordinare/dashboard.py tests/unit/test_dashboard.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: CSS design token foundation and phase-label enforcement — both block all user stories because every panel depends on tokens and every string display depends on `formatPhaseLabel()`.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Phase A — CSS Design Token Foundation (FR-003, FR-009, FR-010)

- [x] T003 Add `:root { --color-* }` custom properties block to the `<style>` section in `src/coordinare/dashboard.py` using the token taxonomy from `data-model.md` (bg-base, bg-surface, bg-elevated, border, border-subtle, text-primary, text-muted, accent-blue, accent-green, accent-yellow, accent-red, and semantic aliases healthy/degraded/error/active)
- [x] T004 Replace all hard-coded hex colour values in the `<style>` CSS block of `src/coordinare/dashboard.py` with `var(--color-*)` references — scan for `#0d1117`, `#161b22`, `#21262d`, `#30363d`, `#c9d1d9`, `#8b949e`, `#58a6ff`, `#3fb950`, `#d29922`, `#f85149`
- [x] T005 Replace all hard-coded hex colour values in JavaScript-generated `style=` attribute strings within `src/coordinare/dashboard.py` (`renderActiveWorkPanels`, `renderSubsystems`, utilization table, cycle history rendering) with `var(--color-*)` references
- [x] T006 Visual verification: start coordinare locally (`set -a && source .env && set +a && uv run python -m coordinare`), open `http://localhost:8080`, confirm all panels render with the correct dark theme (no colour regression)

### Phase B — Phase Label Enforcement (FR-003, SC-006)

- [x] T007 Add `formatPhaseLabel(rawPhase)` JavaScript function to `src/coordinare/dashboard.py` — mirrors Python `format_phase_label()`: replace underscores and hyphens with spaces, title-case each word, return `"—"` for empty/null input
- [x] T008 Audit `renderActiveWorkPanels` in `src/coordinare/dashboard.py` and wrap every `session.phase` and `session.performer_stage` reference with `formatPhaseLabel()` before DOM insertion
- [x] T009 Audit the cycle history table rendering in `src/coordinare/dashboard.py` and wrap every `entry.phase` reference with `formatPhaseLabel()` before insertion into `<td>` cells
- [x] T010 Extend unit tests for `format_phase_label()` in `tests/unit/test_dashboard.py` to cover: empty string, single word, already-title-case input, mixed separators (underscore + hyphen), and `None` input

**Checkpoint**: Foundation ready — design tokens applied, all raw phase strings wrapped. User story work can now begin.

---

## Phase 3: User Story 1 — Understand System Status at a Glance (Priority: P1) 🎯 MVP

**Goal**: Operator can assess overall system health, active card status, and idle vs. active state within 10 seconds without scrolling.

**Independent Test**: Open dashboard with (a) no active cards — verify single idle panel shows; (b) one active card — verify phase label is human-readable, elapsed time is visible, stale flag appears after 30 min; (c) all subsystems healthy — verify single green summary line, no expanded table.

### Phase C — Subsystem Health Summary Panel (FR-005, SC-001)

- [x] T011 [US1] Add `compute_overall_health(subsystems: list[dict]) -> str` Python helper to `src/coordinare/dashboard.py` — returns `"healthy"` if all required subsystems are healthy, `"unavailable"` if any required subsystem is unavailable, otherwise `"degraded"`; ignores optional subsystems
- [x] T012 [US1] Add unit tests for `compute_overall_health()` in `tests/unit/test_dashboard.py` covering: all healthy, one required degraded, one required unavailable, mix of required + optional, empty list
- [x] T013 [US1] Rewrite `renderSubsystems()` JavaScript function in `src/coordinare/dashboard.py` to output a `<details>/<summary>` widget — summary line shows `● All systems healthy` (green) / `⚠ N systems degraded` (yellow) / `✕ N systems unavailable` (red); `<details>` closed by default when healthy, open by default when degraded/unavailable
- [x] T014 [US1] Inside the `<details>` element rendered by `renderSubsystems()`, render the full subsystem table with degraded/unavailable rows highlighted using `var(--color-degraded)` and `var(--color-error)` respectively, and the `details` field shown inline

### Phase G — Idle State Panel (FR-011, SC-005)

- [x] T015 [US1] Add `#idle-panel` hidden `<div>` to the HTML in `src/coordinare/dashboard.py` containing: "Coordinare is idle" heading, board total cards, board in-progress count, assignee filter hint (if set), last poll time (relative), cycles completed today
- [x] T016 [US1] Add `isIdle(snapshot)` check in `applyState()` in `src/coordinare/dashboard.py` — when `active_sessions.length === 0 && snapshot.phase === "idle"`: hide Active Work, Awaiting Review, and Performers cards; show `#idle-panel` with values from `snapshot.board_summary`, `snapshot.last_poll_at`, `snapshot.cycles_completed`, and `snapshot.assignee_filter`
- [x] T017 [US1] Ensure `applyState()` hides `#idle-panel` and shows normal panels when `isIdle()` returns false

### Phase H — SSE Connection Status (FR-009, SC-007)

- [x] T018 [US1] Add `role="status" aria-live="polite"` to the nav SSE status dot wrapper element in `src/coordinare/dashboard.py`
- [x] T019 [US1] Add `title` attribute updates to the SSE dot: `"SSE connected"` on open, `"SSE disconnected — reconnecting"` on error/close in the `EventSource` handlers in `src/coordinare/dashboard.py`

**Checkpoint**: US1 complete — operator can assess system status at a glance within 10 seconds.

---

## Phase 4: User Story 2 — Navigate to the Right Information Quickly (Priority: P2)

**Goal**: Operator can identify their current page immediately and reach any section in one click on any supported viewport.

**Independent Test**: (a) Hard-load `/history` directly — verify History nav link is highlighted with blue underline and `aria-current="page"`. (b) Click each nav link via SPA navigation — verify active link updates. (c) On narrow viewport, verify hamburger menu closes after link click.

### Phase D — Navigation Active-Page Indication (FR-004, FR-012, SC-002)

- [x] T020 [US2] Add `setActiveNav(path)` JavaScript function to `src/coordinare/dashboard.py` — clears `.nav-active` class and `aria-current` from all nav links, then sets `.nav-active` class and `aria-current="page"` on the link whose `href` matches `path`
- [x] T021 [US2] Call `setActiveNav(window.location.pathname)` on `DOMContentLoaded` in `src/coordinare/dashboard.py` so active indication is set on hard-load and direct URL navigation
- [x] T022 [US2] Refactor existing `navigate()` function in `src/coordinare/dashboard.py` to call `setActiveNav(path)` instead of manually toggling the nav-active class
- [x] T023 [US2] Ensure the hamburger menu closes (removes `.nav-open` class) after any nav link click on narrow viewports in `src/coordinare/dashboard.py`

**Checkpoint**: US2 complete — active nav link highlighted on both hard-load and SPA navigation; `aria-current="page"` set correctly.

---

## Phase 5: User Story 3 — Understand Individual Card and Performer Progress (Priority: P3)

**Goal**: Operator can see phase, assigned card, elapsed time, cost, and log stream for any active performer in one view without additional navigation.

**Independent Test**: (a) With an active card, click its row — verify detail view shows all 5 data points. (b) With a card running > 30 min, verify ⚠ prefix on elapsed time. (c) With `agent_dispatch_at` null, verify cost shows `"—"`. (d) Verify Awaiting Review panel has amber left-border and distinct header.

### Phase E — Active Work and Awaiting Review Panel Redesign (FR-001, FR-002, FR-007, FR-008, SC-001, SC-008)

- [x] T024 [US3] Add `is_session_stale(agent_dispatch_at_iso: str | None, threshold_minutes: int = 30) -> bool` Python helper to `src/coordinare/dashboard.py` — returns `True` when elapsed time exceeds threshold; returns `False` when `agent_dispatch_at_iso` is `None`
- [x] T025 [US3] Add unit tests for `is_session_stale()` in `tests/unit/test_dashboard.py` covering: below threshold, at threshold, above threshold, `None` input, and invalid ISO string
- [x] T026 [US3] Rewrite Active Work card rows in `renderActiveWorkPanels()` in `src/coordinare/dashboard.py` to show per-row: card title (linked to `issue_url`), human-readable phase label via `formatPhaseLabel(session.phase)`, elapsed time via `fmtElapsed(elapsedMs(session))`, and cost via `costDisplay(session)` (using `"—"` when `agent_dispatch_at` is null)
- [x] T027 [US3] Add stale elapsed-time treatment in `renderActiveWorkPanels()` in `src/coordinare/dashboard.py` — when `isStale(session)` is true, prefix elapsed time with `⚠ ` and apply `color: var(--color-degraded)` to the elapsed-time element
- [x] T028 [US3] Rewrite Awaiting Review card rows in `renderActiveWorkPanels()` in `src/coordinare/dashboard.py` to show per-row: card title, PR link, waiting duration, cost; apply amber left-border (`var(--color-degraded)`) and distinct header text "Awaiting Your Review"

### Phase F — Consolidated Performer Detail View (FR-006, FR-014, SC-003)

- [x] T029 [US3] Add click handler to Active Work card rows in `src/coordinare/dashboard.py` — on click, find the matching session in `active_sessions` by `card_id` and call a new `showPerformerDetail(session)` function
- [x] T030 [US3] Add `showPerformerDetail(session)` JavaScript function to `src/coordinare/dashboard.py` — renders in the existing detail area: human-readable phase label, card title + issue link, elapsed time (with stale ⚠ flag if applicable), cost estimate (`costDisplay(session)`), and performer log stream (last 20 lines, existing rendering reused)
- [x] T031 [US3] Add a Back button in the performer detail view that returns to the Active Work panel list in `src/coordinare/dashboard.py`
- [x] T032 [US3] Ensure performer detail view live-updates with new SSE snapshots while open in `src/coordinare/dashboard.py` — re-render elapsed, cost, and log on each `applyState()` call when detail is visible

**Checkpoint**: US3 complete — clicking a card row shows all 5 data points (phase, card, elapsed, cost, logs) in one view.

---

## Phase 6: User Story 4 — Operate Confidently on Small Screens (Priority: P4)

**Goal**: All primary dashboard panels are readable and functional at a 768px viewport width with no horizontal scrollbar.

**Independent Test**: Resize browser to 768px width. Navigate to Home, Symphonies, and Config pages. Verify no horizontal scrollbar appears on any page and all panels are readable.

### Phase I — Responsive Layout Fixes (FR-010, SC-004)

- [x] T033 [US4] Wrap the Symphonies page table container in `src/coordinare/dashboard.py` with a `<div style="overflow-x: auto">` wrapper so wide tables scroll horizontally within their container rather than overflowing the viewport
- [x] T034 [US4] Wrap the Global Config page table container in `src/coordinare/dashboard.py` with a `<div style="overflow-x: auto">` wrapper (Global Config page uses form fields — applied to cycle history table instead)
- [x] T035 [US4] Audit all JS-generated HTML in `src/coordinare/dashboard.py` for any fixed-width elements (`width: <Npx>` where N exceeds ~720) and replace with `max-width: 100%` or percentage-based widths as appropriate
- [x] T036 [US4] Visual verification at 768px: open each page (Home, History, Symphonies, Config) in a browser resized to 768px and confirm no horizontal scrollbar on any page

**Checkpoint**: US4 complete — no horizontal overflow at 768px on any page.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Tests and coverage gate, accessibility hardening, and quickstart verification.

### Phase J — Tests and Coverage (Constitution Principle II)

- [x] T037 [P] Add unit tests for `render_performer_pool_widget()` in `tests/unit/test_dashboard.py` covering idle, busy, excluded, and mixed states (if not already present)
- [x] T038 [P] Verify `format_phase_label()` edge-case tests added in T010 all pass: `uv run --extra dev pytest tests/unit/test_dashboard.py -v -k format_phase_label`
- [x] T039 Run full coverage check and confirm ≥90%: `.venv/bin/pytest --cov=coordinare --cov-report=term-missing --cov-fail-under=90`
- [x] T040 Run full lint pass: `.venv/bin/ruff check src/coordinare/dashboard.py tests/unit/test_dashboard.py`

### Accessibility Hardening

- [x] T041 [P] Add `tabindex="0"` and `role="button"` to all clickable performer/card rows in JS-generated HTML in `src/coordinare/dashboard.py`
- [x] T042 [P] Verify `<details>/<summary>` for subsystem health has a visible focus ring at 768px+ (browser default should suffice; add explicit `outline` style if not visible in manual test)

### Quickstart Verification

- [x] T043 Run through each verification scenario in `specs/059-dashboard-ux/quickstart.md` manually and confirm all acceptance criteria from the plan phases (A–I) are met

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — **BLOCKS all user stories**
- **Phase 3 (US1 — P1)**: Depends on Phase 2 completion
- **Phase 4 (US2 — P2)**: Depends on Phase 2 completion; independent of US1
- **Phase 5 (US3 — P3)**: Depends on Phase 2 completion; independent of US1/US2 (note: reads `active_sessions` shape established in Phase 2)
- **Phase 6 (US4 — P4)**: Depends on Phase 2 completion; independent of US1/US2/US3
- **Phase 7 (Polish)**: Depends on all desired user stories being complete

### Within Phase 2 (Foundational)

- T003 must complete before T004 and T005 (tokens defined before they are referenced)
- T007 must complete before T008 and T009 (function defined before call sites updated)
- T010 can run in parallel with T003–T009 (test file, no implementation dependency)

### Parallel Opportunities

- Phases 3, 4, 5, 6 can proceed in parallel once Phase 2 is complete (all touch `dashboard.py` but in different functions — coordinate to avoid merge conflicts)
- Within Phase 3: T011 and T012 can run in parallel (T012 is the test for T011)
- Within Phase 5: T024 and T025 can run in parallel (Python helper + its tests)
- Within Phase 7: T037, T038, T041, T042 can all run in parallel

---

## Implementation Strategy

### MVP (User Story 1 Only)

1. Complete Phase 1: Setup (T001–T002)
2. Complete Phase 2: Foundational (T003–T010)
3. Complete Phase 3: US1 — Status at a Glance (T011–T019)
4. **STOP and VALIDATE**: Verify idle panel, subsystem health widget, SSE dot accessibility
5. Continue with remaining user stories in priority order

### Incremental Delivery

1. Phase 1 + 2 → Tokens applied, all phase labels human-readable (foundation)
2. Phase 3 → Status-at-a-glance readable; idle state informative (**MVP**)
3. Phase 4 → Active nav highlighted; no page-reload confusion
4. Phase 5 → Full card/performer detail; awaiting-review visually distinct
5. Phase 6 → 768px layout clean; no overflow
6. Phase 7 → Coverage gate passes; accessibility hardened; quickstart verified

---

---

## Phase 8: CI Pipeline Fix (Main Branch Build Reliability)

**Context**: The main branch CI build was failing because the `build-full` job ran in parallel with `build-base`. The full image's `Dockerfile.full` used `FROM coordinare-performer:base` — a local tag that doesn't exist in GHCR. The base image had not been pushed to the registry yet when the full image build started.

**Root cause**: Missing `needs: build-base` dependency in `.github/workflows/main-branch-build.yml`.

- [x] T044 Add `build-base` to the `needs` list of the `build-full` job in `.github/workflows/main-branch-build.yml` so the two builds run in series (base pushed first, then full)
- [x] T045 Update the `build-full` job's `docker build` command in `.github/workflows/main-branch-build.yml` to pass `--build-arg BASE_IMAGE="${REGISTRY}-base:${VERSION}"` so `Dockerfile.full` receives the versioned registry reference
- [x] T046 Update `agent/performer/Dockerfile.full` to use `ARG BASE_IMAGE=coordinare-performer:base` + `FROM $BASE_IMAGE` so local builds continue to work without arguments and CI builds override with the registry reference via `--build-arg`

---

## Notes

- All changes confined to `src/coordinare/dashboard.py` and `tests/unit/test_dashboard.py`
- Never use raw hex values in CSS or `style=` attributes — always `var(--color-*)`
- Never insert raw `phase` strings into the DOM — always `formatPhaseLabel()`
- Always use `esc()` for server-supplied string values in JS-generated HTML
- Run `.venv/bin/pytest` (not `python -m pytest`) to avoid pyenv shim issues
- Commit after each phase checkpoint to keep history reviewable
