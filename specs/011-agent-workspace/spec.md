# Feature Specification: Agent Workspace Management

**Feature Branch**: `011-agent-workspace`
**Created**: 2026-03-03
**Status**: Draft

## Overview

Before coordinare can dispatch a card to an AI coding agent, the agent needs a local copy of the repository to work in. Today coordinare dispatches cards without providing any workspace context — the agent has no repo, no branch, and no credentials to push changes. This feature closes that gap by making coordinare responsible for preparing an isolated, ready-to-use workspace for each card and cleaning it up when the session ends.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Ready Workspace on Dispatch (Priority: P1)

When coordinare picks up a card and dispatches it to the agent, the agent receives a fully prepared workspace: the repository has been cloned into a private temporary directory, a unique branch has been created for this card's work, and git credentials are configured so the agent can push without any additional setup.

**Why this priority**: Without a workspace, the agent cannot do any work at all. Every other feature in the system depends on this.

**Independent Test**: Can be tested by dispatching a single card and verifying the agent receives a workspace path, branch name, and repo URL in the dispatch payload — and that the directory exists and contains the repository checked out on the named branch.

**Acceptance Scenarios**:

1. **Given** a card is ready to dispatch, **When** coordinare calls dispatch, **Then** the dispatch payload includes `workspace_path`, `repo_url`, and `branch`, the directory at `workspace_path` is a git clone of the repo checked out on the named branch, and git credentials are pre-configured in the workspace so the agent can push without additional setup.
2. **Given** two cards are dispatched concurrently, **When** each workspace is created, **Then** each card receives a distinct, isolated directory — neither workspace shares state with the other.
3. **Given** the repository clone fails (network error, invalid repo URL), **When** coordinare attempts to set up the workspace, **Then** the card is moved to BLOCKED with a clear explanation, and no partial directory is left behind.

---

### User Story 2 — Deterministic Branch Naming (Priority: P2)

The branch created for each card follows a consistent, human-readable naming pattern derived from the card's identifier and title. Re-running setup for the same card produces the same branch name, preventing duplicate branches and making history traceable.

**Why this priority**: Predictable branch names make it easy for human reviewers to understand which branch belongs to which card and allow idempotent re-dispatch if a session is retried.

**Independent Test**: Can be tested by running workspace setup for a known card and verifying the branch name matches the expected pattern derived from the card's ID and title.

**Acceptance Scenarios**:

1. **Given** a card with a known ID and title, **When** the workspace branch is created, **Then** the branch name is derived deterministically from those values (e.g., slugified title with card ID prefix).
2. **Given** a card title containing special characters or spaces, **When** the branch name is generated, **Then** the result is a valid git branch name with no illegal characters.
3. **Given** the same card is dispatched twice, **When** the branch name is computed, **Then** both runs produce the identical branch name.

---

### User Story 3 — Workspace Cleanup After Session Ends (Priority: P3)

When a card's agent session ends — whether it succeeded, errored, or timed out — coordinare removes the temporary workspace directory. No orphaned directories accumulate on the host over time.

**Why this priority**: Without cleanup, repeated card dispatch would eventually exhaust disk space on the coordinare host.

**Independent Test**: Can be tested by dispatching a card, confirming the workspace directory exists, then simulating session end and verifying the directory no longer exists.

**Acceptance Scenarios**:

1. **Given** a session completes successfully (PR opened), **When** coordinare processes the result, **Then** the workspace directory is removed.
2. **Given** a session ends with an error or timeout, **When** coordinare handles the failure, **Then** the workspace directory is still removed.
3. **Given** cleanup fails (e.g., directory already removed by external process), **When** the error occurs, **Then** coordinare logs a warning and continues — cleanup failure must never block the main workflow.

---

### Edge Cases

- What happens when disk space is insufficient to clone the repository?
- What if the card title is empty or contains only special characters, producing an otherwise invalid branch name?
- What if a branch with the computed name already exists on the remote (e.g., from a previous partial run)?
- What if the coordinare process crashes mid-dispatch before cleanup runs — are orphaned directories recovered on next start?
- What if the repository is very large and the clone takes longer than the dispatch timeout?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Coordinare MUST clone the target repository into an isolated temporary directory before dispatching each card.
- **FR-002**: Coordinare MUST create a new git branch in the cloned workspace, named deterministically from the card ID and title, and check it out before dispatch.
- **FR-003**: Coordinare MUST configure git credentials in the workspace so the agent can push without manual credential entry, reusing the GitHub token already present in coordinare's configuration — no new credential types.
- **FR-004**: The dispatch message MUST include `repo_url` and `branch` for all transport types. For subprocess and SSH transports, `workspace_path` MUST also be included, pointing to the cloned directory on the coordinare host. For the Kubernetes transport, `workspace_path` is omitted — the performer container handles its own workspace setup using credentials supplied separately (e.g., via Kubernetes Secrets managed by the operator).
- **FR-005**: Coordinare MUST remove the workspace directory after the agent session ends, regardless of session outcome (success, error, or timeout).
- **FR-006**: Coordinare MUST move a card to BLOCKED and skip dispatch if workspace setup fails, with a human-readable explanation of the failure included in the card's open questions.
- **FR-007**: Branch names MUST be valid git branch names, derived deterministically from the card ID and a slug of the card title, and consistent across repeated calls for the same card.
- **FR-008**: The repository URL MUST be constructed from the `github_org` and `project_name` values already in coordinare's configuration — no new configuration fields are required for the URL.
- **FR-009**: Workspace setup and teardown MUST occur within the existing dispatch and post-session workflow nodes — no new top-level workflow phases are introduced.
- **FR-010**: Each dispatched card MUST receive its own isolated workspace directory; concurrent sessions MUST NOT share a workspace.
- **FR-011**: When coordinare is configured to use the Kubernetes transport, coordinare MUST read a `performer_image` configuration field specifying the container image to schedule as a Kubernetes Job for each card. This field is not required for subprocess or SSH transports, which use the local environment directly.
- **FR-012**: Coordinare MUST support an optional `workspace_root` configuration field specifying the parent directory under which all workspace directories are created. When set, all workspace directories MUST be created as subdirectories of `workspace_root`. When unset, coordinare MUST fall back to the system temporary directory. This allows operators to point workspace storage at a mounted volume (e.g., a PVC in Kubernetes) rather than ephemeral container or host memory.

### Key Entities

- **Workspace**: A temporary directory containing a full git clone of the target repository, checked out on a card-specific branch, with git credentials configured. Exists only for the duration of an agent session.
- **Branch Name**: A deterministic, git-safe string derived from the card ID and title slug. Links a git branch to its originating card in a human-readable way.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of successfully dispatched cards receive a workspace with a valid git clone, correct branch checkout, and working push credentials.
- **SC-002**: Branch names are 100% deterministic — the same card ID and title always produce the same branch name across runs and environments.
- **SC-003**: All workspace directories are removed within 5 seconds of session end under normal operating conditions.
- **SC-004**: Workspace setup failures produce a BLOCKED card with a human-readable explanation in 100% of cases — no silent failures or unhandled exceptions escape to the operator.
- **SC-005**: No orphaned workspace directories remain on the host after coordinare has been running for 24 hours under normal load.
- **SC-006**: Workspace setup (clone, branch creation, and credential configuration) completes within 120 seconds on a standard network connection for repositories up to 1 GB; failures exceeding this threshold result in a BLOCKED card with a human-readable explanation.

## Assumptions

- The target repository is a GitHub-hosted git repository accessible via HTTPS using the existing `github_token`.
- The coordinare host has `git` installed and available in `PATH`.
- Disk space on the coordinare host is sufficient for at least one full repository clone per concurrent card in flight.
- `project_name` in coordinare config matches the GitHub repository name exactly (repo URL: `https://github.com/{github_org}/{project_name}`).
- If a branch with the computed name already exists on the remote (e.g., from a crashed previous run), the implementation may either reuse it or fail to BLOCKED — the spec does not require a specific conflict resolution strategy beyond not silently corrupting existing work.
- Workspace cleanup is best-effort on crash recovery; coordinare does not guarantee cleanup of directories created before an unexpected process termination (manual cleanup may be needed after a crash).
- For the Kubernetes transport, the performer container is responsible for its own workspace (clone, branch, credentials) — coordinare does not clone on the host side. FR-001 through FR-003 and FR-005 apply only to subprocess and SSH transports.
- The `performer_image` config field is only consulted when `agent_transport` is `kubernetes`; its presence or absence has no effect on other transport modes.
- When `workspace_root` is set to a PVC mount path in Kubernetes, the volume must be accessible to the coordinare process (not the performer container — for subprocess/SSH transports, coordinare writes to it directly).
