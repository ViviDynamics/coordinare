# Feature Specification: Live Web Dashboard

**Feature Branch**: `010-web-dashboard`
**Created**: 2026-03-02
**Status**: Draft

## User Scenarios & Testing

### User Story 1 — Current Status at a Glance (Priority: P1)

An operator opens a browser to the coordinare dashboard URL and immediately sees the daemon's current state: workflow phase, active card details (title, board column, link to the PR if one exists), agent session ID if a session is active, and the time elapsed since the last successful cycle. This gives a complete operational picture without needing to tail logs or query the health endpoint manually.

**Why this priority**: This is the core value of the feature. Every other user story builds on this view. It is also the simplest to deliver — no streaming, no history — making it a self-contained MVP.

**Independent Test**: Navigate to the dashboard URL with the daemon running in each phase (idle, dispatching, monitoring_agent, monitoring_pr, merging, blocked). Confirm each phase is correctly labelled and that card details are present when a card is active and absent when idle.

**Acceptance Scenarios**:

1. **Given** the daemon is idle, **When** the dashboard page loads, **Then** the page shows phase "idle", no active card, and the time since the last cycle.
2. **Given** the daemon is in `monitoring_agent` phase with an active card, **When** the dashboard page loads, **Then** the card title, board column, and PR URL (as a clickable link) are all visible.
3. **Given** the daemon has an active agent session, **When** the dashboard page loads, **Then** the agent session ID is shown.
4. **Given** the daemon is in `blocked` phase with open questions, **When** the dashboard page loads, **Then** the open questions are listed.
5. **Given** the dashboard port is set in config, **When** the operator navigates to `http://localhost:<port>/`, **Then** the dashboard loads without authentication.

---

### User Story 2 — Subsystem Health and Key Metrics (Priority: P2)

The operator can see the health of every registered subsystem (github, agent_ssh, config, notifications) and a small set of operational metrics — total cycles completed, current error count, and most recent cycle duration — all on the same page as the current state.

**Why this priority**: Health and metrics are the second most important operational signal. With phase + health + metrics together, an operator can diagnose most issues without leaving the browser.

**Independent Test**: With one subsystem degraded and the daemon running, confirm the dashboard clearly distinguishes healthy from degraded/unavailable subsystems, and that cycle counts increment between manual page refreshes.

**Acceptance Scenarios**:

1. **Given** all subsystems are healthy, **When** the dashboard loads, **Then** each subsystem is shown with a "healthy" indicator.
2. **Given** a required subsystem is degraded, **When** the dashboard loads, **Then** that subsystem is visually distinct from healthy ones and its required/optional designation is shown.
3. **Given** the daemon has completed cycles, **When** the dashboard loads, **Then** cycles-completed count, most recent cycle duration, and error count since last restart are displayed.

---

### User Story 3 — Recent Cycle History (Priority: P3)

The operator can see a short list of the most recent cycles — timestamp, phase at completion, and whether the cycle succeeded or ended in an error — giving a quick sense of recent activity without needing to read structured logs.

**Why this priority**: History is useful for diagnosing intermittent errors but is not essential for basic operational monitoring. It requires in-memory state that accumulates across cycles, making it a natural follow-on to US1 and US2.

**Independent Test**: Run the daemon through five cycles (mix of success and error), then load the dashboard and confirm the last five cycles appear in reverse-chronological order with correct outcomes.

**Acceptance Scenarios**:

1. **Given** the daemon has completed at least one cycle, **When** the history section loads, **Then** it shows up to 20 most recent entries in reverse-chronological order.
2. **Given** a cycle ended in an error, **When** the history section loads, **Then** that entry is visually distinguished from successful cycles.
3. **Given** the daemon just started with no completed cycles, **When** the history section loads, **Then** an empty-state message is shown rather than a blank section.

---

### User Story 4 — Live Auto-Refresh (Priority: P4)

The dashboard page automatically reflects state changes within a few seconds of them occurring — phase transitions, card changes, health probe updates — without the operator needing to manually reload.

**Why this priority**: Auto-refresh makes the dashboard genuinely live rather than a snapshot viewer. However, a dashboard that requires manual refresh still delivers significant value, making this an enhancement rather than a prerequisite.

**Independent Test**: With the dashboard open, trigger a phase transition on the daemon. Confirm the displayed phase updates on-screen within 5 seconds with no manual interaction.

**Acceptance Scenarios**:

1. **Given** the dashboard is open, **When** the daemon transitions between phases, **Then** the displayed phase updates within 5 seconds.
2. **Given** the dashboard is open and a subsystem becomes degraded, **When** the next health update arrives, **Then** the health indicator changes without a full page reload.
3. **Given** the browser has had the dashboard open for 30 minutes, **When** the page is still open, **Then** it continues receiving updates without requiring a reload.
4. **Given** the daemon process restarts, **When** the connection is restored, **Then** the dashboard reconnects and resumes showing live data automatically.

---

### Edge Cases

- What is shown when the daemon has just started and no first cycle has completed yet?
- If the configured dashboard port is already in use at startup, the daemon MUST log a structured error and exit with a non-zero code rather than starting without the dashboard.
- If the update stream is lost, a "disconnected" banner is shown over the last-known state; when the stream reconnects the banner clears and data refreshes automatically.
- Cycle history is in-memory and resets on daemon restart; the prominently displayed daemon start time (FR-008) lets the operator infer that history prior to that time is unavailable.
- An unrecognised phase value is displayed using the same underscore-to-space title-case transformation, so future phases render gracefully without a code change.

---

## Requirements

### Functional Requirements

- **FR-001**: The dashboard MUST be served at the root path (`/`) of a configurable TCP port, separate from the existing health check port. If the configured port is already in use at daemon startup, the daemon MUST log a structured error and exit with a non-zero code.
- **FR-002**: The dashboard MUST display the current workflow phase as a human-readable label formed by replacing underscores with spaces and applying title case (e.g., `monitoring_agent` → "Monitoring Agent", `relay_feedback` → "Relay Feedback"). This transformation MUST apply to all defined phase values and to any unrecognised phase values that may appear in future.
- **FR-003**: When a card is active, the dashboard MUST display the card title, board column, and PR URL; the PR URL MUST render as a clickable external link.
- **FR-004**: When no card is active, the dashboard MUST show an explicit idle indicator rather than leaving card fields blank.
- **FR-005**: The dashboard MUST display the agent session ID when a session is active.
- **FR-006**: The dashboard MUST list all open questions when the daemon is in the `blocked` phase.
- **FR-007**: The dashboard MUST display the health status (healthy / degraded / unavailable) and required/optional designation for every registered subsystem.
- **FR-008**: The dashboard MUST display total cycles completed since daemon start, most recent cycle duration, the current consecutive error count (number of failed cycles since the last successful cycle), and the daemon start time. The start time MUST be shown prominently so operators can immediately assess whether the cycle history is complete or was truncated by a restart.
- **FR-009**: The dashboard MUST maintain a rolling in-memory log of the 20 most recent cycle completions, each recording timestamp, resulting phase, duration, and success/error outcome.
- **FR-010**: The dashboard MUST display the cycle history list with an empty-state message when no cycles have completed.
- **FR-011**: The dashboard MUST push state updates to open browser sessions within 5 seconds of a change, without requiring a full page reload.
- **FR-012**: When the update stream is lost, the dashboard MUST display a visible "disconnected" banner over the last-known state and attempt automatic reconnection. When the stream is restored, the banner MUST be dismissed and the displayed data MUST refresh immediately.
- **FR-013**: The dashboard MUST require no authentication.
- **FR-014**: The dashboard port MUST be configurable via config file and a `COORDINARE_DASHBOARD_PORT` environment variable, defaulting to a value that does not conflict with the default health check port. The bind address MUST default to `127.0.0.1` (localhost only) and MUST be configurable via a `COORDINARE_DASHBOARD_HOST` environment variable and config file field to support reverse proxy deployments (e.g., `0.0.0.0` to accept connections from all network interfaces).
- **FR-015**: The dashboard serving overhead MUST NOT increase average cycle execution time by more than 5 ms.
- **FR-016**: The dashboard MUST be usable in a standard browser with JavaScript enabled, with no browser extensions or build tools required.
- **FR-017**: Every HTTP request to the dashboard MUST be logged as a structured log entry including method, path, response status code, and response time.

### Key Entities

- **DashboardState**: A point-in-time snapshot of all data the dashboard renders — current phase, active card, agent session, subsystem health probes, metrics counters (including daemon start time), and cycle history. Derived entirely from existing in-process daemon state; not independently persisted.
- **CycleHistoryEntry**: A single completed-cycle record in the rolling in-memory log — timestamp, phase at completion, duration in seconds, and success/error outcome. The log holds a maximum of 20 entries; the oldest is dropped when the limit is exceeded.

---

## Success Criteria

### Measurable Outcomes

- **SC-001**: An operator can determine the current workflow phase and active card within 3 seconds of opening the dashboard URL.
- **SC-002**: A phase transition is reflected on an already-open dashboard within 5 seconds of occurring.
- **SC-003**: The dashboard initial page load completes in under 500 ms under normal operating conditions.
- **SC-004**: Dashboard serving adds no more than 5 ms to average cycle execution time, measured over 50 cycles with the dashboard open and actively receiving updates.
- **SC-005**: The dashboard reconnects and resumes displaying live data within 10 seconds of a daemon restart.
- **SC-006**: All cycle history entries are accurate — timestamp, phase, duration, and outcome match the daemon's internal records for those cycles.

---

## Clarifications

### Session 2026-03-02

- Q: Should the dashboard bind to localhost only or all interfaces by default? → A: Localhost (`127.0.0.1`) by default; bind address configurable via config/env to support reverse proxy access (e.g., `0.0.0.0`).
- Q: What does the browser show when the live update stream is lost? → A: A visible "disconnected" banner overlaid on the last-known state; banner clears automatically when the stream reconnects.
- Q: What should happen if the dashboard port is already in use at daemon startup? → A: Fail fast — log a structured error and exit with a non-zero code.
- Q: How should phase values be displayed as human-readable labels? → A: Underscores replaced with spaces, title-cased (e.g., `monitoring_agent` → "Monitoring Agent"); applies to known and unknown phases alike.
- Q: How should the operator know whether cycle history is complete or was truncated by a restart? → A: Display daemon start time prominently alongside the metrics; operator can infer history completeness from it.
- Q: Should dashboard HTTP requests be logged? → A: Yes — every request logged as a structured entry with method, path, status code, and response time.

---

## Assumptions

- The dashboard is a single-operator view; no multi-user session or concurrent-viewer concerns apply.
- All data displayed is already held in existing in-process state (daemon state, HealthRegistry, CoordinareMetrics, StateStore.last_snapshot). No new persistence layer is required.
- Cycle history (FR-009) is in-memory only and resets on daemon restart; this is acceptable given that structured logs provide long-term history.
- The default dashboard port is 8090; the default health check port is 8080. Both are independently overridable.
- The default dashboard bind address is `127.0.0.1`. Operators running coordinare behind a reverse proxy (e.g., nginx) may set the bind address to `0.0.0.0` to make it reachable from the network; the spec assumes no additional auth layer is added in that case, so network-level access control is the operator's responsibility.
- Updates are delivered using the browser's native streaming event mechanism — no external libraries, no npm dependencies, no build step required in the browser.
- The dashboard is strictly read-only; no control operations are exposed.
