# Feature Specification: Docker and CLI Runtime Visibility

**Feature Branch**: `002-docker-cli-output`  
**Created**: 2026-02-16  
**Status**: Draft  
**Input**: User description: "I want to be able to run this application via Docker and via the command line so that I can see some console output to be able to understand what it is doing."

## Clarifications

### Session 2026-02-16

- Q: How should sensitive data be handled in console/container output? → A: Redact only known secret fields; allow other raw values in debug/error output.
- Q: What output format behavior is required? → A: Human-readable by default, with optional structured mode.
- Q: How much runtime detail should output include during normal operation? → A: Major events plus periodic heartbeat by default, with high-granularity details available at higher log levels.
- Q: How should runtime errors affect process exit behavior? → A: Exit non-zero on any runtime error, including recoverable ones.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run from Command Line with Clear Output (Priority: P1)

A developer runs the application from the command line and sees clear, structured console output that explains startup status, ongoing activity, and errors.

**Why this priority**: Command-line execution is the fastest local feedback loop and the primary way to verify runtime behavior during development.

**Independent Test**: Start the app from the command line in a configured environment and confirm the console shows startup validation, runtime activity updates, and shutdown messaging.

**Acceptance Scenarios**:

1. **Given** valid runtime configuration, **When** the user starts the app from the command line, **Then** the console displays startup progress and confirms the app is running.
2. **Given** the app is actively running, **When** processing cycles occur, **Then** the console displays periodic activity updates that describe what the app is doing.
3. **Given** an invalid startup condition, **When** the user starts the app from the command line, **Then** the console displays a clear, actionable error and exits cleanly.
4. **Given** the app is running, **When** the user sends a termination signal (e.g. Ctrl+C or SIGTERM), **Then** the console emits a shutdown message indicating graceful termination before the process exits.

---

### User Story 2 - Run via Docker Compose with Equivalent Visibility (Priority: P2)

A developer or operator runs the application via Docker Compose and can inspect container logs to understand the same runtime behavior available in command-line mode.

**Why this priority**: Containerized execution is required for consistent deployment and operational testing across environments.

**Independent Test**: Start the app with Docker Compose and confirm container logs provide startup, runtime progress, and failure information equivalent to command-line execution.

**Acceptance Scenarios**:

1. **Given** valid runtime configuration in Docker Compose, **When** the container starts, **Then** runtime logs show startup checks and successful run-state confirmation.
2. **Given** runtime processing in Docker Compose, **When** the app performs polling/processing cycles, **Then** container logs show meaningful activity updates at expected intervals.
3. **Given** a runtime error in Docker Compose, **When** failure occurs, **Then** logs include the failure reason and context needed for troubleshooting.

---

### User Story 3 - Understand Runtime State from Console Output (Priority: P3)

A developer or operator can look at console/log output and quickly determine whether the app is idle, actively processing, blocked by an issue, or recovering from an error.

**Why this priority**: Operational clarity reduces troubleshooting time and improves confidence in unattended execution.

**Independent Test**: Observe output during normal operation and induced failure conditions, then verify a reviewer can correctly identify current state and recent actions.

**Acceptance Scenarios**:

1. **Given** active runtime output, **When** a reviewer reads recent console lines, **Then** they can identify current application state and latest significant action.
2. **Given** consecutive errors, **When** the app encounters a runtime failure, **Then** output communicates the failing step and the process exits non-zero.
3. **Given** the app is waiting on an external dependency or blocked on a prerequisite, **When** output is observed, **Then** a reviewer can identify that the app is in a blocked state and why.
4. **Given** the app has encountered a transient failure and is attempting to recover, **When** output is observed, **Then** a reviewer can see recovery state messaging before the process ultimately exits non-zero.

### Edge Cases

- What happens when the app starts without required configuration values? → FR-003 and FR-005 require that startup validation output includes a clear, actionable error identifying the missing or invalid field before exit.
- What happens when output volume is high over long-running sessions? → Out of scope for this feature phase; see Assumptions.
- How does the app classify and report startup failures versus runtime failures before exiting? → FR-003 covers startup failure output; FR-005 and FR-013 cover runtime failure output. These are treated as distinct failure classes in the output taxonomy.
- How does output behave when the app is intentionally stopped during active processing? → FR-007 requires the shutdown message to indicate the cycle was interrupted rather than completed.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Users MUST be able to start the application from the command line using a documented invocation path.
- **FR-002**: Users MUST be able to run the application via Docker Compose using a documented container invocation path.
- **FR-003**: The system MUST emit console/log output at startup that indicates validation progress and final startup status.
- **FR-004**: The system MUST emit runtime activity output that communicates what work is being performed during processing cycles.
- **FR-005**: The system MUST emit clear error output that includes enough context for a user to diagnose the failure cause.
- **FR-006**: The system MUST indicate state transitions in output so users can distinguish the following runtime states:
  - **idle**: The daemon is running but not currently processing any work (e.g. between polling cycles).
  - **active**: The daemon is actively processing a work item or polling cycle.
  - **blocked**: The daemon is waiting on an external dependency or unresolved prerequisite and cannot proceed without intervention.
  - **recovery**: The daemon has encountered a transient failure and is making a final recovery attempt before exiting. Recovery is a transient output state — the process will exit non-zero after the recovery attempt concludes (see FR-013).
- **FR-007**: The system MUST emit shutdown output that confirms whether termination was graceful or due to failure. If shutdown occurs while a processing cycle is active, the shutdown message MUST indicate that the cycle was interrupted rather than completed.
- **FR-008**: Output semantics MUST remain consistent between command-line and Docker Compose execution modes.
- **FR-009**: Documentation MUST describe how to run the app in both modes and how to interpret key output messages.
- **FR-010**: The system MUST redact the sensitive fields enumerated in FR-014 from all runtime output, while allowing non-sensitive diagnostic values to appear in debug/error output.
- **FR-011**: The system MUST provide human-readable output by default and support an optional structured output mode (enabled via the `--structured-output` flag) for automated parsing.
- **FR-012**: The system MUST log major events and periodic heartbeat summaries at default runtime verbosity (`INFO`), and allow higher-granularity diagnostic output through log-level configuration. Supported log levels are `DEBUG`, `INFO`, `WARNING`, and `ERROR`, selectable via a `--log-level` flag.
- **FR-013**: The system MUST terminate with a non-zero exit status on any runtime error, including those preceded by a recovery attempt (see FR-006). The recovery state is observable in output before exit but does not prevent a non-zero exit. Output MUST include enough context to identify the failing step.
- **FR-014**: The system MUST treat the following keys as sensitive and redact them in output when present: `token`, `password`, `secret`, `api_key`, `webhook_url`, `authorization`.
- **FR-015**: The system MUST emit a heartbeat event at least once every 30 seconds while running at default verbosity.

### Key Entities *(include if feature involves data)*

- **Runtime Session**: Represents a single application execution window from startup to shutdown, including mode, status, and lifecycle events.
- **Runtime Event**: Represents a user-visible output event such as startup check, activity cycle update, warning, error, retry, or shutdown notice.

### Assumptions

- Users running this feature have valid access and credentials required by the application.
- Console output is the primary source of runtime observability for this phase.
- No dedicated visual dashboard is required for this feature scope.
- Sensitive-field matching is case-insensitive for key names.
- Target platform is Linux. Shell-mode execution requires Bash ≥ 3.2; Docker-mode execution requires Docker Engine ≥ 24.0 and Docker Compose plugin ≥ 2.20.
- Log rotation, output buffering limits, and log truncation for long-running sessions are out of scope for this feature phase. Console output is treated as a stream with no volume management requirements.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In 95% of startup attempts, users can determine run success or failure from output within 10 seconds of launch.
- **SC-002**: In both command-line and Docker Compose modes, users can identify current runtime state from recent output within 30 seconds.
- **SC-003**: At least 90% of runtime failures include actionable error context that allows users to identify the failing step without code inspection.
- **SC-004**: At least 90% of users following the run documentation can start the application in both modes on their first attempt.
- **SC-005**: In 95% of runs longer than 2 minutes, heartbeat intervals do not exceed 30 seconds.

### Success Measurement Methods

- **SC-001 method**: Measure elapsed time from process start to appearance of a startup success or failure message across at least 20 observed startup attempts in CI or local environments; ≥19/20 must complete within 10 seconds.
- **SC-002 method**: During integration testing, induce each of the four runtime states and verify that the state is identifiable from a 30-second log window; repeat for both shell and Docker Compose modes.
- **SC-003 method**: Review integration test failure logs and confirm each captured failure event includes at minimum: the failing component, the error message, and the last successful state. Sample at least 10 distinct failure scenarios.
- **SC-004 method**: Validate with the run checklist in `specs/002-docker-cli-output/quickstart.md` across at least 10 first-time executions (mix of shell and Docker Compose flows); success is met when at least 9/10 complete startup on first attempt.
- **SC-005 method**: In the `test_runtime_timing.py` integration test, instrument the daemon for at least 2 minutes and assert that no gap between consecutive heartbeat events exceeds 30 seconds.
