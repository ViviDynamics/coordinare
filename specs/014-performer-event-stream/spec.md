# Feature Specification: Performer Event Stream

**Feature Branch**: `014-performer-event-stream`
**Created**: 2026-03-13
**Status**: Draft
**Input**: Real-time activity feed surfacing AI agent actions during card execution

## User Scenarios & Testing *(mandatory)*

### User Story 1 — See Live Agent Activity in the Dashboard (Priority: P1)

An operator wants to know what the AI coding agent is actually doing while a card is in progress — not just that it is "working", but which files it is reading, which edits it is making, and whether it is running tests. They open the dashboard during an active session and see a scrolling timeline of agent actions updating in real time.

**Why this priority**: This is the core user-facing value. Without it the feature does not exist. All other stories build on this visibility.

**Independent Test**: Start a card execution session with any configured backend. Open the dashboard while the agent is working. Verify the Activity Feed section appears and updates with new entries (file reads, edits, progress messages) without requiring a page refresh.

**Acceptance Scenarios**:

1. **Given** a card is in the `monitoring_agent` phase, **When** the operator opens the dashboard, **Then** an Activity Feed section is visible showing the most recent agent actions in reverse-chronological order.
2. **Given** the Activity Feed is visible, **When** the agent performs an action (reads a file, makes an edit, runs a command), **Then** a new entry appears in the feed within the next poll cycle showing the action type and a human-readable summary.
3. **Given** the agent has produced more than 50 events, **When** the dashboard is viewed, **Then** only the 50 most recent events are displayed (oldest are scrolled off).
4. **Given** the card transitions out of `monitoring_agent` phase, **When** the dashboard is viewed, **Then** the Activity Feed is no longer shown.

---

### User Story 2 — Events Flow Through from opencode Backend (Priority: P1)

An operator running the opencode backend sees the same live activity feed as described in User Story 1. The opencode backend already streams structured events; those events are captured and normalised into the common format.

**Why this priority**: opencode is the primary backend in current use. P1 alongside Story 1 because Story 1 delivers no value without at least one backend producing events.

**Independent Test**: Run a card with `AGENT_BACKEND=opencode`. Confirm the activity feed in the dashboard populates with entries during the session, including tool use entries (file reads, edits) and progress messages.

**Acceptance Scenarios**:

1. **Given** a card is running via the opencode backend, **When** the agent reads a file, **Then** a `tool_use` entry appears in the activity feed with the file name.
2. **Given** a card is running via the opencode backend, **When** the agent produces a text progress update, **Then** a `progress` entry appears with the first 200 characters of the message.
3. **Given** the session ends (success or error), **When** the final event is processed, **Then** the session's complete event history remains visible in the feed until the phase changes.

---

### User Story 3 — Events Flow Through from Claude Code Backend (Priority: P2)

An operator who prefers Claude Code as the agent backend (`AGENT_BACKEND=claude_code`) sees the same live activity feed. A new Claude Code backend is implemented that launches the `claude` CLI with streaming output and parses its event format into the common model.

**Why this priority**: Requires a fully new backend implementation. Depends on the common event infrastructure from Stories 1 and 2 being in place.

**Independent Test**: Configure `AGENT_BACKEND=claude_code` and run a card. Verify the activity feed populates with typed events (progress, tool_use, thinking, cost) and that the session terminates cleanly when the `claude` CLI exits.

**Acceptance Scenarios**:

1. **Given** `AGENT_BACKEND=claude_code` is configured with a valid Claude CLI installation, **When** a card starts, **Then** the claude CLI is launched and its stream is parsed into activity feed entries.
2. **Given** the Claude backend is running, **When** the agent uses a tool (file read/write/edit), **Then** a `tool_use` entry appears with the tool name and a summary of its input.
3. **Given** the Claude backend is running, **When** the agent produces extended thinking output, **Then** a `thinking` entry appears in the feed.
4. **Given** the Claude session ends, **When** the result event is received, **Then** a `cost` entry appears showing token usage, and the backend status transitions to `done` or `error`.
5. **Given** the coordinare sends review feedback during a Claude session, **When** the backend receives it, **Then** the feedback is delivered to the agent — either as a follow-up input to the running session or as a new invocation with the feedback appended, whichever the CLI supports.

---

### User Story 4 — Secrets Are Never Surfaced in the Activity Feed (Priority: P1)

An operator can trust that sensitive values — GitHub tokens, API keys, passwords — never appear in the activity feed displayed in the dashboard, even if the agent passes them as tool arguments internally.

**Why this priority**: A security requirement. If secrets leak into the feed they could be exposed in browser history, dashboard screenshots, or logs. Must be in place before any backend produces real events.

**Independent Test**: Configure a backend and run a card that involves git operations (which use the GitHub token). Inspect all activity feed entries in the dashboard and in structured logs. Confirm no credential strings appear in any `detail` field.

**Acceptance Scenarios**:

1. **Given** an agent invokes a tool with a credential value in its arguments, **When** the event is captured and normalised, **Then** any string matching a known secret pattern (GitHub token, API key format) is replaced with `[REDACTED]` in the `detail` field.
2. **Given** the redaction pass runs, **When** a non-secret value that looks similar to a secret is present, **Then** only values matching defined secret patterns are redacted; other values pass through unchanged.

---

### Edge Cases

- What happens when the agent backend produces no events for an extended period? The activity feed remains showing the last known events with no new entries; no error is shown.
- What happens if an event's text exceeds the display length? Text is truncated to 200 characters with an ellipsis; the full text is available in the `detail` field.
- What happens if the event buffer fills (200 events) before drain? The oldest events are silently dropped; the 200 most recent are retained.
- What happens when the Codex backend is selected? The Codex backend is fully implemented via WebSocket JSON-RPC and handles card execution normally.
- What happens if the claude CLI is not installed when `AGENT_BACKEND=claude_code` is configured? The card fails at startup with an actionable error identifying the missing CLI.
- What happens if two concurrent drain calls race? The drain operation is atomic — each call returns a non-overlapping set of events with no duplicates or losses.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST provide a common event model with fields: timestamp, type (one of: progress, tool_use, thinking, cost, error, output), human-readable text (≤200 chars), and optional detail.
- **FR-002**: System MUST add a `drain_events()` operation to the backend adapter interface that returns all events accumulated since the last call and clears the buffer atomically.
- **FR-003**: The opencode backend MUST capture events from its existing structured output stream and normalise them into the common event model, including tool use, progress, and error events.
- **FR-004**: System MUST include a new Claude Code backend that launches the claude CLI with streaming output, parses its stream into the common event model, and detects session completion from the result event.
- **FR-005**: System MUST include a Codex backend that communicates with the Codex CLI via WebSocket JSON-RPC, parses its event stream into the common event model, and delivers typed events to the activity feed.
- **FR-006**: The performer MUST include all accumulated events in every status response, returning an empty list when no new events have occurred since the last poll.
- **FR-007**: The coordinare MUST accumulate events from each status poll into a rolling buffer capped at 100 entries, merging new events onto the end and dropping the oldest when the cap is exceeded.
- **FR-008**: The dashboard MUST display an Activity Feed section when a card is in the active execution phase, showing the 50 most recent events as a scrolling timeline with timestamp, event type badge, and text.
- **FR-009**: The Activity Feed MUST update automatically with each dashboard refresh cycle without requiring a manual page reload.
- **FR-010**: System MUST apply a redaction pass to the `detail` field of every event before it is stored or transmitted, replacing any value matching a known secret pattern with `[REDACTED]`.
- **FR-011**: The Claude Code backend's `relay_feedback()` operation MUST deliver coordinare feedback to the running agent session; if the CLI does not support mid-session input, a new invocation with the feedback appended to the original prompt MUST be used instead.
- **FR-012**: The design MUST be additive — backends that do not implement `drain_events()` MUST continue to function correctly, returning an empty events list in the status response.

### Key Entities

- **BackendEvent**: A single normalised activity record. Contains: timestamp (when the event occurred), type (categorical label), text (brief human-readable summary, ≤200 chars), detail (optional extended information).
- **EventType**: The category of a BackendEvent — one of: progress (general agent activity), tool_use (a tool was invoked), thinking (internal reasoning), cost (token/cost accounting), error (non-fatal agent error), output (unclassified raw output).
- **EventBuffer**: A per-backend rolling store of BackendEvents, capped at 200 entries. Drained on each status poll.
- **ActivityFeed**: The coordinare-side accumulation of events across all status polls for the current card session, capped at 100 entries.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can see the agent's current action within one poll cycle of it occurring — no manual intervention required.
- **SC-002**: The activity feed displays at least tool_use and progress event types for both the opencode and claude_code backends during a real card session.
- **SC-003**: Zero known secret patterns (GitHub tokens, API keys) appear in any activity feed entry across 100 consecutive test events that include credential-bearing tool calls.
- **SC-004**: The Codex backend successfully executes a card session end-to-end, delivering typed events (progress, tool_use, thinking, cost) to the activity feed — verified by inspecting the event stream during a session.
- **SC-005**: The dashboard activity feed renders correctly and updates without a page reload during an active session lasting at least 5 minutes.
- **SC-006**: Existing card sessions using any supported backend complete successfully after this feature is introduced — no regressions in session success rate.

## Assumptions

- The performer's existing polling protocol (`check_status` response) is reused for event delivery; no new wire message types are introduced.
- The claude CLI (`claude`) is assumed to be installed and on `PATH` when `AGENT_BACKEND=claude_code` is configured; no automatic installation is performed.
- Secret redaction targets a defined list of known patterns (e.g., `ghp_`, `sk-`, bearer token formats); a generic "looks secret" heuristic is out of scope.
- The Codex backend is fully implemented via WebSocket JSON-RPC; no additional research is required.
- Dashboard rendering uses the existing SSE stream and HTML template; no separate frontend build step is introduced.
- Events are ephemeral — they are held in memory only and reset when the coordinare or performer process restarts.
