# Feature Specification: Agent Communication Protocol

**Feature Branch**: `004-agent-protocol`
**Created**: 2026-02-22
**Revised**: 2026-02-22
**Status**: Draft
**Input**: User description: "Define a formal protocol for the interface between coordinare and AI agents, with a pluggable transport layer. The wire protocol (JSON messages and responses) is transport-agnostic. The first implemented transport is subprocess (same container, stdin/stdout). SSH and Kubernetes transports are defined at the interface level but left unimplemented. The protocol covers dispatch, status polling, feedback relay, and health check. A subprocess-based mock agent serves as the integration test harness."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Dispatch a Card and Confirm Agent Acceptance (Priority: P1)

A coordinare picks a card from the board and needs to hand it off to an agent for implementation. The coordinare sends the card's full context through the configured transport and immediately receives a structured acknowledgement confirming the agent has accepted the work and started. The coordinare can then track progress by polling. The protocol is explicit enough that any agent implementation — regardless of underlying AI model, tooling, or where it runs — can satisfy it by adhering to the contract.

**Why this priority**: Without a defined dispatch contract, the coordinare cannot reliably start work. This is the entry point to the entire agent-side workflow. All other protocol messages are meaningless if dispatch is ambiguous.

**Independent Test**: Run a mock agent (subprocess executable) that implements the protocol contract. Issue a dispatch from the coordinare. Verify the coordinare receives a valid acknowledgement and records the session ID for subsequent polling. Delivers a testable, self-contained dispatch cycle with no real AI model.

**Acceptance Scenarios**:

1. **Given** a card with title, description, and acceptance criteria, **When** the coordinare dispatches it to the agent, **Then** the agent responds within 30 seconds with a structured message containing a unique session ID and a status of `accepted`.
2. **Given** a dispatch request, **When** the agent cannot accept new work (e.g., already busy), **Then** the agent responds with status `busy` and the coordinare does not mark the card as dispatched.
3. **Given** a dispatch request, **When** the transport cannot reach the agent (process fails to start, connection refused), **Then** the coordinare receives a transport-level error, logs the failure, and retries according to the retry policy.
4. **Given** a dispatch request, **When** the agent receives the message but encounters an internal setup error, **Then** the agent responds with status `error` and a human-readable `reason` field, and the coordinare moves the card to Blocked.

---

### User Story 2 - Poll Agent Status During Long-Running Work (Priority: P1)

After dispatching a card, the coordinare periodically polls the agent for progress. The agent may be running for minutes or hours while an AI model writes code, runs tests, and iterates. Each poll returns a structured response indicating whether the agent is still working, has opened a PR, needs human input, or has encountered an unrecoverable error. The coordinare uses this response to advance the workflow without any human involvement.

**Why this priority**: Equal priority with dispatch because polling is the mechanism that drives all workflow progression. A dispatch that is never followed by status polling leaves the coordinare permanently in the `monitoring_agent` phase.

**Independent Test**: Run a mock agent that progresses through statuses on a timer (working → pr_opened). Poll it from the coordinare and verify the coordinare correctly transitions to `monitoring_pr` when it receives `pr_opened` with a valid PR URL.

**Acceptance Scenarios**:

1. **Given** an agent actively working on a card, **When** the coordinare polls for status, **Then** the agent returns status `working` with an optional `progress` message describing current activity.
2. **Given** an agent that has opened a pull request, **When** the coordinare polls for status, **Then** the agent returns status `pr_opened` with the PR URL and PR node ID, and the coordinare advances to `monitoring_pr`.
3. **Given** an agent that has determined it needs human input to continue, **When** the coordinare polls for status, **Then** the agent returns status `blocked` with a non-empty `questions` list, and the coordinare moves the card to Blocked on the board.
4. **Given** an agent that has encountered an unrecoverable error, **When** the coordinare polls for status, **Then** the agent returns status `error` with a `reason` field, and the coordinare moves the card to Blocked with the reason as a board comment.
5. **Given** an agent session ID, **When** the coordinare polls with an unknown or expired session ID, **Then** the agent returns status `unknown` and the coordinare treats this as a recoverable error.

---

### User Story 3 - Relay Review Feedback to Agent (Priority: P2)

Review feedback (from Copilot, human engineers, or the Customer Advocate) is collected by the coordinare and relayed to the agent through the same transport and protocol. The agent receives the feedback, understands which PR it applies to, and resumes work to address the comments. The coordinare then resumes polling for updated status.

**Why this priority**: Without feedback relay, the review cycle is broken. The coordinare can detect review comments but cannot close the loop with the agent. This is the mechanism that allows all review stages to drive autonomous remediation.

**Independent Test**: Send a `relay_feedback` message to a mock agent with sample review comments. Verify the agent acknowledges receipt and transitions back to `working` status on the next poll.

**Acceptance Scenarios**:

1. **Given** a card under review with actionable comments, **When** the coordinare sends a `relay_feedback` message containing the PR URL and review comments, **Then** the agent responds with status `acknowledged` and resumes working.
2. **Given** a relay_feedback message, **When** the agent's session has expired, **Then** the agent returns status `session_expired` and the coordinare moves the card to Blocked, as context has been lost.
3. **Given** a relay_feedback message, **When** the feedback results in the agent opening an updated PR, **Then** the agent's next status poll returns `pr_opened` with the new PR URL.

---

### User Story 4 - Pluggable Transport Layer (Priority: P1)

A developer configuring the coordinare for a new environment can change how it communicates with agents by updating a single configuration key. Whether agents run as subprocesses in the same container, on a remote SSH host, or as Kubernetes Jobs, the coordinare's orchestration logic and the JSON wire protocol are completely identical — only the transport changes. The subprocess transport is the first fully implemented option. SSH and Kubernetes transports are defined at the interface level with documented behavior so that future implementors have a clear contract, but selecting either raises an explicit "not yet implemented" error with a helpful message rather than silently failing.

**Why this priority**: The transport abstraction is foundational. Implementing it correctly from the start prevents a future rewrite when deployment requirements change. Defining the unimplemented transports now ensures the interface is stable before anything is built against it.

**Independent Test**: Configure the coordinare for subprocess transport and verify it dispatches using a local executable. Then configure an unimplemented transport and verify it produces a clear not-implemented error at startup rather than a cryptic runtime failure.

**Acceptance Scenarios**:

1. **Given** `agent_transport: subprocess` in config, **When** the coordinare dispatches a card, **Then** it spawns the configured agent executable as a subprocess, writes the JSON message to stdin, and reads the JSON response from stdout.
2. **Given** `agent_transport: ssh` in config, **When** the coordinare starts, **Then** it raises a clear startup error: "SSH transport is defined but not yet implemented. Use subprocess transport."
3. **Given** `agent_transport: kubernetes` in config, **When** the coordinare starts, **Then** it raises a clear startup error: "Kubernetes transport is defined but not yet implemented. Use subprocess transport."
4. **Given** an invalid `agent_transport` value in config, **When** the coordinare starts, **Then** it exits with a validation error listing the valid options.
5. **Given** a subprocess agent that exits with a non-zero code, **When** the coordinare reads its output, **Then** it treats the failure as a transport-level error and applies retry logic.

---

### User Story 5 - Subprocess Mock Agent for Integration Testing (Priority: P2)

A developer working on the coordinare can run the full dispatch-poll-feedback cycle against a realistic agent without needing access to a live AI model. A mock agent executable reads ProtocolMessages from stdin and writes ProtocolResponses to stdout, with its response sequence controlled by a scenario configuration file. The mock is used in integration tests and local development, and requires no infrastructure beyond the coordinare's own process.

**Why this priority**: Without a local test harness, the agent protocol can only be verified against a live AI agent. This blocks development iteration and CI testing of any coordinare logic that touches the agent interface.

**Independent Test**: Run integration tests with the mock agent configured for each scenario. Verify the coordinare progresses through the expected phases with no real AI model, no network calls, and no external services.

**Acceptance Scenarios**:

1. **Given** the mock agent configured for the `happy_path` scenario (accepted → working → pr_opened), **When** the coordinare dispatches and polls, **Then** the coordinare advances to `monitoring_pr` with the expected PR URL — no real agent or AI model involved.
2. **Given** the mock agent configured for the `blocked` scenario, **When** the coordinare polls, **Then** the coordinare moves the card to Blocked with the configured questions.
3. **Given** the mock agent configured for the `error` scenario, **When** the coordinare dispatches, **Then** the coordinare receives the error response and moves the card to Blocked with the configured reason.
4. **Given** the mock agent, **When** integration tests run in CI, **Then** no containers, network calls, or external credentials are required — the mock agent is a local executable invoked as a subprocess.
5. **Given** the mock agent configured for a `session_expired` scenario, **When** the coordinare sends `relay_feedback`, **Then** the coordinare moves the card to Blocked with a session-expired reason.

---

### Edge Cases

- What if the subprocess agent produces no output before timing out? → The coordinare treats a response timeout as `status: unknown` and applies retry logic, identical to a connection timeout in any other transport.
- What if the agent's JSON response is malformed? → The coordinare treats a non-parseable response as a transient error, logs the raw output, and retries. After exhausting retries it moves the card to Blocked.
- What if the subprocess agent writes to stderr instead of stdout? → stderr is captured separately and logged for diagnostics. Only stdout is parsed as a protocol response. A non-empty stderr with empty stdout is treated as a transport-level error.
- What if the subprocess agent takes longer than one poll interval to respond? → The transport timeout must be shorter than the poll interval. A timed-out poll is treated as `status: working`, not an error.
- What if the session ID at dispatch does not match when polling? → The coordinare uses the session ID received at dispatch. If the agent returns `unknown` for that ID, the coordinare moves to Blocked.
- What if the PR URL returned by the agent is invalid or points to a different repository? → The coordinare validates the PR URL against the configured GitHub org before advancing. An invalid URL is treated as an error response.
- What if the relay_feedback payload is very large? → No size limit is imposed. If the subprocess transport fails to write to stdin (broken pipe), the coordinare logs the error and moves the card to Blocked.
- What if two roles use different transports? → Each agent role resolves its transport independently via the agent registry. The transport interface is per-role, not global.

## Requirements *(mandatory)*

### Functional Requirements

**Transport Abstraction**

- **FR-001**: The coordinare MUST communicate with agents through a pluggable transport abstraction; no single transport MUST be hard-coded into orchestration logic.
- **FR-002**: The transport abstraction MUST expose a single operation: `send(ProtocolMessage) → ProtocolResponse`. All coordinare-to-agent communication MUST go through this operation.
- **FR-003**: The active transport MUST be selected via a configuration key (`agent_transport`). Valid values are `subprocess`, `ssh`, and `kubernetes`.
- **FR-004**: `SubprocessTransport` MUST be fully implemented: it spawns the configured agent executable as a child process, writes the JSON-serialised `ProtocolMessage` to stdin, reads the JSON-serialised `ProtocolResponse` from stdout, and exits the process after each exchange.
- **FR-005**: `SshTransport` MUST be defined at the interface level with its intended behavior documented, but MUST NOT be implemented. Selecting it MUST produce a clear startup error identifying it as not yet implemented.
- **FR-006**: `KubernetesTransport` MUST be defined at the interface level with its intended behavior documented, but MUST NOT be implemented. Selecting it MUST produce a clear startup error identifying it as not yet implemented.
- **FR-007**: Each agent role MUST have its transport and executable path (for subprocess) or connection parameters (for future transports) configured independently via the agent registry (spec 009).

**Wire Protocol**

- **FR-008**: Every protocol message sent by the coordinare MUST include: `action` (string), `session_id` (string, empty string for new dispatch), and a message-specific payload object.
- **FR-009**: Every response from the agent MUST include: `status` (string enum) and optionally `reason`, `session_id`, `questions`, `pr_url`, `pr_node_id`, and `progress`.
- **FR-010**: The `dispatch` action payload MUST include the card's title, description, acceptance criteria, board card ID, and column name.
- **FR-011**: The `status` action MUST include the session ID from the original dispatch response.
- **FR-012**: The `relay_feedback` action payload MUST include the session ID, PR URL, and an ordered list of review comments each with reviewer identity, comment body, and file/line reference where applicable.
- **FR-013**: The `health` action MUST return a response within 10 seconds indicating whether the agent is reachable and ready to accept work.
- **FR-014**: The coordinare MUST validate agent responses against the protocol schema before acting on them; malformed responses MUST be treated as transient errors.
- **FR-015**: Valid `status` values in agent responses MUST be one of: `accepted`, `working`, `pr_opened`, `blocked`, `error`, `unknown`, `busy`, `acknowledged`, `session_expired`.
- **FR-016**: The protocol contract MUST be documented as JSON Schema (draft 2020-12) files generated from Pydantic v2 models via `model_json_schema()` and published in `contracts/`. These files are the single source of truth for both the coordinare's response validator (FR-014) and the mock agent; they MUST be sufficient for an external implementor to build an agent without reading coordinare source code (SC-006).
- **FR-016a**: A configurable `transport_timeout_seconds` (default 30 s) MUST apply to all non-health sends (`dispatch`, `status`, `relay_feedback`). The health action uses a fixed 10-second timeout per FR-013. `SubprocessTransport` MUST enforce this timeout by terminating the subprocess and raising a transport-level error if no response is received within the configured window.

**Mock Agent & Testing**

- **FR-017**: A mock agent MUST be provided as a standalone executable that reads `ProtocolMessage` JSON from stdin and writes `ProtocolResponse` JSON to stdout, with its response sequence controlled by a scenario configuration file.
- **FR-018**: The mock agent MUST require no network access, no containers, and no external credentials to run.
- **FR-019**: Integration tests MUST cover at minimum: happy path (accepted → working → pr_opened), blocked path (accepted → working → blocked), error path (accepted → error), and session-expired path (relay_feedback → session_expired).

### Key Entities

- **AgentTransport** (interface): The abstract boundary between coordinare orchestration and agent communication.
  - Single operation: `send(ProtocolMessage) → ProtocolResponse`
  - Transport-level errors (process failure, timeout) are distinct from protocol-level errors (status: error)
  - Each agent role resolves its own transport instance via the agent registry

- **SubprocessTransport** (implemented): Communicates with an agent executable co-located in the coordinare's environment.
  - Spawns a new process per message exchange
  - Writes JSON `ProtocolMessage` to process stdin; reads JSON `ProtocolResponse` from stdout
  - Captures stderr separately for diagnostic logging
  - Process exits after each exchange; no persistent connection

- **SshTransport** (defined, not implemented): Would connect to a remote host via SSH, pass the JSON message via stdin, and read the response from stdout. Raises a not-implemented error if selected.

- **KubernetesTransport** (defined, not implemented): Would create a Kubernetes Job per task, inject the message as environment or config, and receive the response via HTTP callback to the coordinare's API endpoint. Raises a not-implemented error if selected.

- **ProtocolMessage** (coordinare → agent): A structured command, transport-agnostic.
  - `action`: One of `dispatch`, `status`, `relay_feedback`, `health`
  - `session_id`: Identifies the agent session; empty string on initial dispatch
  - `payload`: Action-specific data (card context, review comments, etc.)

- **ProtocolResponse** (agent → coordinare): A structured reply, transport-agnostic.
  - `status`: Current agent state (see FR-015 for valid values)
  - `session_id`: Echoed or newly assigned identifier for this work session
  - `reason`: Human-readable explanation (for `error`, `blocked`, `session_expired`)
  - `questions`: List of questions requiring human input (non-empty when status is `blocked`)
  - `pr_url`: URL of the opened pull request (non-empty when status is `pr_opened`)
  - `pr_node_id`: GitHub node ID of the PR (non-empty when status is `pr_opened`)
  - `progress`: Optional description of current activity (when status is `working`)

- **MockAgent**: A test fixture executable implementing the protocol contract via stdin/stdout with configurable response sequences.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The coordinare can complete a full dispatch → poll → pr_opened cycle against the mock agent subprocess in under 60 seconds in a local development environment.
- **SC-002**: Every valid agent response status value is exercised by at least one integration test against the mock agent.
- **SC-003**: A malformed agent response is detected and handled without crashing the coordinare in 100% of test cases.
- **SC-004**: The full integration test suite runs to completion in CI in under 2 minutes with no containers, network access, or external credentials required.
- **SC-005**: The protocol contract schema is the single authoritative source for both the coordinare's response validator and the mock agent — no duplication of protocol rules in code.
- **SC-006**: A new agent implementation can be built to the protocol contract using only the contract document and transport interface definition, without reading coordinare source code.
- **SC-007**: Selecting an unimplemented transport produces a startup error naming the transport, explaining it is not yet implemented, and stating the correct alternative — in under 1 second, before any board polling begins.

## Clarifications

### Session 2026-02-22

- Q: What machine-readable format should FR-016's protocol contract schema use? → A: JSON Schema (draft 2020-12), generated from Pydantic v2 models via `model_json_schema()` and published as static `.json` files in `contracts/`. This satisfies SC-006 (external implementors read JSON Schema, not Python code) and SC-005 (Pydantic models are the authoritative source; the exported files are derived and versioned).
- Q: What timeout applies to `status` and `relay_feedback` transport sends (only 30s for dispatch and 10s for health are specified)? → A: A single configurable `transport_timeout_seconds` (default 30 s) applies to all non-health sends (`dispatch`, `status`, `relay_feedback`). Health uses its own fixed 10-second timeout per FR-013. The transport timeout MUST be documented in the schema and enforced by `SubprocessTransport`.

## Assumptions

- Agent executables for subprocess transport are co-located in the coordinare container image or available on the system PATH. Packaging and distribution of agent executables is outside the scope of this spec.
- Each dispatch creates a new logical session identified by a session ID assigned by the agent. The coordinare stores this session ID in persisted state (spec 003-state-persistence).
- The agent is responsible for its own internal retry and recovery. The protocol reflects agent-reported state only.
- For subprocess transport, each `send` call spawns a fresh process. Session continuity is maintained by the agent reading its own persistent state on disk (e.g., a working directory or session file).
- SSH and Kubernetes transports are explicitly out of scope for implementation in this feature. Their interface definitions serve as a stable contract for future implementation.
- Review comments relayed via `relay_feedback` are pre-filtered by the coordinare — only comments from the relevant reviewer type are included for each relay phase.
- The agent registry (spec 009) is responsible for mapping role names to transport configurations and executable paths. This spec defines the transport interface; the registry defines how roles resolve to transport instances.
