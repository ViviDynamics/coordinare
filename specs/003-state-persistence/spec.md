# Feature Specification: Workflow State Persistence

**Feature Branch**: `003-state-persistence`
**Created**: 2026-02-22
**Status**: Draft
**Input**: User description: "The coordinare daemon needs to persist card workflow state across process restarts. Currently all state is held in-memory inside the LangGraph CoordinareState dict, so a crash or deliberate restart while a card is In Progress orphans that card on the board indefinitely. We need durable state storage so that on startup the coordinare can recover the current board position, resume monitoring the active card, and continue the workflow without human intervention. The storage must be embeddable (no external database dependency), survive container restarts, and be queryable for the health endpoint to report current phase and active card."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Crash Recovery Without Human Intervention (Priority: P1)

A coordinare daemon is actively monitoring an agent working on a card. The host machine is rebooted, or the process is killed unexpectedly. When the coordinare restarts — whether in seconds or hours later — it reads its last saved state and resumes exactly where it left off: monitoring the same card, in the same workflow phase, without re-dispatching work to the agent or losing track of the PR under review.

**Why this priority**: Without this, every unclean shutdown leaves a card permanently stuck In Progress on the board. An operator must manually inspect the board, determine what was happening, and either reset the card or re-dispatch manually. This defeats the purpose of an autonomous coordinare. Recovery on restart is the minimum viable guarantee of reliability.

**Independent Test**: Start the coordinare with a card In Progress. Send SIGKILL. Restart. Verify the coordinare resumes monitoring the same card in the same phase — no duplicate dispatch, no board card reset — within one poll interval.

**Acceptance Scenarios**:

1. **Given** the coordinare is monitoring an agent actively working a card, **When** the daemon process is killed uncleanly (SIGKILL), **Then** on restart the coordinare reads saved state and resumes monitoring the same card without re-dispatching it to the agent.
2. **Given** the coordinare is in the `monitoring_pr` phase watching an open PR, **When** the host is rebooted, **Then** on restart the coordinare resumes watching the same PR without opening a new one or losing the PR URL.
3. **Given** the coordinare is in the `blocked` phase with open questions on a card, **When** the process restarts, **Then** the coordinare resumes in the blocked phase with the same open questions intact, ready to detect when the team has answered them.
4. **Given** the coordinare is idle with no active card, **When** the process restarts, **Then** the coordinare starts fresh, polling the board normally.
5. **Given** a coordinare restart, **When** the phase is `monitoring_agent` and the agent has already finished (PR opened during the downtime), **Then** the coordinare reconciles with the board on startup and advances to `monitoring_pr` rather than repeating the dispatch.

---

### User Story 2 - Health Endpoint Reflects Persisted State (Priority: P2)

An operator queries the coordinare's health endpoint at any time — including immediately after a restart — and receives an accurate, current report of what the coordinare is doing: which card is active, what phase it is in, and when the last state transition occurred. This information is drawn from persisted storage, so it is available even before the first poll cycle completes.

**Why this priority**: The health endpoint is the primary operational visibility tool. If it only reflects in-memory state it is blind during startup and after restarts. Persisted state makes the health report authoritative across process boundaries.

**Independent Test**: Restart the coordinare (without completing a cycle), immediately query `/health`, and verify it returns the correct active card ID and phase from the previous session.

**Acceptance Scenarios**:

1. **Given** the coordinare has persisted state from a prior session, **When** the health endpoint is queried immediately after a restart (before the first poll cycle completes), **Then** the response includes the active card identity and phase from the last saved state.
2. **Given** the coordinare is actively running, **When** a phase transition occurs (e.g., dispatching → monitoring_agent), **Then** the health endpoint reflects the new phase within one poll interval.
3. **Given** the coordinare is idle with no active card, **When** the health endpoint is queried, **Then** the response indicates idle state with no active card.

---

### User Story 3 - Graceful Handling of Corrupted or Stale State (Priority: P3)

A stored state file is corrupted (e.g., truncated during an unclean write), references a card that no longer exists on the board, or was written by a previous version of the coordinare with a different schema. The coordinare detects this condition on startup, logs a clear warning identifying the problem, discards the invalid state, and begins a fresh idle cycle rather than crashing.

**Why this priority**: Robustness of the recovery path is essential. If corrupted state causes a crash loop, the operator is worse off than if persistence didn't exist at all. The board is always the source of truth — when saved state can't be trusted, the coordinare must defer to the board.

**Independent Test**: Corrupt the state file on disk, restart the coordinare, and verify it logs a warning (not a crash), starts fresh, and resumes normal polling.

**Acceptance Scenarios**:

1. **Given** the state file contains malformed data (e.g., partial write, invalid encoding), **When** the coordinare starts, **Then** it logs a warning identifying the file as unreadable, discards it, and begins a fresh idle cycle.
2. **Given** the state file references a card ID that no longer exists on the board, **When** the coordinare reconciles on startup, **Then** it logs a warning, discards the stale state, and begins a fresh idle cycle.
3. **Given** the state file was written by an incompatible prior version, **When** the coordinare starts, **Then** it detects the schema mismatch, logs a warning, discards the file, and begins a fresh idle cycle.
4. **Given** no state file exists (first run or deleted), **When** the coordinare starts, **Then** it begins a fresh idle cycle with no warnings.

---

### Edge Cases

- What happens if the state file is being written when power is lost (torn write)? → The system must use an atomic write strategy (write to a temp file, rename) so a partial write never corrupts the last valid state.
- What happens if two coordinare instances accidentally share the same state file? → Out of scope for this feature (single-instance assumption). A future spec may address distributed coordination.
- What happens if the board column was manually changed by a human during the coordinare's downtime (e.g., card moved from In Progress to Blocked by a human)? → On startup the coordinare queries the board as the authoritative source. If the board state contradicts saved state, the board wins and the coordinare logs the discrepancy before resuming from the board's current position.
- What happens if the state file path is not writable (permission error)? → The coordinare logs an error at startup and exits with a non-zero code; it does not silently run in stateless mode.
- What happens if disk is full during a state write? → The atomic write fails, the previous state file remains intact, and the coordinare logs an error. It continues running in memory but logs a warning that persistence is degraded.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST write the current workflow state to durable storage after every phase transition, before the next operation begins.
- **FR-002**: The system MUST read persisted state from storage on startup and use it to restore the active card and current phase before the first poll cycle.
- **FR-003**: The system MUST reconcile persisted state against the live board on startup; the board is authoritative when a conflict is detected.
- **FR-004**: State writes MUST be atomic — a partial or interrupted write MUST NOT corrupt the previously saved state.
- **FR-005**: The storage backend MUST be embeddable: no external database server, broker, or network service is required to run the coordinare.
- **FR-006**: Persisted state MUST survive a container restart when the state file path is on a mounted volume.
- **FR-007**: The health endpoint MUST report active card identity and current phase sourced from persisted state, available immediately on startup before the first poll cycle completes.
- **FR-008**: The system MUST NOT re-dispatch a card to an agent if that card is already In Progress or In Review according to reconciled state.
- **FR-009**: When persisted state cannot be loaded (corrupted, missing, schema mismatch, unreadable card), the system MUST log a structured warning and begin a fresh idle cycle rather than crashing.
- **FR-010**: The state file path MUST be configurable via the configuration file and environment variable override. The default path MUST be `./coordinare.state.json` relative to the coordinare's working directory.
- **FR-011**: If the configured state file path is not writable, the system MUST log a structured error and exit with a non-zero code at startup.
- **FR-012**: The system MUST expose the following Prometheus metrics for state persistence observability: `coordinare_state_write_duration_seconds` (histogram), `coordinare_state_write_failures_total` (counter), and `coordinare_state_last_written_timestamp` (gauge).

### Key Entities

- **WorkflowSnapshot**: The durable record of the coordinare's active workflow position at a given moment. Serialised as a JSON file on disk; written atomically via temp-file-rename.
  - Active card identity (board ID, title, current column)
  - Current workflow phase (idle, dispatching, monitoring_agent, monitoring_pr, blocked, relay_feedback)
  - Open questions (non-empty only in blocked phase)
  - PR reference (URL, node ID — non-empty only in monitoring_pr and later phases)
  - Agent session ID (non-empty only in monitoring_agent and relay_feedback phases)
  - Snapshot timestamp (when this state was last written)
  - Schema version (for detecting incompatible prior versions)

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After any unclean restart (SIGKILL, OOM, power loss), the coordinare resumes the active workflow within one poll interval with no duplicate dispatches and no manual operator intervention required.
- **SC-002**: Workflow state is written to durable storage within one second of every phase transition.
- **SC-003**: The health endpoint reflects the correct phase and active card within five seconds of any phase change, including immediately after a restart.
- **SC-004**: A corrupted, missing, or schema-incompatible state file never causes the coordinare to crash or enter a crash loop; it always results in a logged warning and a clean fresh start.
- **SC-005**: No additional infrastructure (database servers, message queues, network services) is required to run the coordinare with state persistence enabled.
- **SC-006**: State file writes survive interrupted writes — the previously valid state is always recoverable even if a write is interrupted mid-operation.

## Clarifications

### Session 2026-02-22

- Q: What format should the WorkflowSnapshot use for on-disk storage? → A: JSON file (human-readable, atomic via temp-file-rename, zero additional dependencies).
- Q: What should the default state file path be? → A: `./coordinare.state.json` — relative to the coordinare's own WORKDIR, which is mounted independently from any agent subprocess working directories.
- Q: Should state persistence health be observable via Prometheus metrics? → A: Yes — expose `state_write_duration_seconds`, `state_write_failures_total`, and `state_last_written_timestamp` for alerting and dashboards.

## Assumptions

- A single coordinare instance owns the state file at any given time. Multi-instance coordination is explicitly out of scope.
- The project board (GitHub Projects) is the authoritative source of truth for card column position. Persisted state is a recovery hint, not a replacement for the board.
- The coordinare runs from its own dedicated WORKDIR, mounted independently from any agent subprocess working directories. The state file lives in the coordinare's WORKDIR and is never shared with agents.
- Container volumes (bind mounts or named volumes) provide sufficient durability for the operational environments targeted by this feature. Cross-host replication and HA are out of scope.
- State size is small (single active card at a time) — storage performance and size constraints are not a concern.
- The schema version embedded in the state file is sufficient for forward-compatibility detection; formal migration tooling is out of scope for this feature.
