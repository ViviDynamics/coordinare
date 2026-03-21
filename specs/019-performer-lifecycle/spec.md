# Feature Specification: Performer Lifecycle

**Feature Branch**: `019-performer-lifecycle`
**Created**: 2026-03-18
**Status**: Draft

## Overview

The coordinare today dispatches a single hardcoded implementer performer and monitors it to completion. This feature refactors the coordinare graph to support a sequential multi-role performer lifecycle, where a card progresses through a defined sequence of performer roles before reaching human review. Each role is a distinct performer (advocate, assessor, architect, implementer, reviewer, security, QA, tech writer), and the coordinare advances the card to the next role automatically on success. Human feedback from the PR is classified and routed back to the earliest affected performer role.

The six GitHub board columns remain fixed and map as follows:
- **Backlog** → card waiting for the advocate scan
- **TODO** → card assessed and ready for dispatch
- **In Progress** → card is being worked on by any performer role (all internal stages show here)
- **Blocked** → performer is blocked and waiting for human input
- **In Review** → card is in human PR review (after the last automated role completes)
- **Done** → PR merged

## Clarifications

### Session 2026-03-18

- Q: Should roles execute sequentially or in parallel? → A: Strictly sequentially, one role at a time, to minimise operational complexity of the feedback cycle.
- Q: How many board columns? → A: Six fixed columns: Backlog, TODO, In Progress, Blocked, In Review, Done. All internal performer stages map to "In Progress".
- Q: How is human PR feedback routed? → A: A `classify_human_feedback` coordinare node reads PR comments, classifies them by concern (implementation, architecture, security, documentation, etc.), and re-enters the lifecycle at the earliest affected performer role, resetting `performer_stage` accordingly.
- Q: Can different roles use different AI backends? → A: Yes. Each role's backend agent is declared independently in `config.yaml`. No AI provider is hardcoded.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Sequential Role Advancement (Priority: P1)

When a card is dispatched, the coordinare runs each performer role in sequence (implement → review → security → QA → tech writer by default). On each role's success, the card automatically advances to the next role without human intervention. After the final role completes, the card moves to "In Review" for a human reviewer on GitHub.

**Why this priority**: This is the core orchestration capability. Without it, the multi-role lifecycle cannot function at all.

**Independent Test**: Can be tested by dispatching a card with a stub performer service for each role (returning immediate success), verifying that `performer_stage` advances through all configured stages and the card ends in "In Review" state with a PR open.

**Acceptance Scenarios**:

1. **Given** a card is in TODO and all performer services are configured, **When** the coordinare dispatches the first role, **Then** `performer_stage` is set to the first stage and the card moves to "In Progress".
2. **Given** the current performer role returns `pr_opened`, **When** the coordinare processes the success, **Then** `performer_stage` advances to the next configured role and that role's performer is dispatched.
3. **Given** the final performer role (tech writer) returns `pr_opened`, **When** the coordinare processes the success, **Then** the card moves to "In Review" and no further performer dispatch occurs.
4. **Given** any performer role returns `error`, **When** the coordinare processes the failure, **Then** the card moves to "Blocked" and `performer_stage` does not advance.

---

### User Story 2 — Shared Dispatch and Monitor Nodes (Priority: P1)

The coordinare graph uses a single `dispatch_performer` node and a single `monitor_performer` node for all roles, resolving the correct `AgentService` at runtime from a `performer_services` registry. Adding a new performer role requires no new graph nodes — only a new entry in the registry and config.

**Why this priority**: Without shared nodes, adding each new performer role requires duplicating graph infrastructure, violating the single-responsibility principle and making the graph unmaintainable.

**Independent Test**: Can be tested by verifying that `dispatch_performer` dispatches to the correct service based on the current `performer_stage`, and that `monitor_performer` polls the correct service on each invocation.

**Acceptance Scenarios**:

1. **Given** `performer_stage = "implementing"` and an implementer service registered, **When** `dispatch_performer` runs, **Then** it dispatches to the implementer's `AgentService` and not any other service.
2. **Given** `performer_stage = "reviewing"` and a reviewer service registered, **When** `monitor_performer` runs, **Then** it polls the reviewer's `AgentService`.
3. **Given** a new performer role is added to `performer_services` and `config.yaml`, **When** the coordinare starts, **Then** the new role is available for dispatch without any graph code changes.

---

### User Story 3 — Human Feedback Classification and Rerouting (Priority: P2)

When a human posts comments on the GitHub PR, the coordinare classifies those comments and re-enters the lifecycle at the appropriate performer role. Implementation concerns route to the implementer, architecture concerns to the architect, security concerns to the security performer, and documentation concerns to the tech writer.

**Why this priority**: Without feedback routing, human input on the PR is lost and the automated lifecycle cannot self-correct based on reviewer guidance.

**Independent Test**: Can be tested by posting a PR comment with a known concern keyword and verifying that `performer_stage` is reset to the expected role and a new performer dispatch occurs.

**Acceptance Scenarios**:

1. **Given** a PR in "In Review" has a human comment about a code bug, **When** `classify_human_feedback` runs, **Then** `performer_stage` is set to `"implementing"` and the implementer is dispatched.
2. **Given** a PR in "In Review" has a human comment about architecture, **When** `classify_human_feedback` runs, **Then** `performer_stage` is set to `"architecting"` and the architect is dispatched.
3. **Given** a PR in "In Review" has a human comment about missing documentation, **When** `classify_human_feedback` runs, **Then** `performer_stage` is set to `"documenting"` and the tech writer is dispatched.
4. **Given** a PR in "In Review" is approved by the human reviewer, **When** the coordinare detects the approval, **Then** the card moves to "Done" without re-entering any performer role.

---

### User Story 4 — Per-Role Backend Configuration (Priority: P2)

Each performer role's backend agent is declared independently in `config.yaml`. Operators can assign different AI providers, models, or endpoints to different roles without changing any code. If a role is not configured, it is skipped in the lifecycle.

**Why this priority**: Avoids vendor lock-in and allows operators to optimise cost, speed, and capability per role (e.g., a cheaper model for documentation, a stronger model for security review).

**Independent Test**: Can be tested by configuring two roles with different backend settings in `config.yaml`, dispatching a card, and verifying each role's performer uses the backend specified for it.

**Acceptance Scenarios**:

1. **Given** `config.yaml` specifies `backend: opencode` for the implementer and `backend: claude-code` for the reviewer, **When** the coordinare dispatches each role, **Then** the correct backend is used for each.
2. **Given** a role is absent from `config.yaml`, **When** the coordinare reaches that stage in the lifecycle, **Then** it skips that stage and advances to the next configured role.
3. **Given** `config.yaml` has no performer role configuration at all, **When** the coordinare tries to dispatch, **Then** it returns an error explaining that no performer roles are configured.

---

### Edge Cases

- What happens if the `performer_services` registry has no entry for the current `performer_stage`?
- What if `classify_human_feedback` cannot determine which role a PR comment targets?
- What if a performer role's service is unreachable (health check fails) when the coordinare tries to dispatch to it?
- What if the PR is closed by the human without merging — does the card return to TODO?
- What if two human comments conflict in their routing classification?
- What if a performer role opens a PR and the next role also needs to push to the same branch?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The coordinare MUST maintain a `performer_stage` field in `CoordinareState` that tracks which role is currently active. Valid values are: `advocate`, `assessing`, `architecting`, `implementing`, `reviewing`, `security`, `qa`, `documenting`.
- **FR-002**: The coordinare MUST maintain a `performer_services` registry in `CoordinareState` mapping each role name to its configured `AgentService` instance.
- **FR-003**: A single `dispatch_performer` graph node MUST dispatch to the `AgentService` resolved from `performer_services[performer_stage]`. It MUST NOT contain role-specific logic.
- **FR-004**: A single `monitor_performer` graph node MUST poll the `AgentService` resolved from `performer_services[performer_stage]`. It MUST NOT contain role-specific logic.
- **FR-005**: The lifecycle MUST advance `performer_stage` to the next configured role automatically when the current role reports `pr_opened` or an equivalent success state.
- **FR-006**: The lifecycle MUST stop advancing and set `phase = "blocked"` if any performer role reports `error` or `blocked`.
- **FR-007**: After the final configured role succeeds, the coordinare MUST move the card to "In Review" (GitHub board column) and set `phase = "monitoring_pr"`.
- **FR-008**: The coordinare MUST include a `classify_human_feedback` node that reads PR comments, classifies each comment by concern type, and resets `performer_stage` to the earliest affected role.
- **FR-009**: `classify_human_feedback` MUST support at minimum four classification categories: `implementation` (→ implementing), `architecture` (→ architecting), `security` (→ security), `documentation` (→ documenting).
- **FR-010**: When `classify_human_feedback` routes back to a performer role, the coordinare MUST dispatch that role's performer with the PR comments included in the payload as `relay_feedback`.
- **FR-011**: When a human approves the PR on GitHub, `classify_human_feedback` MUST detect the approval and advance the card to "Done" without re-entering any performer role.
- **FR-012**: Each performer role's backend MUST be independently configurable in `config.yaml` under a `performers` key, specifying at minimum: `backend`, `transport`, and optional `image`.
- **FR-013**: If a role is absent from `config.yaml`, the coordinare MUST skip that role during lifecycle advancement.
- **FR-014**: If no performer roles are configured, the coordinare MUST surface a startup error and refuse to dispatch any card.
- **FR-015**: The existing `monitor_agent` and `dispatch_card` nodes MUST be replaced by `dispatch_performer` and `monitor_performer` to eliminate hardcoded implementer logic. Backward compatibility with the existing implementer-only config MUST be maintained through a migration path or config default.

### Key Entities

- **PerformerStage**: The current role active in the lifecycle. One of: `advocate`, `assessing`, `architecting`, `implementing`, `reviewing`, `security`, `qa`, `documenting`.
- **PerformerServices**: A mapping from role name → `AgentService` instance, built at startup from `config.yaml`.
- **LifecycleSequence**: The ordered list of roles that will be executed for a given card, derived from which roles are present in `config.yaml`.
- **HumanFeedbackClassification**: The result of analysing a PR comment — a concern category and the target performer role it should route to.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A card with four configured performer roles (implementer, reviewer, security, QA) passes through all four roles and lands in "In Review" with no human intervention, in a single coordinare run.
- **SC-002**: Adding a new performer role to `config.yaml` requires zero graph code changes — only configuration.
- **SC-003**: A PR comment classified as an implementation concern routes back to the implementer role within one coordinare polling cycle.
- **SC-004**: The board column shown to a human is always "In Progress" while any performer role is active — the human never sees intermediate stage names.
- **SC-005**: A PR approval by a human advances the card to "Done" within one coordinare polling cycle, without re-dispatching any performer role.
- **SC-006**: Removing a role from `config.yaml` causes that role to be silently skipped in the lifecycle — no error, no graph change required.

## Assumptions

- The wire protocol (JSON stdin/stdout, dispatch/status/relay_feedback/health actions) is shared by all performer roles — the protocol layer is not role-specific.
- All performer roles push to the same branch. Roles that produce incremental changes (reviewer, QA) push new commits rather than opening separate PRs.
- The `classify_human_feedback` node classifies PR comments by concern type. V1 uses keyword heuristics for fast, deterministic routing; a future version may integrate AI-powered classification for ambiguous comments.
- The `performer_services` registry is built once at coordinare startup from `config.yaml` and does not require a restart to reflect config file changes. Hot-reload is out of scope.
- "Skip role" means the lifecycle advances as if that role returned success immediately — it does not insert a placeholder or log a warning to the board.
