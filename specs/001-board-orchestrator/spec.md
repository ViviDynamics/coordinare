# Feature Specification: Board Orchestrator Daemon

**Feature Branch**: `001-board-orchestrator`
**Created**: 2026-02-16
**Status**: Draft
**Input**: User description: "Coordinare daemon that orchestrates project board workflow, dispatching cards to AI agents and managing the full card lifecycle including PR reviews, notifications, and blocked card handling."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Board Monitoring & Card Dispatch (Priority: P1)

An engineering team has a project board with cards in the ToDo / Backlog column. The coordinare daemon runs continuously, watching the board. When no cards are in the "In Progress" or "In Review" columns, the coordinare picks the next card from "ToDo / Backlog," moves it to "In Progress," and dispatches it to the configured project agent along with the card's requirements and acceptance criteria. The agent begins working on the card in its own environment.

**Why this priority**: This is the fundamental value proposition — without the ability to monitor a board and dispatch work to an agent, no other feature has meaning. This story alone delivers an automated workflow that removes the manual step of assigning work.

**Independent Test**: Can be fully tested by setting up a board with cards in ToDo / Backlog, running the coordinare, and verifying that the first card moves to In Progress and the agent receives the card's context. Delivers automated card pickup and agent dispatch.

**Acceptance Scenarios**:

1. **Given** a board with 3 cards in ToDo / Backlog and no cards in In Progress or In Review, **When** the coordinare daemon starts, **Then** the first card in ToDo / Backlog is moved to In Progress and dispatched to the configured agent with the card's title, description, and acceptance criteria.
2. **Given** a board with 1 card in In Progress, **When** the coordinare checks the board, **Then** no new cards are moved from ToDo / Backlog (one-at-a-time constraint enforced).
3. **Given** a board with 1 card in In Review and none in In Progress, **When** the coordinare checks the board, **Then** no new cards are moved from ToDo / Backlog (waiting for review to complete).
4. **Given** a board with no cards in In Progress or In Review and an empty ToDo / Backlog column, **When** the coordinare checks the board, **Then** the coordinare remains idle and continues polling.
5. **Given** a coordinare with a project configuration file, **When** the daemon starts, **Then** it connects to the configured board provider and agent endpoint, validating that both are reachable.

---

### User Story 2 - PR Review & Merge Cycle (Priority: P2)

When the project agent finishes implementing a card and opens a pull request, the coordinare monitors the PR for reviews from the engineering team. The coordinare explicitly ignores automated CoPilot review comments and only acts on feedback from configured human team members. When human reviewers leave feedback, the coordinare relays that feedback to the agent to address. When the engineering team approves the PR, the coordinare squash-merges it into the main branch, moves the card to "Done," and triggers the next card pickup from ToDo / Backlog.

**Why this priority**: Without this story, the workflow loop is incomplete — cards get dispatched but never finish. This closes the loop and enables continuous delivery of completed work.

**Independent Test**: Can be tested by creating a PR linked to a card in In Review, leaving human review comments, and verifying the coordinare relays them to the agent. Then approve the PR and verify squash-merge occurs and card moves to Done.

**Acceptance Scenarios**:

1. **Given** a card in In Review with an open PR, **When** a configured human team member leaves review feedback, **Then** the coordinare relays that feedback to the project agent for remediation.
2. **Given** a card in In Review with an open PR, **When** CoPilot or another automated reviewer leaves comments, **Then** the coordinare ignores those comments and does not relay them to the agent.
3. **Given** a card in In Review with an approved PR (by a human team member), **When** the coordinare detects the approval, **Then** it squash-merges the PR into main, moves the card to Done, and checks ToDo / Backlog for the next card.
4. **Given** a card just moved to Done, **When** there are cards remaining in ToDo / Backlog and nothing in In Progress or In Review, **Then** the coordinare immediately picks the next card and begins the dispatch cycle.

---

### User Story 3 - Notification System (Priority: P3)

Whenever a card transitions between columns on the board, the coordinare sends a notification to both the configured email address (coordinare@vividynamics.com) and a project-specific Slack channel. The notification includes the card's current status, a description of the task, any open questions the coordinare has added to the ticket, and a summary of commits and PR activity related to the card.

**Why this priority**: The core workflow functions without notifications, but the engineering team needs visibility into what the coordinare is doing. This story ensures the team stays informed without having to watch the board constantly.

**Independent Test**: Can be tested by triggering a card column transition and verifying that both an email and a Slack message arrive with the correct contextual information.

**Acceptance Scenarios**:

1. **Given** a card moving from ToDo / Backlog to In Progress, **When** the transition completes, **Then** an email is sent to coordinare@vividynamics.com containing the card's new status, task description, and any open questions.
2. **Given** a card moving from In Progress to In Review, **When** the transition completes, **Then** a Slack message is posted to the project's configured channel with the card's status, task description, and a summary of commits made and the PR link.
3. **Given** a card moving from In Review to Done, **When** the transition completes, **Then** both email and Slack notifications include a summary of all commits included in the squash-merge.
4. **Given** a card moving to Blocked, **When** the transition completes, **Then** both notifications clearly indicate that input is required and include the specific questions needing answers.
5. **Given** a notification failure (email server down or Slack API error), **When** the send fails, **Then** the coordinare logs the failure and retries, but does not block the card workflow.

---

### User Story 4 - Blocked Card & Clarity Requests (Priority: P4)

When the project agent determines it cannot proceed with a card — either because it needs input from the engineering team or because the card's details are insufficiently specified — the coordinare moves the card to the "Blocked" column. The coordinare comments directly on the card with specific questions for the engineering team and sends notifications (email and Slack) indicating that human input is required. When the team provides answers (on the card or PR), the coordinare unblocks the card and resumes work.

**Why this priority**: This story handles the exception path. Without it, the coordinare would stall silently when an agent gets stuck. This ensures the team is always aware of blockers and can respond quickly.

**Independent Test**: Can be tested by dispatching a card with intentionally vague requirements to the agent, verifying the card moves to Blocked, a comment appears on the card, and notifications are sent. Then add a response and verify the card resumes.

**Acceptance Scenarios**:

1. **Given** an agent working on a card in In Progress, **When** the agent reports it needs input to continue, **Then** the coordinare moves the card to Blocked and comments on the card with specific questions.
2. **Given** a card dispatched to the agent, **When** the coordinare determines the card's details are insufficient for confident execution, **Then** the coordinare comments on the card asking for clarity and moves it to Blocked before the agent begins implementation.
3. **Given** a card in Blocked with open questions, **When** the engineering team answers the questions on the card, **Then** the coordinare detects the response, moves the card back to In Progress, and relays the answers to the agent.
4. **Given** a card moved to Blocked, **When** the transition occurs, **Then** email and Slack notifications are sent indicating input is required, including the specific questions.

---

### Edge Cases

- What happens when the board provider API is temporarily unavailable? The coordinare MUST retry with backoff and not crash or lose track of card state.
- What happens when the agent becomes unreachable mid-task? The coordinare MUST move the card to Blocked and notify the team that the agent is unresponsive.
- What happens when a card is manually moved by a team member while the coordinare is operating on it? The coordinare MUST detect external board changes and reconcile its internal state.
- What happens when the PR has merge conflicts? The coordinare MUST move the card to Blocked and notify the team rather than attempting to resolve conflicts.
- What happens when a card in Blocked receives no response for an extended period? The coordinare MUST send reminder notifications at a configurable interval.
- What happens when the coordinare daemon restarts? It MUST recover its state from the board and resume operation without duplicating work or missing cards.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The coordinare MUST run as a long-lived daemon process that continuously monitors a single configured project board.
- **FR-002**: The coordinare MUST enforce a one-card-at-a-time policy — only one card may be in "In Progress" or "In Review" at any given time.
- **FR-003**: The coordinare MUST move the next card from "ToDo / Backlog" to "In Progress" only when both "In Progress" and "In Review" columns are empty.
- **FR-004**: The coordinare MUST dispatch card context (title, description, acceptance criteria, and any linked resources) to the configured project agent when a card enters "In Progress."
- **FR-005**: The coordinare MUST monitor PRs associated with in-progress cards and distinguish between human team reviews and automated reviews (CoPilot, bots).
- **FR-006**: The coordinare MUST only act on review feedback from configured human team members and MUST ignore CoPilot and other automated reviewer comments.
- **FR-007**: The coordinare MUST relay human review feedback to the project agent for remediation.
- **FR-008**: The coordinare MUST squash-merge a PR into the main branch and move the corresponding card to "Done" once at least one configured human reviewer has approved the PR.
- **FR-009**: The coordinare MUST send email notifications to coordinare@vividynamics.com on every card column transition.
- **FR-010**: The coordinare MUST send Slack notifications to the project's configured Slack channel on every card column transition.
- **FR-011**: Notifications MUST include: current status (column), task description, any open questions added by the coordinare, and a summary of commits/PR activity when applicable.
- **FR-012**: The coordinare MUST move a card to "Blocked" when the agent reports it needs input or when the card details are insufficient for confident execution.
- **FR-013**: The coordinare MUST comment on the card with specific questions when moving it to Blocked.
- **FR-014**: The coordinare MUST detect when a blocked card receives answers and resume work by moving it back to In Progress.
- **FR-015**: The coordinare MUST be configurable via a layered configuration system: a configuration file serves as the base (designed to be mounted into containers via Docker Compose volumes or Kubernetes ConfigMaps/Secrets), and any configuration value MUST be overridable via environment variables. Environment variables MUST take precedence over config file values. Configuration includes: board provider credentials, board identifier, agent SSH connection details, notification targets (email, Slack), and the list of human team reviewers.
- **FR-016**: The coordinare MUST support the following board columns at minimum: "ToDo / Backlog," "Blocked," "In Progress," "In Review," and "Done."
- **FR-017**: The coordinare MUST recover gracefully from restarts by reading current board state rather than relying solely on in-memory state.
- **FR-018**: The coordinare MUST integrate with GitHub Projects as the board provider. GitHub Projects is the sole supported provider for the initial release, leveraging native integration with PRs, reviews, and issues within the same platform.
- **FR-019**: The coordinare MUST communicate with project agents via CLI invocation over SSH. The coordinare connects to the configured agent host via SSH and executes commands to dispatch card context, relay feedback, and retrieve agent status.
- **FR-020**: The coordinare MUST emit structured logs (JSON-formatted) for all significant events including card transitions, agent dispatches, PR actions, notification sends, and errors.
- **FR-021**: The coordinare MUST expose a health-check endpoint that reports daemon status, current card being processed (if any), and connectivity to external services (GitHub, agent host, SMTP, Slack).
- **FR-022**: The coordinare MUST export metrics suitable for dashboard consumption, including: cards processed count, average card cycle time, notification success/failure rates, agent dispatch latency, and error counts by category.

### Key Entities

- **Project Configuration**: Represents the coordinare's settings for a single project. Includes board provider type, board credentials, board identifier, agent connection details, notification recipients (email, Slack webhook/channel), list of human reviewers, and polling intervals.
- **Card**: A work item on the board (story, bug, or task). Has a title, description, acceptance criteria, current column/status, assigned agent, linked PR, and a history of transitions.
- **Agent**: The external AI agent assigned to implement cards. Has a connection endpoint, health status, and a reference to the project it serves. Runs in a potentially remote container.
- **Notification**: A message sent on card transitions. Contains the card's current status, task description, open questions, commit summary, and PR link. Delivered via both email and Slack.
- **Review**: Feedback on a PR associated with a card. Has an author, content, type (human or automated), and an approval status. Only human reviews trigger coordinare actions.

### Assumptions

- The coordinare manages exactly one project/board at a time (single-project assignment as stated).
- Card ordering in the ToDo / Backlog column is determined by the board's native ordering (position, priority field, or creation date depending on provider).
- The project agent exposes a well-defined interface for receiving card context and returning work status/results.
- The engineering team's review is the authoritative approval — no automated review can substitute for human sign-off.
- Email delivery uses standard SMTP configuration provided in the project configuration.
- Slack integration uses incoming webhooks or bot tokens as configured per project.
- The coordinare does not resolve merge conflicts — these are escalated to the team via Blocked status.
- Reminder notifications for stale blocked cards are sent at configurable intervals (default: daily).

## Clarifications

### Session 2026-02-16

- Q: How should operators monitor the coordinare daemon's health? → A: Full observability stack — structured logging, health-check endpoint, and metrics export for dashboards.
- Q: How should secrets and configuration be managed? → A: Layered configuration — config file as base (mounted via Docker Compose or Kubernetes), with environment variables taking precedence to override any config file value. Secrets included in config file or env vars; env vars always win.
- Q: How many human approvals are required before the coordinare merges a PR? → A: One approval from any configured human reviewer.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The coordinare picks up the next ToDo / Backlog card and dispatches it to the agent within 60 seconds of In Progress and In Review columns becoming empty.
- **SC-002**: Card column transitions trigger email and Slack notifications within 2 minutes of the transition.
- **SC-003**: The coordinare correctly ignores 100% of automated/CoPilot review comments and acts only on configured human team member feedback.
- **SC-004**: Approved PRs are squash-merged and the corresponding card moved to Done within 5 minutes of the final approval.
- **SC-005**: When an agent reports a blocker or card details are insufficient, the card is moved to Blocked and team is notified within 2 minutes.
- **SC-006**: The coordinare recovers from a restart and resumes correct operation within 60 seconds without duplicating card assignments or missing transitions.
- **SC-007**: The engineering team can configure a new project (board, agent, notifications) and have the coordinare operational without code changes.
- **SC-008**: Zero cards are lost or stuck in an inconsistent state during normal operation over a 30-day period.
