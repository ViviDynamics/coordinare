# Feature Specification: No Daemon Clone for Self-Cloning Performers

**Feature Branch**: `107-no-daemon-clone`
**Created**: 2026-10-13
**Status**: Draft
**Input**: Issue #107, from the audit in spec 555: "Daemon clones the target repo onto its own filesystem at dispatch (`WorkspaceManager.prepare`)"

## Overview

Coordinare is an orchestrator. It decides when work happens and which performer does it; performers do the work on the code base. At every dispatch (except on the Kubernetes transport) the daemon cloned the target repository onto its own filesystem. For containerized performers that clone served nothing: the container clones for itself, because the dispatch payload carries only the repository URL and the branch.

After spec 555 and issue #106, two things in the daemon still read that clone, plus one that works in it:

1. the subprocess transport, where the performer is a child of the daemon and works in the daemon's clone;
2. the spec 083 scanner floor, which runs semgrep and bandit over the clone when a security role is on the prose path (no `workflow: security`), and fails closed when there is no clone;
3. the persona scope classifier, which reads the head of `CLAUDE.md` and `AGENTS.md` from it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Container deployments stop cloning in the daemon (Priority: P1)

An operator runs performers in containers. When a card is dispatched, the daemon resolves the branch and the credential and hands them to the performer. It does not clone the repository.

**Why this priority**: this is the boundary the audit found crossed on every dispatch.

**Independent Test**: dispatch a card to a containerized performer for a non-security stage and confirm the daemon ran no git command and created no clone directory, while the performer still received the branch, repository URL and credential.

**Acceptance Scenarios**:

1. **Given** a containerized performer and an implementer, reviewer, QA, documenter or closer stage, **When** the card is dispatched, **Then** the daemon makes no clone and the dispatch carries the branch, URL and credential.
2. **Given** a security role configured with `workflow: security`, **When** it is dispatched to a containerized performer, **Then** the daemon makes no clone.
3. **Given** the Kubernetes transport, **When** any card is dispatched, **Then** behavior is unchanged (it already made no clone).

---

### User Story 2 - Nothing that needed the clone silently breaks (Priority: P1)

The two remaining daemon-side users keep working.

**Why this priority**: the scanner floor fails closed without a clone, so skipping the clone for it would turn every prose-path security dispatch into a "scanner unavailable" finding.

**Independent Test**: dispatch with the subprocess transport, and separately dispatch a prose-path security role to a containerized performer; confirm both still get a daemon clone.

**Acceptance Scenarios**:

1. **Given** the subprocess transport, **When** any card is dispatched, **Then** the daemon clones as before.
2. **Given** a security role without `workflow: security` on a containerized performer, **When** it is dispatched, **Then** the daemon clones as before and logs a warning naming `workflow: security` as the way to move the scan into the performer.
3. **Given** a workspace manager or performer service that predates this feature, **When** a card is dispatched, **Then** the daemon clones as before.

---

### User Story 3 - The scope classifier keeps its project context (Priority: P2)

When there is no daemon clone, the classifier reads the same two files through the hosting platform's read interface, at the PR head.

**Why this priority**: without it, container deployments would lose classifier context they have today. With it, Kubernetes deployments gain context they never had.

**Independent Test**: classify a card with no local workspace and confirm the two files are read through the platform, truncated to the same length, and that any failure yields empty context rather than an error.

**Acceptance Scenarios**:

1. **Given** no daemon clone, **When** the classifier runs, **Then** it reads `CLAUDE.md` and `AGENTS.md` at the PR head through the platform.
2. **Given** the platform read fails or a file is missing, **When** the classifier runs, **Then** it proceeds with empty project context.

### Edge Cases

- A performer service wrapped by the resilience layer: the "clones for itself" property is found through the wrapper.
- A test double or third-party service that answers every attribute: only an explicit true value counts, so it keeps the clone.
- The clone used to double as an early credential and reachability check: a failure blocked the card at dispatch, before any performer started. Without it, a bad credential or missing repository surfaces from the performer as a failed job after the card has moved to in-progress, and is handled by the monitor's ordinary error path. This is how the Kubernetes transport has always behaved.
- The PR head is unknown: the classifier reads the default branch.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The daemon MUST NOT clone the target repository for a dispatch when the performer clones for itself and no daemon-side consumer needs the tree.
- **FR-002**: The daemon MUST still resolve and hand over the branch, repository URL and credential in that case.
- **FR-003**: The daemon MUST keep cloning for the subprocess transport.
- **FR-004**: The daemon MUST keep cloning for a security stage whose role does not run the security workflow, and MUST log a warning that names the alternative.
- **FR-005**: Only a performer service that explicitly declares that it clones for itself is treated as such. Anything else keeps the clone.
- **FR-006**: A workspace manager that does not accept the new option MUST be called exactly as before.
- **FR-007**: With no daemon clone, the scope classifier MUST read its project context through the platform's read interface, best effort, with the same size limit.
- **FR-008**: No configuration key is added and no persisted state changes.

### Key Entities

- **Self-cloning performer**: a performer service that declares it clones the repository itself.
- **Daemon clone decision**: per dispatch, whether anything on the daemon side needs the tree.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For containerized performers, daemon-side clones per dispatch drop to zero for every stage except a prose-path security stage.
- **SC-002**: Zero prose-path security dispatches report "scanner unavailable" because of this change.
- **SC-003**: Subprocess-transport deployments see no behavior change.
- **SC-004**: Dispatch latency for containerized performers no longer includes a repository clone.

## Assumptions

- The daemon-side scanner floor itself is left in place. Moving it into the performer is what `workflow: security` (spec 170) already does; retiring the prose-path floor is a separate decision and is tracked on issue #107.
- Static scanning (semgrep, bandit) reads the tree and does not execute it.
