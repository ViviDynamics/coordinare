# Research: Dashboard Redesign

## R1: Client-Side Routing Strategy

**Decision**: Vanilla JS `pushState` + `window.addEventListener('popstate')` for client-side routing. Same HTML shell served for all routes (`/`, `/performers`, `/personas`, `/history`). JS reads `location.pathname` on load and `popstate` events to render the correct page.

**Rationale**: No build tooling, no framework dependencies. The existing dashboard is already a single HTML blob with JS toggling DOM sections. Adding a router is a natural extension. `pushState` provides real URLs for bookmarks and back/forward.

**Alternatives considered**:
- Hash routing (`#/performers`): Simpler but URLs look ugly and some integrations (like direct linking from Slack/email) handle `#` differently.
- Full page reload on each nav: Defeats the purpose. SSE reconnect cost on every navigation.

## R2: HTML Shell Strategy

**Decision**: All 4 routes (`/`, `/performers`, `/personas`, `/history`) return the same `_DASHBOARD_HTML` template. The JS `router()` function is called on DOMContentLoaded and on `popstate`, and it shows/hides or renders the appropriate page sections.

**Rationale**: FastAPI just adds 3 new `@app.get` routes that each return `HTMLResponse(_DASHBOARD_HTML)`. Zero template duplication. The SSE stream, snapshot state, and all existing APIs continue to work because they're backend-only.

**Alternatives considered**:
- Server-side rendering with separate HTML templates: Requires Jinja2, more backend logic, and a larger refactor.
- URL fragment routing with `#`: Works but gives ugly URLs and no pushState semantics.

## R3: Active-Performer View Design

**Decision**: A new `#active-performers` section at the top of the dashboard page. When sessions are active, shows a row of "tiles" — one per active session — with role name, card title + issue number, elapsed time badge, and last event. Uses CSS grid for 1–3 tiles per row. When idle, shows a single "No active performers" tile with phase and last poll time.

**Rationale**: The spec says "within 3 seconds, no scrolling required for ≤3 sessions." CSS grid with tiles achieves this. The existing SSE snapshot already includes `active_sessions` data.

**Alternatives considered**:
- Table layout: More compact but less scannable for "at a glance."
- Timeline/swimlane: Over-engineered for 2-3 active cards.

## R4: Workflow Diagram Reduction

**Decision**: Add `max-height: 25vh; overflow: hidden;` CSS to the workflow diagram container. Position it below the active-performer view and metrics summary. Keep it collapsible via a toggle if the operator wants to expand it.

**Rationale**: The spec requires ≤25% viewport height. CSS is the minimal change — no HTML restructuring needed. The Mermaid diagram is already rendered as SVG inside a container we can size.

**Alternatives considered**:
- Remove the diagram entirely: Spec says it must stay; the operator may still want context.
- Reduce node count in the diagram: Changes the backend rendering logic unnecessarily.

## R5: Navbar Implementation

**Decision**: Simple `<nav>` bar at the top of the page with 4 `<a>` links. CSS highlights the active link by matching `location.pathname`. Click handler calls `pushState` + `router()` to avoid full reload. Mobile: hamburger toggle via a `<button>` and CSS `display:none` on narrow viewports.

**Rationale**: Vanilla CSS+JS, no dependencies. Matches the existing inline-style approach of the dashboard.

**Alternatives considered**:
- Tab-based navigation: Would require more significant layout changes.
- Sidebar: More complex layout, not standard for single-operator dashboards.

## R6: Performers Page Data

**Decision**: The `/performers` page derives per-role status from `state.active_sessions` (which cards are in monitoring_performer per stage), `state.role_utilization` (from spec 048 SlotManager), and `state.performer_events`. "Last completion time" is derived from the cycle history. No new backend data needed.

**Rationale**: All required data is already in the SSE snapshot. The page just renders it differently from the main dashboard.

**Alternatives considered**:
- New `/api/performers` endpoint: Unnecessary since snapshot already has the data.
