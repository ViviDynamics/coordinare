# Tasks: Dashboard Redesign

**Input**: Design documents from `/specs/049-dashboard-redesign/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Phase 1: Setup

**Purpose**: Shared infrastructure — all user stories depend on these

- [x] T001 Add 3 new FastAPI routes to src/coordinare/dashboard.py — `/performers`, `/personas`, `/history` — each returns `HTMLResponse(_DASHBOARD_HTML)` using the existing HTML shell (same as `/`)
- [x] T002 Add CSS for navbar to src/coordinare/dashboard.py — persistent `<nav>` element with links to Dashboard, Performers, Personas, History; active-link highlighting via `.nav-active` class; hamburger toggle for narrow viewports (<768px)
- [x] T003 Add JS router() function to src/coordinare/dashboard.py — reads `location.pathname`, calls the matching page-render function, updates navbar active state; called on DOMContentLoaded and `window.addEventListener('popstate')`
- [x] T004 Add JS `navigate(path)` helper to src/coordinare/dashboard.py — calls `pushState`, updates `<title>`, calls `router()`; intercept `<a>` clicks in navbar to use navigate() instead of full reload

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core layout changes required by all user stories

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T005 Insert `<nav>` bar HTML at the top of `_DASHBOARD_HTML` in src/coordinare/dashboard.py — before the existing `<div id="app">` wrapper; add `id="main-content"` wrapper div that all page-renders target
- [x] T006 Add page-visibility helper `showPage(sections)` to src/coordinare/dashboard.py JS — takes list of section IDs to show, hides all others; used by each page-render function to toggle sections
- [x] T007 Write test for new FastAPI routes in tests/unit/test_dashboard.py — verify GET /performers, GET /personas, GET /history all return 200 with the same HTML shell as GET /

**Checkpoint**: Navbar visible on page, routes return HTML, JS router wired. No visible content changes yet.

---

## Phase 3: User Story 1 — Active performers view (Priority: P1) 🎯 MVP

**Goal**: Main dashboard leads with per-session active-performer tiles. Workflow diagram reduced to ≤25vh.

**Independent Test**: Two active sessions in-flight → open `/` → both visible above fold within 3 seconds.

- [x] T008 [US1] Add `#active-performers` section HTML to `_DASHBOARD_HTML` in src/coordinare/dashboard.py — placed above the workflow diagram; contains a `<div id="active-performer-tiles">` grid container
- [x] T009 [US1] Add CSS for active-performer tiles to src/coordinare/dashboard.py — CSS grid, 1–3 tiles per row; each tile shows role badge, card title, issue number, elapsed time badge, last event; bold typography; empty state "No active performers" tile
- [x] T010 [US1] Add `renderActivePerformers(s)` JS function to src/coordinare/dashboard.py — derives tiles from `s.active_sessions`; computes elapsed time from `s.agent_dispatch_at`; renders tiles or empty-state message; called from `renderDashboard()`
- [x] T011 [US1] Reduce workflow diagram CSS max-height to 25vh in src/coordinare/dashboard.py — add `max-height: 25vh; overflow: hidden;` to the mermaid/diagram container; add optional expand toggle button
- [x] T012 [US1] Add `renderDashboard(s)` page function to src/coordinare/dashboard.py — calls `showPage(['active-performers', 'phase-card', 'active-card', 'workflow', 'metrics-section', 'cycles-section', 'subsystems-section', ...])` and calls `renderActivePerformers(s)`; registered in router() for path "/"
- [x] T013 [US1] Update `renderState(s)` to call `router()` approach — ensure active-performer tiles refresh on each SSE update when on the `/` page
- [x] T014 [US1] Add board snapshot summary to idle state in src/coordinare/dashboard.py — when no active sessions, show column counts (TODO, IN_PROGRESS, IN_REVIEW, DONE) from `s.board_snapshot`

**Checkpoint**: Main page shows active performer tiles above fold; diagram reduced; idle state shows board counts.

---

## Phase 4: User Story 2 — Navbar multi-page navigation (Priority: P2)

**Goal**: Persistent navbar with client-side routing; URL updates; back/forward work.

**Independent Test**: Click "Performers" → URL becomes /performers, nav highlights; browser back → returns to /.

- [x] T015 [US2] Wire router() into renderState(s) in src/coordinare/dashboard.py — on each SSE update, re-render the current page by calling the appropriate page function; preserves page-specific state between updates
- [x] T016 [US2] Add `<title>` update to navigate() helper in src/coordinare/dashboard.py — update document.title to match the page ("Dashboard — Coordinare", "Performers — Coordinare", etc.)
- [x] T017 [US2] Verify SSE stream stays connected across navigation in src/coordinare/dashboard.py — EventSource is created once; navigate() must NOT recreate it; verify no memory leaks on repeated navigation
- [x] T018 [US2] Add graceful empty-state rendering for each page in src/coordinare/dashboard.py — all pages show sensible content when `s` is null or missing fields (e.g. "Waiting for first poll..." )

**Checkpoint**: All 4 nav links work; URL updates; back/forward navigate correctly; SSE stays connected.

---

## Phase 5: User Story 3 — Persona editor on /personas page (Priority: P3)

**Goal**: Persona editor moved off main dashboard to /personas; functionally identical.

**Independent Test**: Open /personas → all roles listed; edit one → save; navigate away and back → change persisted.

- [x] T019 [US3] Add `renderPersonas(s)` page function to src/coordinare/dashboard.py — calls `showPage(['personas-section'])` and calls existing `loadPersonas()`; registered in router() for path "/personas"
- [x] T020 [US3] Hide `#personas-section` from main dashboard renderDashboard() call in src/coordinare/dashboard.py — remove personas from the sections shown on "/"
- [x] T021 [US3] Ensure `loadPersonas()` is called on entry to /personas page in src/coordinare/dashboard.py — called from `renderPersonas()`; not called on "/" (saves an API call per cycle)

**Checkpoint**: /personas page shows editor; main dashboard no longer shows persona editor; edit/save/hot-reload preserved.

---

## Phase 6: User Story 4 — Performers page (Priority: P4)

**Goal**: /performers page shows per-role status, card, elapsed time, event history, utilization.

**Independent Test**: Navigate to /performers with implementer active → row shows "active", card title, elapsed time.

- [x] T022 [US4] Add `#performers-page` section HTML to `_DASHBOARD_HTML` in src/coordinare/dashboard.py — table/list of per-role rows; separate from existing `#performers-card` (which stays on main dashboard for the compact view)
- [x] T023 [US4] Add `renderPerformersPage(s)` function to src/coordinare/dashboard.py — derives per-role status from `s.role_utilization` + `s.active_sessions` + `s.performer_events`; shows active/idle badge, card titles, elapsed time, queued count, last 10 events per role; registered in router() for path "/performers"
- [x] T024 [US4] Add CSS for performers page table in src/coordinare/dashboard.py — role badge colors, status indicators (green=active, grey=idle), event log list styling, utilization bar reusing existing role_utilization styles

**Checkpoint**: /performers page shows all configured roles with current status and event history.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [x] T025 Add History stub page in src/coordinare/dashboard.py — `renderHistory()` shows "History — Coming soon" message; registered in router() for path "/history"
- [x] T026 Add SSE disconnection indicator to navbar in src/coordinare/dashboard.py — when SSE is disconnected, show a small dot/badge on the navbar title; reuse existing reconnect logic
- [x] T027 Test all 5 quickstart scenarios from specs/049-dashboard-redesign/quickstart.md — manually verify each scenario in browser
- [x] T028 Run .venv/bin/pytest tests/unit/test_dashboard.py -q — all tests pass
- [x] T029 Run .venv/bin/ruff check src/coordinare/dashboard.py — lint clean

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — routes + CSS + JS infrastructure
- **Phase 2 (Foundational)**: Depends on Phase 1 — HTML structure + showPage helper
- **Phase 3 (US1)**: Depends on Phase 2 — MVP active-performer view
- **Phase 4 (US2)**: Depends on Phase 2 — navbar routing (independent of US1 content)
- **Phase 5 (US3)**: Depends on Phase 2 — persona page (independent of US1/US2 content)
- **Phase 6 (US4)**: Depends on Phase 2 + Phase 4 (needs router) — performers page
- **Phase 7 (Polish)**: Depends on all prior phases

### User Story Dependencies

- **US1 (P1)**: Foundational only — MVP standalone (active-performer view)
- **US2 (P2)**: Foundational only — independent of US1 (routing works without new content)
- **US3 (P3)**: Foundational + router (Phase 4) — personas page needs routing
- **US4 (P4)**: Foundational + router (Phase 4) — performers page needs routing

---

## Implementation Strategy

### MVP First (User Story 1 + foundational routing)

1. Complete Phase 1: Routes + CSS + router infrastructure
2. Complete Phase 2: HTML shell + showPage
3. Complete Phase 3: Active-performer tiles + diagram reduction
4. **STOP and VALIDATE**: Open browser, verify active performers visible above fold
5. Deploy if ready

### Incremental Delivery

1. Phases 1-3 → active-performer view (MVP)
2. Phase 4 → navbar routing (all pages accessible)
3. Phase 5 → personas on own page
4. Phase 6 → performers page with utilization
5. Phase 7 → polish

---

## Notes

- All changes are in **one file**: `src/coordinare/dashboard.py` (~1625 lines, will grow to ~1900)
- No new Python files, no new test files beyond test_dashboard.py additions
- JS is inline in the HTML template string — no bundler, no transpilation
- The existing SSE stream, snapshot, and API endpoints are **unchanged**
- `showPage()` is the key abstraction — controls section visibility per route
