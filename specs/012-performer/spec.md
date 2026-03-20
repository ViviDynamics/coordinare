# Feature Specification: Performer

**Feature Branch**: `012-performer`
**Created**: 2026-03-03
**Status**: Draft

## Overview

A performer is an AI-backed actor that carries out a specialized role in the software development lifecycle on behalf of the coordinare. Performers are self-contained, deployable units — each receives a score (card assignment) from the coordinare, does its assigned work, and returns a result. All performers share the same wire protocol (JSON stdin/stdout) and are built and deployed independently as container images.

The development lifecycle supported by performers is fully sequential — each role completes and approves before the next is triggered. This keeps the feedback cycle simple and predictable; parallelisation is a future optimisation once the sequential path is proven.

```
advocate → assessor → architect → implementer → reviewer → security → QA → tech writer → human
                                       ↑                                                     │
                                       └─────────── human review feedback ───────────────────┘
```

When the human reviews the PR and requests changes, the coordinare reads the review comments and classifies them to determine where in the cycle to re-enter — rather than always restarting from the implementer. This classification is performed by a dedicated **`handle_human_review_feedback`** coordinare node (not a performer) that routes feedback to the appropriate role.

- **Advocate**: scans GitHub issues and surfaces new work for the coordinare board (see spec 007)
- **Assessor**: evaluates whether a card is sufficiently specified before dispatch (built into the coordinare; see spec 001)
- **Architect**: produces a technical design and approach for the card before any code is written — the implementer follows this plan rather than starting from a blank slate
- **Implementer**: clones the repo, writes code following the architect's plan, pushes a branch, and opens a pull request
- **Reviewer**: reads the PR diff, leaves inline comments, and either approves or requests changes — before any human sees the PR
- **Security**: performs a focused security review of the diff — routes code-level findings back to the implementer, and architecture-level findings back to the architect
- **QA**: checks out the approved branch, exercises the application, and validates that the acceptance criteria are met — triggered only after the security reviewer approves
- **Tech Writer**: updates documentation, changelogs, and READMEs to reflect the change — triggered only after QA passes

**Feedback loops** (all sequential — a role sends feedback and waits for another attempt before proceeding):

| Role | Finding type | Feedback goes to |
|------|-------------|-----------------|
| Reviewer | Code quality / logic issues | Implementer |
| Security | Code-level vulnerability | Implementer |
| Security | Architecture-level vulnerability | Architect |
| QA | Acceptance criteria not met | Implementer |
| Tech Writer | Needs implementation clarification | Implementer |
| Human | Code / implementation issue | Implementer |
| Human | Design / architecture concern | Architect |
| Human | Security issue | Security |
| Human | Acceptance criteria not met | QA |
| Human | Documentation gap | Tech Writer |
| Human | Card requirements unclear | Assessor |

Each role has a configurable maximum number of feedback cycles before the card is BLOCKED for human intervention.

**GitHub board column mapping**:

The coordinare tracks its internal performer stage in its own state machine. The GitHub project board shows a simplified, human-readable view with exactly six columns:

| Board column | Meaning |
|-------------|---------|
| `Backlog` | Not yet ready for work; not managed by coordinare |
| `TODO` | Ready for coordinare to pick up |
| `In Progress` | Coordinare is actively directing a performance — covers all internal stages from assessor through tech writer |
| `Blocked` | Needs human input, or a fundamental dependency/architectural blocker exists |
| `In Review` | All performer stages complete; PR is open and ready for human review |
| `Done` | Human approved; coordinare squash-merged the PR |

Board transitions:
- `TODO` → `In Progress`: coordinare picks up the card and begins the performer chain
- `In Progress` → `Blocked`: any performer exhausts its retry limit, or raises a blocker requiring human input
- `In Progress` → `In Review`: tech writer completes; PR is ready for human eyes
- `In Review` → `Done`: human approves; coordinare squash-merges and closes the card
- `In Review` → `In Progress`: human requests changes; coordinare classifies the feedback and re-enters the performer chain at the appropriate role
- `Blocked` → `TODO`: human resolves the blocker; coordinare re-queues the card

The board column model is intentionally minimal. Additional columns (e.g. per-stage visibility) are a future configuration concern and out of scope for this spec.

**Each performer role is a distinct deployable unit** with its own backend, tools, and response contract. They share the same wire protocol (spec 004), but what runs on the other side of the wire differs per role:

| Role | Nature of work |
|------|---------------|
| Architect | Design and planning — no code execution required |
| Implementer | Active coding — needs workspace, git access, tool use |
| Reviewer | Code analysis — reads PR diff, posts GitHub review comments |
| Security | Vulnerability analysis — specialized scanning + synthesis |
| QA | Acceptance validation — needs workspace to build and run the application |
| Tech Writer | Documentation — reads changes, writes and commits docs updates |

**No vendor lock-in**: each role's backend agent is declared in configuration. The coordinare does not hardcode any specific AI provider. A team may run the architect on one model, the implementer on another, and the security reviewer on a third — or all on the same. The configuration drives it.

**Coordinare-side registry**: the coordinare maintains a `performer_services` registry — a map from role name to the configured service instance for that role. `dispatch_performer` looks up the registry at dispatch time. Adding a new role requires only a new registry entry, not new graph nodes.

**Persona injection**: each service injects its configured persona instructions into the dispatch payload before sending. The persona is role-scoped and backend-agnostic (see spec 018).

This spec introduces the performer pattern and the first concrete implementation: the **implementer**. The architect, reviewer, security, QA, and tech writer roles are specified here as future user stories and will be built in subsequent implementation cycles. Today coordinare has no performer to dispatch to — this is the first concrete implementation of the other side of the coordinare wire protocol.

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

### User Story 7 — Architect Performer (Priority: P4 — future)

Before the implementer begins coding, an architect performer is dispatched with the card details. It produces a technical design document: which files to touch, what approach to take, key decisions and trade-offs, and any risks or unknowns. The implementer receives this plan as part of its dispatch payload and follows it rather than starting from a blank slate.

**Why this priority**: Without an architect, the implementer makes structural decisions ad hoc on every card — leading to inconsistent approaches and avoidable rework. The architect front-loads the thinking so the implementer executes rather than designs.

**Independent Test**: Dispatch an architect with a real card; verify it produces a structured technical plan covering files to change, approach, and key decisions. Dispatch the implementer with that plan; verify the resulting code follows it.

**Acceptance Scenarios**:

1. **Given** an architect is dispatched with a card, **When** it completes, **Then** it returns a structured technical plan containing: files to change, approach summary, key decisions, and known risks.
2. **Given** an architect's plan is included in the implementer's dispatch payload, **When** the implementer runs, **Then** the resulting code follows the plan's structural decisions.
3. **Given** the architect determines the card is impossible or contradictory to implement, **When** it cannot produce a valid plan, **Then** it returns `blocked` with a specific explanation so the assessor or human can refine the card.
4. **Given** security finds an architecture-level vulnerability in a prior cycle, **When** the architect is re-dispatched with the security findings, **Then** it produces a revised plan addressing the vulnerability before the implementer attempts another cycle.

---

### User Story 9 — Security Reviewer Performer (Priority: P4 — future)

After the reviewer approves, a security performer reviews the diff for vulnerabilities: injection flaws, secrets in code, insecure dependencies, missing auth checks, and architectural security concerns. Code-level findings are sent back to the implementer; architecture-level findings (wrong auth model, insecure data flow) are sent back to the architect for a redesign. The security reviewer only approves — passing to QA — when no unresolved findings remain.

**Why this priority**: Security review is a specialized discipline distinct from code quality review. Running it as a dedicated sequential step ensures it is never skipped and its findings are always routed to the right level of the stack.

**Independent Test**: Dispatch a security reviewer against a diff containing a known SQL injection vulnerability; verify it returns `changes_requested` with the finding categorized as code-level and routed to the implementer. Dispatch it against a clean diff; verify it returns `approved`.

**Acceptance Scenarios**:

1. **Given** the security reviewer is dispatched with a PR diff, **When** it finds a code-level vulnerability (e.g. injection, hardcoded secret), **Then** it returns `changes_requested` with the finding marked as implementer-level and does not advance to QA.
2. **Given** the security reviewer finds an architecture-level vulnerability (e.g. broken auth model), **When** it cannot be fixed by the implementer alone, **Then** it returns `changes_requested` with the finding marked as architect-level, triggering an architect re-dispatch.
3. **Given** the security reviewer finds no issues, **When** its review is complete, **Then** it returns `approved` and QA is triggered.
4. **Given** security has exceeded its configured maximum fix cycles without approval, **Then** it returns `blocked` for human intervention.

---

### User Story 10 — Tech Writer Performer (Priority: P5 — future)

After QA passes, a tech writer performer reviews the changes and updates any affected documentation — README sections, API docs, changelogs, inline code comments where behavior has changed. The updated docs are committed to the same branch before the PR is presented to a human reviewer.

**Why this priority**: Documentation is consistently the last thing addressed in a development cycle and frequently skipped under time pressure. Automating it as a dedicated final step ensures the PR a human sees is complete — code and docs together.

**Independent Test**: Dispatch a tech writer against a branch containing code changes with no corresponding doc updates; verify it commits documentation updates to the branch before returning `done`.

**Acceptance Scenarios**:

1. **Given** the tech writer is dispatched with a branch that has code changes, **When** it identifies affected documentation, **Then** it commits updated docs to the same branch and returns `done`.
2. **Given** the changes require no documentation updates (e.g. internal refactor with no behavior change), **When** the tech writer finds nothing to update, **Then** it returns `done` without committing any changes.
3. **Given** the tech writer needs clarification about what changed or why, **When** it cannot determine the scope of documentation needed, **Then** it returns `blocked` with specific questions routed back to the implementer.

---

### User Story 8 — Reviewer Performer (Priority: P4 — future)

After the implementer opens a PR, a reviewer performer is dispatched with the PR URL and diff. It reads the changes, assesses code quality and correctness, and either approves the PR or requests specific changes via inline GitHub review comments. If changes are requested, the feedback is relayed back to the implementer for another coding cycle. The reviewer repeats this loop until it is satisfied, then approves — only then is the QA performer triggered.

**Why this priority**: Automated code review before human involvement catches obvious issues (style violations, missing tests, logic errors) and reduces the review burden on humans. It also closes the feedback loop faster than waiting for a human reviewer.

**Independent Test**: Dispatch a reviewer with a known PR containing deliberate issues; verify it leaves review comments and returns `changes_requested`. Then dispatch it against a clean PR and verify it returns `approved`.

**Acceptance Scenarios**:

1. **Given** the reviewer is dispatched with a PR URL and diff, **When** it finds issues in the code, **Then** it posts inline review comments on the PR and returns `changes_requested` with a summary of the issues.
2. **Given** the reviewer is dispatched with a PR URL and diff, **When** the code meets its standards, **Then** it approves the PR on GitHub and returns `approved`.
3. **Given** the reviewer has requested changes and the implementer has pushed a new commit, **When** the reviewer is dispatched again, **Then** it re-reads only the new changes and updates its review accordingly.
4. **Given** the reviewer has exceeded a configurable number of review cycles without approval, **When** the next dispatch occurs, **Then** it returns `blocked` with a summary so a human can intervene.

---

### User Story 8 — QA Performer (Priority: P4 — future)

After the reviewer approves the PR, a QA performer is dispatched with the branch and the card's acceptance criteria. It checks out the branch, builds and runs the application, and exercises the stated acceptance criteria. If all criteria pass it returns `passed`; if any fail it sends a detailed failure report back to the implementer via a separate feedback channel for another fix cycle.

**Why this priority**: Automated acceptance testing before human review ensures the PR actually does what the card says, not just that the code looks correct. It shifts defect detection left and reduces back-and-forth in human review.

**Independent Test**: Dispatch a QA performer against a branch with a known failing acceptance criterion; verify it returns `failed` with specific failure details. Then fix the branch and dispatch again; verify it returns `passed`.

**Acceptance Scenarios**:

1. **Given** the QA performer is dispatched with a branch and acceptance criteria, **When** all criteria are met by the running application, **Then** it returns `passed` and signals the coordinare to proceed to human review.
2. **Given** the QA performer is dispatched with a branch and acceptance criteria, **When** one or more criteria are not met, **Then** it returns `failed` with specific details of which criteria failed and what the observed behavior was.
3. **Given** QA has reported a failure and the implementer has pushed a fix, **When** QA is dispatched again, **Then** it re-validates all acceptance criteria from scratch (not just the previously failing ones).
4. **Given** QA has exceeded a configurable number of fix cycles without all criteria passing, **When** the next dispatch occurs, **Then** it returns `blocked` with a full failure report so a human can intervene.
5. **Given** QA and the reviewer use separate feedback channels, **When** QA sends a failure report to the implementer, **Then** it does not share, overwrite, or interfere with any pending reviewer feedback.

---

### User Story 11 — Human Review Feedback Routing (Priority: P5 — future)

After the tech writer completes and the PR is presented for human review, a human may approve (done), or request changes with comments. When changes are requested, the coordinare reads the review comments and classifies them to determine where in the lifecycle to re-enter — rather than blindly restarting from the implementer.

This classification is performed by a dedicated coordinare node (`handle_human_review_feedback`), not a performer. It reads the PR comments and routes back to the appropriate performer role, which then runs its full cycle forward again from that point.

**Why this priority**: Blindly routing all human feedback to the implementer is wasteful and incorrect. A comment about a flawed design decision should go to the architect; a comment about missing tests to the QA; a comment about outdated docs to the tech writer. Smart re-entry avoids unnecessary rework and keeps the downstream roles (reviewer, security, QA, tech writer) honest — they re-run from the re-entry point forward.

**Routing rules**:

| Human feedback type | Re-enter at |
|--------------------|-------------|
| Code logic / implementation issue | Implementer |
| Design / architecture concern | Architect |
| Security vulnerability | Security |
| Acceptance criteria not met / behavior wrong | QA |
| Documentation missing or incorrect | Tech Writer |
| Card requirements unclear or contradictory | Assessor |

Once re-entered, the lifecycle runs forward sequentially from that point through to human review again.

**Independent Test**: Create a PR with a human review comment about a documentation gap; verify the coordinare routes to the tech writer (not the implementer). Create a comment about a design concern; verify it routes to the architect.

**Acceptance Scenarios**:

1. **Given** a human requests changes with a comment about a code bug, **When** `handle_human_review_feedback` classifies the comment, **Then** the implementer is dispatched with the feedback and the lifecycle proceeds forward from implementer → reviewer → security → QA → tech writer → human.
2. **Given** a human requests changes with a comment about a design flaw, **When** `handle_human_review_feedback` classifies the comment, **Then** the architect is dispatched with the feedback and the lifecycle proceeds forward from architect → implementer → reviewer → security → QA → tech writer → human.
3. **Given** a human review contains comments of mixed types (e.g. a doc gap and a code bug), **When** `handle_human_review_feedback` processes them, **Then** it re-enters at the earliest affected role so all issues are addressed in a single cycle.
4. **Given** a human approves the PR without requesting changes, **When** the coordinare detects the approval, **Then** the card is marked done and no further performer dispatch occurs.
5. **Given** the coordinare cannot confidently classify a human comment, **When** classification is ambiguous, **Then** it defaults to re-entering at the implementer (safest re-entry point) and includes the original comment verbatim in the dispatch payload.

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

- **FR-017** *(future — architect)*: An architect performer MUST accept a dispatch payload containing the card's title, description, and acceptance criteria, and return a structured technical plan: files to change, approach summary, key decisions, and known risks.
- **FR-018** *(future — architect)*: The architect's plan MUST be included in the subsequent implementer dispatch payload as a dedicated field so the implementer receives it as part of its working context.
- **FR-019** *(future — architect)*: When the architect receives security findings marked as architecture-level, it MUST produce a revised plan addressing those findings before the implementer is re-dispatched.
- **FR-020** *(future — reviewer)*: A reviewer performer MUST accept a dispatch payload containing `pr_url`, `pr_node_id`, and the card's acceptance criteria, in addition to the standard workspace fields.
- **FR-021** *(future — reviewer)*: A reviewer performer MUST return one of: `approved` (PR meets standards), `changes_requested` (with inline comments posted to GitHub and a summary), or `blocked` (max review cycles exhausted).
- **FR-022** *(future — reviewer)*: The reviewer MUST post review comments directly to the GitHub PR so that human reviewers can see the full review history in the GitHub UI.
- **FR-023** *(future — security)*: A security performer MUST accept a dispatch payload containing the PR diff and categorize findings as either code-level (route to implementer) or architecture-level (route to architect).
- **FR-024** *(future — security)*: A security performer MUST return one of: `approved` (no findings), `changes_requested` with a `target` field indicating `implementer` or `architect`, or `blocked` (max cycles exhausted).
- **FR-025** *(future — security)*: Security MUST only be triggered after the reviewer has returned `approved` — roles are strictly sequential.
- **FR-026** *(future — QA)*: A QA performer MUST accept a dispatch payload containing the branch, `repo_url`, `github_token`, and the card's acceptance criteria. It MUST check out the branch, build and run the application, and validate each criterion.
- **FR-027** *(future — QA)*: A QA performer MUST return one of: `passed` (all criteria met), `failed` (with specific per-criterion failure details and observed behavior), or `blocked` (max QA cycles exhausted).
- **FR-028** *(future — QA)*: QA feedback MUST travel through a separate feedback channel from reviewer and security feedback — the coordinare MUST NOT merge or conflate them; the implementer receives each as distinct inputs.
- **FR-029** *(future — QA)*: QA MUST only be triggered after the security performer has returned `approved`.
- **FR-030** *(future — tech writer)*: A tech writer performer MUST accept a dispatch payload containing the branch and a summary of changes, check out the branch, update affected documentation, and commit those changes to the branch before returning `done`.
- **FR-031** *(future — tech writer)*: Tech writer MUST only be triggered after QA has returned `passed`.
- **FR-032** *(future — all roles)*: All performer roles MUST share the same wire protocol (JSON stdin/stdout, action types, response shapes). Role-specific dispatch payload fields are additive extensions to the base Score model.
- **FR-037**: The coordinare MUST NOT hardcode any AI provider or backend for any performer role. Each role's backend agent MUST be independently configurable via the coordinare configuration file.
- **FR-038**: The coordinare MUST maintain a performer services registry — a map from role name to the configured service instance for that role. `dispatch_performer` resolves the correct service at dispatch time by looking up the current `performer_stage` in this registry.
- **FR-039**: The configuration MUST allow each performer role to declare its own backend independently. A team MUST be able to run different roles on different agents (e.g. architect on one model, implementer on another) without any code changes.
- **FR-040**: If a performer role has no backend configured, the coordinare MUST treat the card as blocked with a clear message indicating which role is unconfigured, rather than silently skipping the stage or crashing.
- **FR-033** *(future — human feedback routing)*: The coordinare MUST implement a `handle_human_review_feedback` node that reads PR review comments from a human and classifies each comment into one of: implementer, architect, security, QA, tech writer, or assessor — then re-enters the lifecycle at the earliest affected role.
- **FR-034** *(future — human feedback routing)*: When a human review contains comments spanning multiple classification types, the coordinare MUST re-enter at the earliest role in the sequential lifecycle that covers all findings, so all issues are addressed in a single forward pass.
- **FR-035** *(future — human feedback routing)*: When comment classification is ambiguous, the coordinare MUST default to re-entering at the implementer and include the original comment verbatim in the dispatch payload.
- **FR-036** *(future — human feedback routing)*: Human PR approval MUST be detected by the coordinare and result in the card being marked done with no further performer dispatch.

### Key Entities

- **Score**: The dispatch payload received from the coordinare — contains the card's title, description, acceptance criteria, `repo_url`, `branch`, and `github_token`. Extended for reviewer dispatches to include `pr_url` and `pr_node_id`; for QA dispatches to include the full acceptance criteria list.
- **Stand**: The ephemeral local workspace created for a single performance — a cloned repository directory that exists only for the duration of that performance.
- **Performance**: A single end-to-end session from dispatch receipt through a terminal result, identified by a unique session ID returned on acceptance.
- **Backend**: The AI agent the performer delegates work to — the actual intelligence (e.g. opencode, Claude Code, Codex) that reads the codebase and produces changes or analysis. Each backend is implemented as an adapter (strategy) that encapsulates start, monitor, relay_feedback delivery, and stop — so the performer's protocol layer is unaffected by backend swaps.
- **Performer Role**: One of `architect`, `implementer`, `reviewer`, `security`, `QA`, or `tech writer` — determines the dispatch payload shape, the work performed, and the set of valid terminal responses. All roles share the same wire protocol and are executed strictly sequentially.
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
