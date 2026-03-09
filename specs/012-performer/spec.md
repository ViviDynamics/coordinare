# Feature Specification: Performer

**Feature Branch**: `012-performer`
**Created**: 2026-03-03
**Status**: Draft

## Overview

The performer is the orchestra member that does the actual coding work the coordinare assigns. It is a self-contained, deployable unit that receives a score (card assignment) from the coordinare, sets up its own stand (workspace), runs an AI coding agent against the codebase, and returns the result of its performance — a pull request — back to the coordinare. The performer lives alongside the coordinare in the same repository but is built and deployed independently as a container image.

Today coordinare has no performer to dispatch to — this is the first concrete implementation of the other side of the coordinare wire protocol.

## Clarifications

### Session 2026-03-03

- Q: Does the performer process loop and handle multiple messages in a single run, or exit after each message and persist state to disk? → A: The performer runs as a long-lived loop for one full performance — handling dispatch then all subsequent status/relay_feedback messages — and exits only when the performance concludes (Option B).
- Q: When the target branch already exists on the remote and cannot be pushed to, what should the performer do? → A: Return `error` status with a clear reason; do not push; the coordinare is responsible for retrying with a clean branch (Option B).
- Q: What is the maximum allowed wall-clock time for a single AI backend session before the performer declares it timed out? → A: Configurable via `AGENT_TIMEOUT` env var with a 30-minute default. Additionally, status responses SHOULD include runtime metrics: tokens processed (where the backend exposes it), performer PID and child PIDs, memory usage, and CPU usage — so the coordinare has visibility into backend health during long-running sessions.
- Q: How is relay_feedback delivered to the running AI backend, and does it vary by backend type? → A: Feedback delivery is the responsibility of the backend adapter — each backend strategy implements its own input channel (e.g. stdin for interactive CLI tools like opencode, an API call for API-based agents). The performer treats feedback delivery as an operation on the backend abstraction, following a strategy pattern so that swapping backends does not require changes to the performer's protocol layer.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Perform a Score End-to-End (Priority: P1)

When a coordinare dispatches a score (card) to a performer, the performer sets up its stand, runs the AI coding backend against the codebase, pushes the result to a branch, opens a pull request, and reports `pr_opened` with the PR URL back to the coordinare. A human reviewer then sees a real pull request in GitHub ready for review.

**Why this priority**: This is the entire point of the system. Without a complete end-to-end performance — card in, PR out — nothing else matters.

**Independent Test**: Can be tested by sending a dispatch message to the performer directly (via stdin) with a real repo, branch, and token, and verifying a pull request appears in GitHub within the expected time window.

**Acceptance Scenarios**:

1. **Given** a performer receives a valid dispatch payload with `repo_url`, `branch`, and `github_token`, **When** the AI coding backend completes its work, **Then** the performer pushes the branch and opens a GitHub pull request, and returns `{"status": "pr_opened", "pr_url": "...", "pr_node_id": "..."}` on the next status poll.
2. **Given** a performer container starts fresh with no prior state, **When** it receives a dispatch, **Then** it performs the full setup-work-push cycle without requiring any pre-existing files or credentials on the host.
3. **Given** the AI coding backend produces no meaningful changes, **When** the performer attempts to push, **Then** it returns an appropriate `error` or `blocked` status rather than opening an empty pull request.

---

### User Story 2 — Health Check Before Dispatch (Priority: P2)

Before the coordinare dispatches a score, it asks the performer if it is ready. The performer responds immediately with a healthy or unhealthy status so the coordinare can route accordingly without waiting for a timeout.

**Why this priority**: The coordinare's health-check-before-dispatch logic (already implemented) depends on a fast, reliable health response. Without it, the coordinare blocks on an unresponsive performer.

**Independent Test**: Can be tested by sending `{"action": "health"}` to the performer and verifying `{"status": "healthy"}` is returned within 2 seconds regardless of whether a backend is configured or warmed up.

**Acceptance Scenarios**:

1. **Given** the performer container is running and its coding backend is available, **When** it receives a health action, **Then** it responds `{"status": "healthy"}` within 2 seconds.
2. **Given** the performer container is running but its coding backend is unavailable or misconfigured, **When** it receives a health action, **Then** it responds with a non-healthy status and a human-readable reason.

---

### User Story 3 — Status Polling During a Performance (Priority: P2)

While the performer is working, the coordinare polls it periodically for status. The performer reports its current state — still working, blocked on a question, errored, or done — so the coordinare can decide what to do next.

**Why this priority**: Without status polling the coordinare has no visibility into a running performance and cannot relay feedback or detect failures.

**Independent Test**: Can be tested by dispatching a score and immediately polling status before the backend finishes — verifying `working` is returned — then polling again after completion and verifying `pr_opened`.

**Acceptance Scenarios**:

1. **Given** a performance is in progress, **When** the coordinare sends a status action with the session ID, **Then** the performer returns `{"status": "working"}` with an optional progress note.
2. **Given** the AI backend has raised a question it cannot resolve, **When** the coordinare polls status, **Then** the performer returns `{"status": "blocked", "questions": ["..."]}`.
3. **Given** the performance has completed successfully, **When** the coordinare polls status, **Then** the performer returns `{"status": "pr_opened", "pr_url": "...", "pr_node_id": "..."}`.
4. **Given** the AI backend encountered an unrecoverable error, **When** the coordinare polls status, **Then** the performer returns `{"status": "error", "reason": "..."}`.

---

### User Story 4 — Configurable AI Backend (Priority: P3)

The performer is not tied to a single AI coding tool. The backend (opencode by default) can be switched by setting a single environment variable or config value, so the same performer container can be used with different AI agents as they mature or as operator preference changes.

**Why this priority**: Avoids lock-in to a single AI tool at a time when the space is evolving rapidly. Opencode is the default but operators must be able to switch without rebuilding the image.

**Independent Test**: Can be tested by running the performer with `AGENT_BACKEND=opencode` and verifying it dispatches to opencode, then switching to a different valid backend and verifying the switch takes effect.

**Acceptance Scenarios**:

1. **Given** `AGENT_BACKEND` is not set, **When** the performer receives a dispatch, **Then** it runs opencode as the default backend.
2. **Given** `AGENT_BACKEND` is set to a supported value, **When** the performer receives a dispatch, **Then** it runs the specified backend instead.
3. **Given** `AGENT_BACKEND` is set to an unsupported value, **When** the performer starts, **Then** it reports unhealthy on the health check with a clear explanation.

---

### User Story 5 — Two-Tier Image Strategy (Priority: P3)

The performer ships as two published container images: a lean **base** image containing only the protocol entrypoint and AI backend, and a **full** image built on top of it that includes a standard set of common language runtimes and build tools. Operators can use the full image out of the box, extend the base image with their own dependencies, or ignore both and bring their own image entirely.

**Why this priority**: The base image is the foundation for all deployment modes. The full image removes friction for teams who just want something that works without customisation. The extension pattern enables teams with unusual stacks to stay compatible without forking the project.

**Independent Test**: Can be tested by verifying the base image contains the entrypoint and backend but no language runtimes, and the full image contains the base plus at minimum Node.js, Python, and a common build toolchain — each confirmable by running the respective image and checking installed tools.

**Acceptance Scenarios**:

1. **Given** an operator pulls `coordinare-performer:base`, **When** they run a dispatch against a repository with no special runtime requirements, **Then** the performance completes successfully using only what the base image provides.
2. **Given** an operator pulls `coordinare-performer:full`, **When** they run a dispatch against a Node.js, Python, or Ruby project without any custom image, **Then** the performance completes successfully without any additional setup.
3. **Given** an operator writes a custom Dockerfile beginning with `FROM coordinare-performer:base`, **When** they add their project-specific dependencies and build the image, **Then** the resulting image behaves as a fully functional performer.
4. **Given** the `performer_image` field is set in coordinare's configuration, **When** coordinare creates a Kubernetes Job for a dispatch, **Then** it uses that image rather than any default — allowing operators to supply any of the three image tiers (base, full, or custom).

---

### User Story 6 — Stateless Isolation Between Performances (Priority: P3)

Each performance is fully isolated from every other. No files, credentials, environment state, or session data persist from one dispatch to the next within a single performer container, and no state leaks between concurrent performers.

**Why this priority**: Without isolation, a failed or compromised performance could contaminate future performances or leak credentials across sessions.

**Independent Test**: Can be tested by running two dispatches sequentially in the same container and verifying no files from the first performance exist during the second, and that credentials from the first do not appear in the second's environment.

**Acceptance Scenarios**:

1. **Given** a performer has completed one performance, **When** it receives a second dispatch, **Then** the stand (workspace) from the first performance is fully cleaned up before the second begins.
2. **Given** two performer containers receive concurrent dispatches, **When** both are running simultaneously, **Then** their stands are completely separate and neither can read the other's files or credentials.

---

### Edge Cases

- What happens if the repository clone fails (bad token, network error, repo not found)?
- If the AI backend exceeds the `AGENT_TIMEOUT` limit (default 30 minutes) without completing, the performer MUST terminate the backend, clean up the stand, and return `{"status": "error", "reason": "backend timed out"}` on the next status poll.
- If the branch already exists on the remote and cannot be pushed to, the performer MUST return `{"status": "error", "reason": "..."}` describing the conflict; it MUST NOT force-push or rename the branch.
- What if the AI backend produces a merge conflict when pushing?
- What if the performer container runs out of disk space mid-clone or mid-generation?
- What if the session ID on a status poll does not match any known active performance?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The performer MUST implement the coordinare wire protocol: read one JSON message from stdin and write one JSON response to stdout — for each message in the performance loop. The loop runs for the duration of one full performance (from `dispatch` through a terminal state) and the process exits only when a terminal state (`pr_opened`, `error`) is reached or stdin closes.
- **FR-002**: The performer MUST support four action types: `dispatch`, `status`, `relay_feedback`, and `health`.
- **FR-003**: On a `dispatch` action, the performer MUST clone the repository from `repo_url` onto a local stand, check out the specified `branch`, configure git credentials using the provided `github_token`, and begin running the AI coding backend.
- **FR-004**: On a `dispatch` action, the performer MUST return `{"status": "accepted", "session_id": "..."}` immediately so the coordinare can begin polling, without waiting for the backend to complete.
- **FR-005**: On a `status` action, the performer MUST return the current state of the named session: `working`, `pr_opened` (with `pr_url` and `pr_node_id`), `blocked` (with `questions`), `error` (with `reason`), or `session_expired` if the session is unknown. Status responses SHOULD include a `metrics` object with: `tokens_processed` (integer, where the backend exposes it), `pid` (performer process ID), `child_pids` (list of active sub-process IDs), `memory_bytes` (current RSS), and `cpu_percent` (current CPU utilisation). All metric fields are best-effort and may be omitted if unavailable.
- **FR-006**: On a `health` action, the performer MUST return `{"status": "healthy"}` if it is ready to accept a dispatch, or a non-healthy status with a `reason` if it is not.
- **FR-007**: When the AI coding backend completes, the performer MUST push the working branch to the remote and open a GitHub pull request using the `github_token` from the dispatch payload.
- **FR-008**: The performer MUST support a configurable AI coding backend, defaulting to opencode, selectable via the `AGENT_BACKEND` environment variable. Each backend MUST be implemented as a self-contained adapter (strategy) that encapsulates: how to start the backend, how to monitor its progress, how to deliver relay_feedback to it, and how to stop it. Swapping backends MUST NOT require changes to the performer's protocol layer.
- **FR-009**: The performer MUST clean up its stand (cloned workspace) after each performance ends, regardless of outcome.
- **FR-010**: The performer MUST be fully stateless across performances — no credentials, source files, or session data from one performance may be accessible during any other.
- **FR-011**: The performer MUST be packaged as a **base** Docker image buildable from `agent/performer/Dockerfile` — containing the performer entrypoint and AI backend only, with no language-specific runtimes. This image is the extension point for custom environments.
- **FR-012**: A **full** Docker image MUST be provided at `agent/performer/images/full/Dockerfile`, built `FROM` the base image and extended with a curated set of common language runtimes and build tools (at minimum: Node.js LTS, Python 3.12, and standard POSIX build utilities). This image MUST work out of the box for the majority of common project types without any customisation.
- **FR-013**: The full image MUST NOT replace or override the performer entrypoint — it extends the environment only, leaving all protocol behaviour unchanged.
- **FR-014**: The performer repository MUST include a README documenting: how to build each image tier, how to extend the base image for a custom stack, how to run a local test performance, and how to verify protocol compliance.
- **FR-015**: The performer MUST enforce a configurable backend timeout via the `AGENT_TIMEOUT` environment variable (default: 30 minutes). When the timeout elapses, the performer MUST terminate the backend process tree, clean up the stand, and transition the session to `error` state with a human-readable reason.
- **FR-016**: Status responses SHOULD include a `metrics` object containing best-effort runtime telemetry: tokens processed by the AI backend (where the backend exposes a token count), the performer's process ID, active child process IDs, current memory usage (RSS in bytes), and current CPU utilisation (percentage). Metric fields are individually omittable if the data is unavailable.

### Key Entities

- **Score**: The dispatch payload received from the coordinare — contains the card's title, description, acceptance criteria, `repo_url`, `branch`, and `github_token`.
- **Stand**: The ephemeral local workspace created for a single performance — a cloned repository directory that exists only for the duration of that performance.
- **Performance**: A single end-to-end session from dispatch receipt through PR creation, identified by a unique session ID returned on acceptance.
- **Backend**: The AI coding agent the performer delegates actual code generation to (opencode by default) — the actual coding intelligence (e.g. opencode, Claude Code, Codex) that reads the codebase and produces changes. Each backend is implemented as an adapter (strategy) that encapsulates start, monitor, relay_feedback delivery, and stop — so the performer's protocol layer is unaffected by backend swaps.
- **Base Image**: The minimal performer container image (`coordinare-performer:base`) — protocol entrypoint + AI backend only. The intended extension point for custom environments.
- **Full Image**: A ready-to-use performer container image (`coordinare-performer:full`) built on top of the base, extended with common language runtimes and build tools. No customisation required for typical projects.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of accepted dispatches that complete without backend error result in an open pull request in the target GitHub repository.
- **SC-002**: Health check responses are returned within 2 seconds in 99% of cases.
- **SC-003**: `dispatch` acceptance (session ID returned) occurs within 5 seconds of receiving the message — before the backend has finished.
- **SC-004**: 100% of stands are cleaned up within 30 seconds of a performance ending, under normal operating conditions.
- **SC-005**: Switching the AI backend requires only a change to a single environment variable — no image rebuild, no config file changes beyond that variable.
- **SC-006**: A developer unfamiliar with the codebase can pull `coordinare-performer:full`, run a local test performance against a real repository, and observe a pull request opened — following only the README — within 30 minutes.
- **SC-007**: The base image size is smaller than the full image by at least the footprint of the bundled language runtimes — confirming the two tiers are genuinely distinct and the base is not carrying unnecessary weight.

## Assumptions

- The coordinare wire protocol (JSON stdin/stdout, action types, response shapes) is fully defined by the existing `ProtocolMessage` and `ProtocolResponse` models in the coordinare codebase — the performer must conform to those schemas exactly.
- `opencode` is available as an installable CLI tool and can be invoked non-interactively from within a container.
- The `github_token` provided in the dispatch payload has sufficient permissions to clone the repository, push a branch, and open a pull request.
- The performer runs as a single-use process per invocation (one dispatch at a time per container instance) — horizontal scaling is handled by running multiple container instances, not by multiplexing within one. Within a single invocation, the performer loops handling all protocol messages (dispatch, then status/relay_feedback polls) until the performance concludes, then exits.
- The PR title and body are generated by the AI coding backend or derived from the card title and description — the performer does not need to synthesise them independently.
- `relay_feedback` is a supported action (coordinare may send human answers to blocked questions, or PR review comments). The performer's protocol layer accepts and routes the message; the backend adapter is responsible for delivering it to the running agent via whatever input channel that backend supports (stdin for interactive CLI tools, API call for API-based agents, etc.).
- The two-tier image strategy (base + full) is a publishing concern — both images are built from the same `agent/performer/` directory tree and share the same entrypoint. The full image adds no logic, only environment.
- For subprocess and SSH transports, no container image is involved — the host machine's environment is used directly, and the image tier concept does not apply.
