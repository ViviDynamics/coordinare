# Feature Specification: Post-PR Workflow Bug Fixes

**Feature Branch**: `017-fix-post-pr-workflow`
**Created**: 2026-03-16
**Status**: Draft

## Overview

Four confirmed bugs in the coordinare's post-PR workflow cause a broken cycle: the performer opens a pull request, but the orchestrator loops back, re-dispatches a new performer, force-pushes over the existing PR, and eventually triggers a GitHub 422 error. Questions asked in earlier cycles are lost, and workspace context is sometimes missing entirely. These bugs are confirmed via production issues on ViviDynamics/website (#71, #77).

## User Scenarios & Testing

### User Story 1 — PR Opened: Card Moves to IN_REVIEW Without Re-dispatch (Priority: P1)

A developer is monitoring the coordinare dashboard. After a performer opens a pull request and reports `pr_opened`, the coordinare moves the card to the IN_REVIEW column on the GitHub project board and begins monitoring the PR. No second performer is dispatched and no duplicate PR is created.

**Why this priority**: This is the root cause of the double-dispatch loop. Fixing it stops the cascade of bugs 2, 3, and 422 errors.

**Independent Test**: Run the coordinare against a real or mocked GitHub board. Trigger a performer that returns `pr_opened` with a valid `pr_url` and `pr_node_id`. Confirm the card column on GitHub is updated to IN_REVIEW and the coordinare enters `monitoring_pr` phase without re-dispatching.

**Acceptance Scenarios**:

1. **Given** a card is in IN_PROGRESS and a performer reports `pr_opened` with a valid PR URL and PR node ID, **When** `monitor_agent` processes the result, **Then** the GitHub board column for that card is updated to IN_REVIEW.
2. **Given** the card column has been updated to IN_REVIEW, **When** the next `check_board` cycle runs, **Then** the coordinare routes to `monitoring_pr`, not back to `monitor_agent`.
3. **Given** a card has been moved to IN_REVIEW, **When** a new `check_board` cycle completes, **Then** no new performer session is started for that card.

---

### User Story 2 — Session Expiry With Existing PR Routes to Monitoring, Not Re-dispatch (Priority: P2)

A developer sees that a performer session has expired after successfully opening a PR. The coordinare recognises the open PR from state, skips the TODO reset, and resumes monitoring the existing PR.

**Why this priority**: This is the fallback safety net when Bug 1 occurs and the card is not yet in IN_REVIEW. It prevents re-dispatch even in the degraded state.

**Independent Test**: Simulate `session_expired` being received in `monitor_agent` when `current_card.pr_node_id` is already set. Confirm the coordinare transitions to `monitoring_pr` instead of resetting the card to TODO.

**Acceptance Scenarios**:

1. **Given** `monitor_agent` receives `session_expired` and `current_card.pr_node_id` is set, **When** the handler processes the event, **Then** the coordinare transitions to `monitoring_pr` phase without resetting the card column.
2. **Given** `monitor_agent` receives `session_expired` and `current_card.pr_node_id` is NOT set, **When** the handler processes the event, **Then** the card is reset to TODO as before (existing behaviour preserved).

---

### User Story 3 — Open Questions Preserved Across Session Expiry (Priority: P3)

A developer raises a question during assessment. If the performer session expires before the question is answered, the question is not lost — it is preserved in `card_clarifications` and visible to the next performer.

**Why this priority**: Secondary bug whose primary cause is fixed by US1 and US2. Still needs an independent fix for the edge case where session expiry happens before the question is answered.

**Independent Test**: Trigger a session expiry while `open_questions` is non-empty. Confirm `card_clarifications` contains an entry recording the unanswered question. Confirm `open_questions` is cleared after saving.

**Acceptance Scenarios**:

1. **Given** `monitor_agent` receives `session_expired` and `open_questions` is non-empty, **When** the handler processes the event, **Then** the questions are saved to `card_clarifications` before `open_questions` is cleared.
2. **Given** a new performer is dispatched for the same card, **When** `assess_card` builds the dispatch payload, **Then** the previously unanswered questions from `card_clarifications` are included.

---

### User Story 4 — Workspace Setup Failure Blocks Dispatch With Clear Error (Priority: P2)

A developer is watching the coordinare. When workspace setup fails (e.g., git clone fails, token missing), the card is moved to BLOCKED with a human-readable error message instead of proceeding to dispatch with an empty or incomplete workspace payload.

**Why this priority**: Ranked equal to US2 — workspace failures cause performer to run in wrong directory and ask confusing questions. Fail-fast is safer than silent degraded dispatch.

**Independent Test**: Trigger a workspace setup failure (mock the clone to raise). Confirm the card is moved to BLOCKED and the dispatch never fires. Confirm the block reason is recorded in state.

**Acceptance Scenarios**:

1. **Given** workspace setup raises an exception, **When** `dispatch_card` attempts to set up the workspace, **Then** the card is moved to BLOCKED phase with the error reason and no performer session is started.
2. **Given** workspace setup returns no result, **When** `dispatch_card` validates preconditions, **Then** the card is moved to BLOCKED and dispatch is aborted.
3. **Given** a card is in BLOCKED state due to workspace failure, **When** the `handle_blocked` node runs, **Then** the error reason is included in the block notification to the human reviewer.

---

### User Story 5 — PR CI Checks Pass Before Moving to IN_REVIEW (Priority: P2)

A developer expects that when a PR is submitted for human review, all automated CI checks have already passed. After the performer opens a pull request, it waits for all required CI checks to complete. If any check fails, the performer analyses the failure output and pushes a corrective commit before re-polling. Only when all checks report success does the performer report `pr_opened` to the coordinare, signalling that the card is ready for human review.

**Why this priority**: This prevents humans from being asked to review PRs that are known to be broken, improving review throughput and reducing back-and-forth noise.

**Independent Test**: Configure a performer session where the backend finishes, a PR is opened, and the GitHub check-run API reports a failing check. Confirm the performer does NOT report `pr_opened` immediately. Instead, it relays the failure details to the backend, waits for the backend to apply a fix, re-pushes, re-polls checks. Once checks pass, confirm `pr_opened` is returned. Confirm a configurable retry limit prevents infinite loops.

**Acceptance Scenarios**:

1. **Given** the backend finishes and a PR is opened, **When** all check runs complete with `success` or `neutral` conclusion, **Then** the performer reports `pr_opened` to the coordinare.
2. **Given** the backend finishes and a PR is opened, **When** one or more check runs report `failure`, **Then** the performer relays the failure details back to the backend without reporting `pr_opened`.
3. **Given** check run failures have been relayed and the backend applies a fix and finishes again, **When** the performer pushes the new commit and re-polls checks, **Then** the polling cycle restarts for the updated commit.
4. **Given** check failures have recurred for `CHECK_MAX_ATTEMPTS` consecutive fix attempts, **When** the performer reaches the retry limit, **Then** it reports `blocked` to the coordinare with a human-readable reason listing the failing checks.
5. **Given** the check runs API returns only `queued` or `in_progress` runs with no failures, **When** the performer polls, **Then** it reports `working` (checks still pending) so the coordinare polls again on the next cycle.
6. **Given** no check runs are configured for the repository, **When** the performer polls and receives an empty check-run list, **Then** it reports `pr_opened` immediately (no required checks to wait for).

---

### Edge Cases

- What happens when `pr_opened` is received but `pr_url` or `pr_node_id` is missing from the payload? (Treat as error; do not move card to IN_REVIEW.)
- What happens when `github.move_card()` fails after `pr_opened`? (Log error; do not re-dispatch performer; retry on next cycle.)
- What happens when workspace clone succeeds but `github_token` is missing from the workspace context? (Treat as workspace failure and block the card.)
- What happens when `session_expired` is received, `pr_node_id` is set, but the PR has since been closed on GitHub? (Out of scope; existing `monitor_pr` logic handles stale PRs.)
- What happens when the GitHub Check Runs API is unavailable while polling? (Treat as a transient error; return `working` so the coordinare retries on the next cycle; do not count against the fix attempt budget.)
- What happens when a check run reports `action_required` or `cancelled`? (Treat as `failure` and relay details to the backend for analysis.)

## Requirements

### Functional Requirements

- **FR-001**: When a performer reports `pr_opened` with a valid `pr_url` and `pr_node_id`, the system MUST call the GitHub card move operation to update the card column to IN_REVIEW before transitioning to `monitoring_pr` phase.
- **FR-002**: The system MUST persist `pr_url` and `pr_node_id` to coordinare state when `pr_opened` is received.
- **FR-003**: When `session_expired` is received and the current card already has a known open PR, the system MUST transition to `monitoring_pr` instead of resetting the card to TODO.
- **FR-004**: When `session_expired` is received and there are unanswered open questions in state, the system MUST save those questions to the card's clarification history before clearing the open questions list.
- **FR-005**: When workspace setup fails or returns incomplete context, the system MUST move the card to BLOCKED phase with a descriptive reason and MUST NOT dispatch a performer session.
- **FR-006**: The dispatch payload sent to a performer MUST always include repository URL, target branch, and authentication token; if any of these is absent the dispatch MUST be aborted. Workspace path is optional — the Kubernetes transport legitimately omits it when the performer manages its own workspace.
- **FR-007**: All state transitions introduced by this fix MUST be persisted via the state store so they survive coordinare restarts.
- **FR-008**: After opening a pull request, the performer MUST poll the GitHub Check Runs API for the PR's head commit before reporting `pr_opened`. If all check runs complete with `success` or `neutral` conclusion (or no check runs are configured), the performer MAY report `pr_opened`. If any check run has not yet completed, the performer MUST report `working` so the coordinare retries on the next poll cycle.
- **FR-009**: If one or more check runs complete with a failing conclusion (`failure`, `timed_out`, `cancelled`, or `action_required`), the performer MUST relay the failure details (check name and failure output) to the running backend so it can apply a corrective fix, then push the updated commit and restart the check-polling cycle.
- **FR-010**: The performer MUST enforce a configurable maximum number of check-fix attempts (`CHECK_MAX_ATTEMPTS`, default 3). If the retry limit is reached, the performer MUST report `blocked` with a human-readable reason listing the failing checks instead of continuing to loop.

### Key Entities

- **CoordinareState**: The shared mutable state passed between graph nodes. Relevant fields: `phase`, `current_card` (includes open PR identifier, PR URL, clarifications, board status), `open_questions`, `card_clarifications`, `agent_session_id`.
- **WorkspaceInfo**: Immutable value object produced by the workspace manager. Fields include workspace path, repository URL, target branch, and authentication token. A missing or incomplete `WorkspaceInfo` is a blocking error that prevents dispatch.

## Success Criteria

### Measurable Outcomes

- **SC-001**: After a performer reports `pr_opened`, the GitHub project board column for that card shows IN_REVIEW within one poll cycle, with zero additional performer sessions started.
- **SC-002**: A coordinare run against the website project completes a card end-to-end (TODO → IN_PROGRESS → IN_REVIEW) without triggering a GitHub 422 duplicate-PR error.
- **SC-003**: When a performer session expires after a PR has been opened, zero new performer sessions are started for that card; the coordinare resumes PR monitoring within one poll cycle.
- **SC-004**: Questions raised before a session expiry are included in the next performer dispatch payload; no questions are silently dropped.
- **SC-005**: When workspace setup fails, the card appears in BLOCKED state with a human-readable reason within one poll cycle; no performer process is started.
- **SC-006**: The changes introduced by this fix do not increase per-cycle processing time beyond the existing poll interval. Adding one `move_card` call to the `pr_opened` path is the only new network operation; all other changes are in-memory dict operations with negligible overhead.
- **SC-007**: A PR is only moved to IN_REVIEW (human review requested) after all configured CI checks report `success` or `neutral` on the PR's head commit; zero PRs with failing checks are submitted for review.
- **SC-008**: If CI checks cannot be fixed within `CHECK_MAX_ATTEMPTS` attempts, the card appears in BLOCKED state with a reason listing the failing check names within one poll cycle after the limit is reached.

## Assumptions

- The existing `monitoring_pr` node correctly handles post-review PR state (merging, closing); no changes are needed there.
- The GitHub Check Runs API (`GET /repos/{owner}/{repo}/commits/{ref}/check-runs`) is available and accessible with the same `github_token` already in the workspace context.
- `CHECK_MAX_ATTEMPTS` defaults to 3 and can be overridden via environment variable.
- Only the performer is changed for US5; the coordinare wire protocol and coordinare graph nodes are unchanged.
- The GitHub card move operation is already implemented and tested; this fix only adds the missing call site in `monitor_agent`.
- PR auto-close by GitHub automation is out of scope.
- Advocate scanning behaviour and notification routing are out of scope.
- The workspace manager's public contract (returning workspace info or raising on failure) is stable; this fix adds a guard at the call site, not inside the workspace manager.
