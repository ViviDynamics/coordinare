# Feature Specification: Dashboard UX Redesign

**Feature Branch**: `059-dashboard-ux`  
**Created**: 2026-05-07  
**Status**: Draft  
**Input**: User description: "Dashboard UX redesign: improve the coordinare web dashboard's overall usability through a design cycle — better information hierarchy, clearer status communication, improved layout and navigation, more actionable operator experience"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Understand System Status at a Glance (Priority: P1)

An operator opens the dashboard and within seconds knows: is the system healthy, what is actively being worked on, and does anything need their attention right now? Today the dashboard requires reading multiple scattered cards, parsing cryptic phase names, and mentally assembling a picture across sections. After this redesign, the operator gets an immediate, confident read on overall state without scrolling or cross-referencing.

**Why this priority**: This is the primary use case for the dashboard. Everything else is secondary to answering "is the system OK and what's happening?"

**Independent Test**: Can be fully tested by opening the dashboard cold (no prior context) and correctly stating what is active, what is healthy, and whether human action is required — within 10 seconds.

**Acceptance Scenarios**:

1. **Given** the coordinare has one active card in the `monitoring_agent` phase, **When** an operator opens the dashboard, **Then** the active card is prominently displayed with a human-readable phase label, elapsed time, and a clear visual indicator that no human action is needed.
2. **Given** a PR is awaiting human review, **When** the operator opens the dashboard, **Then** a distinct visual call-to-action prominently communicates "needs your attention" without requiring the operator to hunt for it.
3. **Given** all subsystems are healthy, **When** the operator glances at the dashboard, **Then** system health is communicated by a single unobtrusive indicator, not a wall of green statuses.
4. **Given** a subsystem is degraded, **When** the operator views the dashboard, **Then** the degraded subsystem is immediately visually prominent and includes a plain-language description of the problem.

---

### User Story 2 - Navigate to the Right Information Quickly (Priority: P2)

An operator knows what they're looking for — performer logs, symphony configuration, cycle history — and wants to reach it in one or two clicks without hunting through menus or scrolling past irrelevant content. Today, navigation is a flat list of links, many of which lead to pages that are visually identical and hard to orient within. After this redesign, the operator can find any piece of information quickly and always knows where they are.

**Why this priority**: Operators returning to the dashboard for specific information (debugging, configuration review) need efficient navigation. Poor navigation wastes time and erodes trust in the tool.

**Independent Test**: Can be fully tested by starting from any page and reaching any other page in at most one click, and by confirming the active page is always visually indicated in the navigation.

**Acceptance Scenarios**:

1. **Given** the operator is on the History page, **When** they want to view Performer details, **Then** they can navigate there in one click and the active nav item updates to reflect the current page.
2. **Given** the operator is on a detail view (e.g., a specific performer's log), **When** they want to return to the parent list, **Then** a clear back affordance is always visible and functional.
3. **Given** the dashboard is displayed on a 1280px-wide screen, **When** the operator uses the navigation, **Then** all nav items are visible and accessible without a hamburger expand step.
4. **Given** the operator is on the Global Config page, **When** they save a change, **Then** feedback (success or error) is visible on that page without navigating away.

---

### User Story 3 - Understand Individual Card and Performer Progress (Priority: P3)

An operator wants to monitor a specific active card — what phase it's in, how long it has been there, what the performer is doing, and what has happened so far in this session. Today this information is split across the Active Work card, the Performers card, and the performer log detail view, requiring multiple clicks and mental assembly. After this redesign, an operator can drill into a single card and see its full story — current state, elapsed time, cost, recent log activity — in one place.

**Why this priority**: Detailed card monitoring is important but secondary to the overview. Most operators only need it when investigating a slow or stuck card.

**Independent Test**: Can be fully tested by clicking an active card and verifying that phase, performer, elapsed time, cost, and recent log lines are all visible without additional navigation.

**Acceptance Scenarios**:

1. **Given** an active card is being processed by a performer, **When** the operator clicks it, **Then** a detail view shows the current phase, assigned performer, elapsed time, running cost estimate, and the last N log lines — all in one view.
2. **Given** a card has been in the same phase for longer than the alert threshold, **When** the operator views the card detail, **Then** elapsed time is visually distinct to draw attention to the duration.
3. **Given** the operator is viewing a performer log stream, **When** new log lines arrive, **Then** they appear in the detail view without a full page reload.

---

### User Story 4 - Operate Confidently on Small Screens (Priority: P4)

An operator checking on the system from a laptop or tablet can use the full dashboard without horizontal scrolling, layout breakage, or hidden content. Today, many inner sections use fixed-width inline styles and dense table layouts that overflow on narrower screens. After this redesign, the dashboard is fully usable at 768px and above.

**Why this priority**: Operators frequently check on system status from non-workstation contexts. A broken layout erodes confidence in the tool.

**Independent Test**: Can be fully tested by resizing the browser to 768px and verifying all primary information (active work, health, navigation) is accessible and not clipped.

**Acceptance Scenarios**:

1. **Given** the viewport is 768px wide, **When** the operator loads the dashboard, **Then** no content is clipped, no horizontal scrollbar appears, and all primary panels are readable.
2. **Given** the viewport is 768px wide and the operator navigates to the History page, **Then** cycle history rows are readable and table columns are either visible or gracefully collapsed.

---

### Edge Cases

- What happens when there are no active cards and no performers? The dashboard must display a clear "idle" state rather than a collection of empty panels.
- What happens when there are 10+ active cards simultaneously? The active work panel must scroll or paginate gracefully rather than overflow.
- What happens when a subsystem reports an unknown status string? The dashboard must render it legibly rather than blank or error silently.
- What happens when the SSE connection drops? The connection status indicator must update immediately; reconnection progress must be visible without a page reload.
- What happens when a phase label is a long compound string? Labels must truncate or wrap gracefully without breaking card layout.
- What happens when cost data is unavailable for a card? The cost display must show a clear placeholder rather than "$0.00" which implies a real value.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The dashboard home page MUST present system status (active work, human attention needed, overall health) without requiring scroll on a 1080p display.
- **FR-002**: The dashboard MUST display a clear visual distinction between cards that are actively progressing vs. cards awaiting human action.
- **FR-003**: Phase labels MUST be displayed in human-readable form (e.g. "Monitoring Agent" not "monitoring_agent") everywhere they appear on the dashboard.
- **FR-004**: The active navigation item MUST be visually indicated at all times so the operator always knows which page they are on.
- **FR-005**: Subsystem health statuses MUST be summarised into a single health indicator on the home page; individual subsystem details MUST be accessible on demand (e.g., expandable section) rather than always visible.
- **FR-006**: An operator MUST be able to reach a performer's log stream from an active card detail in at most two interactions from the home page.
- **FR-007**: The dashboard MUST display elapsed time for each active card and MUST visually flag cards whose elapsed time exceeds a threshold (default: 30 minutes).
- **FR-008**: Running cost estimates MUST be visible on active cards in the overview without requiring a detail view.
- **FR-009**: The SSE connection status MUST be visible at all times; a disconnected state MUST be visually distinct from connected.
- **FR-010**: All pages MUST be usable at viewport widths of 768px and above without horizontal overflow.
- **FR-011**: The dashboard MUST display a meaningful idle state when no cards are active and no performers are busy, rather than a collection of empty panels.
- **FR-012**: Navigation MUST allow reaching any primary page in at most one click from any other page.
- **FR-013**: Inline actions (e.g., force-poll, save config) MUST display success or error feedback inline without navigating away from the current page.
- **FR-014**: The performer detail view MUST show current phase, assigned card, elapsed time, cost, and recent log lines in a single consolidated view.

### Key Entities

- **Active Card**: A GitHub project card currently being processed; has a phase, elapsed time, assigned performer, and cost.
- **Awaiting-Review Card**: A card with an open PR waiting for human approval; visually distinguished from active cards.
- **Performer**: An agent instance with an availability status, current assignment, and log stream.
- **Subsystem**: An internal health-monitored component; has a status and optional detail message.
- **Cycle**: A completed orchestration loop recorded in history; has a timestamp, duration, and outcome.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can correctly assess overall system state (healthy/unhealthy, active/idle, action needed/not needed) within 10 seconds of opening the dashboard, without scrolling.
- **SC-002**: An operator can navigate from any page to any other primary page in a single interaction.
- **SC-003**: An operator can reach a performer's log stream from the dashboard home page in at most two interactions.
- **SC-004**: All primary dashboard content is accessible and readable at 768px viewport width without horizontal scrolling.
- **SC-005**: The dashboard correctly renders a distinct idle state when no active work is in progress.
- **SC-006**: Phase labels appear in human-readable form in 100% of dashboard surface areas (no raw snake_case strings visible to operators).
- **SC-007**: The SSE connection status indicator updates within 5 seconds of a real connection change.
- **SC-008**: Cards whose elapsed time exceeds 30 minutes are visually differentiated from normal-duration cards.

## Scope

### In Scope

- Layout and information hierarchy of the dashboard home page
- Navigation structure and active-page indication
- Active Work and Awaiting Review panel presentation and visual distinction
- Subsystem health summary (collapsing always-visible detail into summary + on-demand view)
- Performer detail view consolidation (phase, card, elapsed, cost, logs in one place)
- Phase label rendering (human-readable everywhere)
- Elapsed time display and threshold-based visual flagging
- Idle state presentation
- Responsive layout fixes at 768px+
- SSE connection status indicator behaviour
- Inline feedback for user actions (force-poll, config save)
- Cost estimate visibility on active card overview

### Out of Scope

- New data or backend API changes (display-layer changes only; no new endpoints)
- New configuration options exposed via the dashboard
- Mobile (< 768px) support
- Dark/light theme toggling
- Internationalisation or localisation
- Notification or alerting UI
- Changes to performer, symphony, or persona data models

## Assumptions

- The dashboard remains a single-file server-side-rendered HTML application with inline CSS and JavaScript; no frontend build toolchain is introduced.
- All data needed for the redesigned views is already present in the SSE state snapshot or existing API endpoints; no new backend routes are required.
- The 30-minute elapsed-time threshold for visual flagging is hardcoded initially; configurability is a future enhancement.
- "768px and above" means the existing layout is adjusted to reflow gracefully, not a ground-up responsive rebuild.
- The existing dark colour scheme and brand identity are preserved; this is a usability and hierarchy redesign, not a visual rebrand.
