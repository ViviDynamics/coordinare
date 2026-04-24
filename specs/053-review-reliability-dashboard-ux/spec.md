# Feature Specification: Review Reliability and Dashboard UX Completion

**Feature Branch**: `053-review-reliability-dashboard-ux`  
**Created**: 2026-04-22  
**Status**: Draft  
**Input**: Coordinare is getting hard-blocked when reviewer backend output is not JSON, and the dashboard redesign still has critical UX gaps: weak Active Performers idle summary, History page still stubbed, no role-detail drilldown from /performers, and workflow/performers cards still over-sized on desktop.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Reviewer output failures do not hard-block normal flow (Priority: P1)

When the reviewer performer returns prose or progress narration instead of the required JSON object, the system performs a structured recovery attempt and avoids immediately hard-blocking the card. Operators get a clear diagnostic if retries are exhausted, and the error is classified as backend-format reliability rather than normal review feedback.

**Why this priority**: Current runs can halt in `blocked` even when code is healthy, purely because backend output format drifted. This is an availability issue.

**Independent Test**: Simulate a reviewer backend response that is non-JSON on first pass and valid JSON on second pass. Verify lifecycle continues without entering `blocked`.

**Acceptance Scenarios**:

1. **Given** reviewer stage returns non-JSON text, **When** status is polled, **Then** performer executes a JSON-format recovery attempt and returns `working` instead of terminal `error`.
2. **Given** recovery output is valid JSON, **When** next status poll occurs, **Then** stage returns normal reviewer outcome (`approved` or `changes_requested`).
3. **Given** all recovery attempts are exhausted, **When** terminal failure is reported, **Then** error reason includes stage, retry counts, and output preview without leaking secrets.
4. **Given** terminal format failure occurred, **When** monitor_performer handles the error, **Then** it is routed through a system-error/retry path before final human-blocking.

---

### User Story 2 - Active Performers card has meaningful idle observability (Priority: P1)

When no performer is active, the Active Performers card shows actionable summary data (board column counts and last board poll) instead of only phase/cycles.

**Why this priority**: Idle-time observability is the primary dashboard use-case between dispatches.

**Independent Test**: Run coordinare in idle state with populated `board_snapshot`; open dashboard and verify the idle tile includes TODO/IN_PROGRESS/IN_REVIEW/DONE counts and last poll timestamp.

**Acceptance Scenarios**:

1. **Given** `active_sessions` is empty and board snapshot exists, **When** dashboard renders, **Then** idle tile includes board counts by column.
2. **Given** `last_poll_at` exists, **When** idle tile renders, **Then** it includes a human-readable last poll time.
3. **Given** `assignee_filter` is configured, **When** idle tile renders, **Then** filter hint is shown with the board summary.

---

### User Story 3 - History page is functional, not a stub (Priority: P2)

The `/history` route displays real historical data and is no longer a "Coming soon" placeholder.

**Why this priority**: Operators need timeline context without staying on the main dashboard.

**Independent Test**: After several cycles, navigate to `/history`; verify cycle history table is rendered and updates via SSE.

**Acceptance Scenarios**:

1. **Given** cycle history entries exist, **When** `/history` loads, **Then** page shows timestamp, phase, duration, and outcome rows.
2. **Given** no history exists yet, **When** `/history` loads, **Then** page shows an explicit empty state (not placeholder text).
3. **Given** new cycle completes while on `/history`, **When** SSE update arrives, **Then** history table refreshes without full reload.

---

### User Story 4 - Performers page supports drilldown details (Priority: P2)

Operators can click a role row on `/performers` to open detailed live view (session/log/metrics/backend link), instead of having detail view only on the main dashboard card.

**Why this priority**: The dedicated page currently loses the most valuable debug interaction.

**Independent Test**: Navigate to `/performers`, click an active role row, verify detailed panel appears and back navigation returns to list.

**Acceptance Scenarios**:

1. **Given** a role is active, **When** operator clicks its row on `/performers`, **Then** detail panel opens with live events and metrics.
2. **Given** detail panel is open, **When** operator clicks back, **Then** list view restores without route reload.
3. **Given** no role is active, **When** operator clicks an idle row, **Then** detail panel still shows recent role context with clear idle labeling.

---

### User Story 5 - Workflow and Performers cards use single-column footprint on desktop (Priority: P2)

On desktop widths, the workflow card and compact performers card each occupy one grid column (side-by-side) rather than both spanning full width.

**Why this priority**: The current layout over-allocates vertical space and hides higher-value information.

**Independent Test**: Open `/` at >=900px width and verify workflow card and performers card each render as one-column cards.

**Acceptance Scenarios**:

1. **Given** viewport width >=900px, **When** dashboard loads, **Then** workflow card and performers card each occupy one grid column.
2. **Given** viewport width <900px, **When** dashboard loads, **Then** layout degrades to single-column stack without horizontal scrolling.
3. **Given** workflow diagram is collapsed by default, **When** dashboard loads, **Then** no expand/collapse control is required for baseline readability.

---

### Edge Cases

- Reviewer backend emits mixed prose + JSON fenced block; parser must extract valid object without false positives.
- Backend output contains secrets/tokens in text; diagnostics must remain redacted.
- `board_snapshot` missing keys (for example BLOCKED/BACKLOG only); idle summary must default missing counts to 0.
- `/performers` role row click while SSE updates are streaming; selected detail view should remain stable.
- `/history` loaded before first cycle; page must render clean empty state.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Performer MUST apply role-aware terminal prompt behavior so reviewer-style stages do not receive generic "commit your changes" directives.
- **FR-002**: For stages that require JSON (`assessing`, `reviewing`, `closing_review`, `security`, `qa`, `documenting`), non-JSON backend output MUST trigger a format-recovery attempt before terminal failure.
- **FR-003**: Format-recovery diagnostics MUST remain secret-redacted and include stage + retry context.
- **FR-004**: Monitor path for backend-format terminal errors MUST use retryable system-error handling before final `blocked` escalation.
- **FR-005**: Dashboard SSE snapshot MUST include board summary counts derived from `state["board_snapshot"]` and include `last_poll_at`.
- **FR-006**: Active performers idle state MUST render board summary counts and last poll in the idle tile.
- **FR-007**: `/history` page MUST render live cycle history using existing snapshot data (timestamp, phase, duration, outcome).
- **FR-008**: History page MUST remove the "Coming soon" placeholder text.
- **FR-009**: `/performers` page MUST support click-to-detail interaction for each role row.
- **FR-010**: Detailed performer view on `/performers` MUST surface live events, stderr logs, metrics, backend URL, and session stats (same data class currently shown on dashboard detail).
- **FR-011**: On desktop widths, workflow and compact performers cards on `/` MUST not use `.full` width and should render side-by-side.
- **FR-012**: Mobile layout MUST remain single-column and readable.
- **FR-013**: Existing dashboard route/navigation behavior (`pushState`, SSE persistence) MUST continue unchanged.

### Key Entities

- **BackendFormatFailure**: Structured representation of backend output format mismatch. Includes stage, raw preview (redacted), retry count, and recovery attempt status.
- **BoardSummary**: Dashboard summary object derived from `board_snapshot` with per-column counts and `last_poll_at`.
- **PerformerRoleViewState**: Frontend route state for `/performers` list/detail selection and active role context.

## UX / Design Improvement Plan

### Design Intent

- Make "what is happening now?" immediately visible in under 3 seconds.
- Reduce vertical sprawl on desktop by prioritizing current state over static explanatory visuals.
- Keep interaction model consistent across routes (same nav, same status language, same empty-state style).

### Information Hierarchy

1. Primary: Active performers (active sessions or meaningful idle summary).
2. Secondary: Current phase/card and performer diagnostics.
3. Tertiary: Workflow diagram and historical context.
4. Administrative: Personas editor and low-frequency config surfaces.

### Layout Plan by Route

- **Dashboard (`/`)**: Top area shows Active Performers (full width). Below it, desktop (`>=900px`) uses two columns with workflow and compact performers as single-column cards. Mobile (`<900px`) stacks all sections in the same order.
- **Performers (`/performers`)**: Default to list-first table with one row per role; row click opens in-place detail panel with explicit back control.
- **History (`/history`)**: Replace placeholder with cycle timeline table and explicit empty state using shared card styling.

### Component Behavior and States

- **Active Performers card**: Active state shows one tile per session (role, card, elapsed). Idle state shows phase, cycle count, board summary counts, last poll, and optional assignee filter.
- **Performers drilldown**: Preserve selected row during SSE refreshes; do not collapse detail panel on each update; label idle roles clearly.
- **History table**: Update live from SSE without full page reload; empty state should say no cycles have completed yet.

### Visual and Interaction Rules

- Use consistent card spacing and typography scale between routes.
- Remove dependency on expand/collapse controls for core readability.
- Keep status badges color-consistent (`healthy/degraded/error`, `active/idle`, `success/error` outcomes).
- Avoid introducing additional navigation patterns beyond existing navbar + in-page toggles.

### Accessibility and Usability

- All interactive rows and controls must be keyboard reachable.
- Drilldown row controls must expose clear focus state and semantic role.
- Color should not be the only state signal; keep text labels for all badges/states.
- Preserve readability at narrow widths without horizontal scrolling.

### Validation Plan

1. Run existing dashboard e2e route tests.
2. Add `/history` real data rendering coverage (no placeholder copy).
3. Add `/performers` list-to-detail and detail-to-list transition coverage.
4. Add desktop two-column layout and idle Active Performers summary assertions.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A reviewer non-JSON first response no longer causes immediate card `blocked` state when recovery succeeds on subsequent attempt.
- **SC-002**: Active performers idle tile shows board counts and last poll on first dashboard paint after SSE snapshot.
- **SC-003**: `/history` displays real cycle data within 1 second of route navigation.
- **SC-004**: `/performers` detail drilldown opens in under 300ms and continues live updates.
- **SC-005**: At 1080p width, workflow and compact performers cards render as two separate columns without expand/collapse interaction.

## Assumptions

- Existing `cycle_history`, `board_snapshot`, and `last_poll_at` state fields are sufficient for first functional history/idle summary pass.
- No new backend persistence store is required; all data remains in-memory/SSE snapshot driven.
- This feature intentionally completes deferred parts of 049 dashboard redesign rather than replacing the routing architecture.
