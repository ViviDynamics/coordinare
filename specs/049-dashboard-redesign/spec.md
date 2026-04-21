# Feature Specification: Dashboard Redesign

**Feature Branch**: `049-dashboard-redesign`  
**Created**: 2026-04-16  
**Status**: Draft  
**Input**: Dashboard redesign with navbar for navigation, multi-page layout. Move advanced features (persona editor) to their own pages. Main dashboard emphasises observability: which performers are active, metrics, feedback-cycle usage, cost. Workflow diagram smaller and less prominent. Active performer state more visible than the static diagram.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Main dashboard shows active performers at a glance (Priority: P1)

When the operator opens the dashboard, the most prominent element is a live view of which performer roles are currently active, which card each is working on, and how long the current session has been running. The workflow diagram is still present but reduced in size — it provides context, not the primary signal. The operator can tell within 3 seconds which cards are in-flight and at which stage.

**Why this priority**: During live testing, the most common question was "what's happening right now?" The current dashboard buries this under the workflow diagram and phase label. The active-performer view answers the question immediately.

**Independent Test**: Start the coordinare with two cards in-flight (one in implementing, one in reviewing). Open the dashboard. Verify both active sessions are visible with card title, role, and elapsed time — without scrolling.

**Acceptance Scenarios**:

1. **Given** two active sessions (implementer on card #89, reviewer on card #91), **When** the operator opens the dashboard, **Then** both sessions are visible above the fold with card title, role name, and elapsed time.
2. **Given** no active sessions (coordinare idle), **When** the operator opens the dashboard, **Then** the active-performer area shows "No active performers" with the current phase (idle) and last poll timestamp.
3. **Given** the workflow diagram is visible, **When** comparing its size to the active-performer view, **Then** the active-performer view occupies more visual weight (larger area, bolder typography).

---

### User Story 2 — Navbar enables multi-page navigation (Priority: P2)

The dashboard has a persistent top navigation bar with links to: Dashboard (home), Performers, Personas, and History. Clicking a nav item loads the corresponding page without a full page reload. The current page is visually indicated in the nav.

**Why this priority**: The single-page dashboard has accumulated too many features (persona editor, force-poll button, event stream, metrics, workflow diagram). A navbar lets each concern live on its own page, reducing clutter and making each page faster to scan.

**Independent Test**: Open the dashboard. Click each nav item. Verify the page content changes and the active nav item is highlighted. Verify the browser URL updates so bookmarks and direct links work.

**Acceptance Scenarios**:

1. **Given** the operator is on the Dashboard page, **When** they click "Personas" in the navbar, **Then** the persona editor page loads without a full reload and "Personas" is highlighted in the nav.
2. **Given** the operator navigates to `/performers`, **When** the page loads, **Then** the Performers page content is shown and the nav highlights "Performers".
3. **Given** the operator bookmarks `/personas`, **When** they open the bookmark later, **Then** the Personas page loads directly.

---

### User Story 3 — Persona editor on its own page (Priority: P3)

The persona editor (currently inline on the main dashboard) moves to a dedicated `/personas` page. The page shows each configured role, its current instructions, and an edit button. Editing is the same inline-edit experience as today, but on its own page with no competing dashboard widgets.

**Why this priority**: The persona editor is used infrequently (during setup or after a lifecycle failure) but takes up significant vertical space on the main dashboard. Moving it to its own page frees space for observability on the home page and gives the editor more room when it's actually needed.

**Independent Test**: Navigate to `/personas`. Verify all configured roles are listed with their instructions. Edit one role's instructions. Verify the change persists after navigating away and back.

**Acceptance Scenarios**:

1. **Given** 8 performer roles configured, **When** the operator opens `/personas`, **Then** all 8 roles are listed with their current instructions and an edit button.
2. **Given** the operator edits the reviewer's instructions and saves, **When** they navigate to Dashboard and back to Personas, **Then** the reviewer's instructions reflect the saved change.
3. **Given** the coordinare is running, **When** the operator edits a persona on `/personas`, **Then** the change takes effect on the next dispatch (hot-reload) without restarting the coordinare.

---

### User Story 4 — Performers page shows per-role status and history (Priority: P4)

A dedicated `/performers` page shows each role's current state: active/idle, current card (if active), session duration, last completion time, and recent event history (last 10 events per role). In multi-card mode with horizontal scaling (spec 048, when available), this page shows utilization per role (active/max/queued).

**Why this priority**: Debugging lifecycle issues requires knowing which roles are healthy and which are stalling. Today this information is only in the logs. A dedicated page surfaces it without log-grepping.

**Independent Test**: Navigate to `/performers` while the implementer is active. Verify the implementer row shows "active" with the card title and session duration. Check that the event history shows recent dispatch/complete events.

**Acceptance Scenarios**:

1. **Given** the implementer is active on card #89, **When** the operator views `/performers`, **Then** the implementer row shows status "active", card "#89 — [title]", and elapsed time.
2. **Given** the reviewer is idle, **When** the operator views `/performers`, **Then** the reviewer row shows "idle" with the timestamp of its last completion.
3. **Given** horizontal scaling is configured (spec 048), **When** the operator views `/performers`, **Then** each role shows "N / M active" (utilization) and queued count if at capacity.

---

### Edge Cases

- **Dashboard opened before coordinare starts**: All sections show sensible empty states — "No data yet" or "Waiting for first poll" — not broken HTML or JavaScript errors.
- **Browser back/forward navigation**: The navbar uses client-side routing with `pushState` so back/forward buttons work correctly between pages.
- **Mobile / narrow viewport**: The navbar collapses to a hamburger menu. Content reflows to a single column. No horizontal scrolling.
- **SSE disconnection**: If the SSE event stream drops, the dashboard shows a reconnection indicator and auto-reconnects. Data that arrived during the disconnect is fetched on reconnection via a snapshot endpoint.
- **Many active cards (10+)**: The active-performer view on the main dashboard scrolls vertically if needed. The most recently dispatched cards appear first.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The main dashboard page MUST show an active-performer view as the primary element: role name, card title + issue number, and session elapsed time for each running performer session.
- **FR-002**: The workflow diagram MUST still be present on the main dashboard but reduced in size (no more than 25% of the viewport height) and positioned below or beside the active-performer view.
- **FR-003**: A persistent top navbar MUST be present on all pages with links to: Dashboard, Performers, Personas, and History.
- **FR-004**: Navigation MUST use client-side routing so page transitions don't require a full reload and browser back/forward buttons work.
- **FR-005**: The persona editor MUST be moved from the main dashboard to a dedicated `/personas` page.
- **FR-006**: A `/performers` page MUST show per-role status (active/idle), current card, session duration, last completion time, and recent events (last 10 per role).
- **FR-007**: When no performer sessions are active, the main dashboard MUST show the current coordinare phase, last poll timestamp, and board snapshot summary (counts per column).
- **FR-008**: The navbar MUST visually indicate which page is currently active.
- **FR-009**: All pages MUST display graceful empty states when no data is available yet.
- **FR-010**: The existing SSE event stream MUST continue to power real-time updates across all pages. Each page subscribes to the events it needs.

### Key Entities

- **DashboardPage**: A navigable view within the dashboard application. Contains: route path, page title, page component, required SSE event subscriptions.
- **ActivePerformerTile**: A visual card on the main dashboard representing one running performer session. Contains: role name, card title, issue number, issue URL, session elapsed time, last event summary.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Operator can identify which performers are active and which cards they're working on within 3 seconds of opening the dashboard — no scrolling required when 3 or fewer sessions are active.
- **SC-002**: Navigation between pages completes in under 200ms (no full-page reload).
- **SC-003**: The persona editor on `/personas` is functionally identical to the current inline editor — all existing edit/save/hot-reload behavior is preserved.
- **SC-004**: The `/performers` page loads with per-role status data within 1 second of navigation, even with 8 configured roles.
- **SC-005**: The main dashboard's workflow diagram occupies no more than 25% of the initial viewport height on a 1080p display.

## Assumptions

- The dashboard continues to use the existing SSE event stream and snapshot API. No new backend APIs are needed beyond reorganizing how the frontend subscribes to and displays events.
- The dashboard is a single-page application served by the coordinare's FastAPI server. All routing is client-side (vanilla JavaScript `pushState`) — no server-side rendering or framework required.
- The `/performers` page shows utilization data from spec 048 (horizontal scaling) when available, but degrades gracefully to showing "1 / 1 active" when scaling is not configured.
- History page content is deferred to a future spec. For now, the nav link is present but the page shows "Coming soon."
