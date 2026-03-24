# Feature Specification: Reviewer Performer

**Feature Branch**: `021-reviewer-performer`
**Created**: 2026-03-18
**Status**: Complete

## Overview

The reviewer performer is the fifth role in the sequential performer lifecycle (after advocate, assessor, architect, and implementer). It analyses the implementer's changes — reading the PR diff, the architecture plan committed by the architect, and the original acceptance criteria — then posts structured review comments directly to the GitHub Pull Request. It returns one of two terminal outcomes: `approved` (no blocking issues) or `changes_requested` (specific issues found). If changes are requested, the coordinare routes the reviewer's comments back to the implementer for resolution, and the reviewer re-runs after the implementer pushes new changes.

The reviewer acts as an automated code review gate before the feature reaches human eyes or proceeds to security and QA. Its feedback channel is distinct from the QA performer — reviewer concerns are about code correctness, adherence to the architecture plan, and implementation quality, not application runtime behaviour.

## Clarifications

### Session 2026-03-18

- Q: Does the reviewer open a new PR or comment on the existing one? → A: Comments on the existing PR opened by the implementer. It does not open a separate PR.
- Q: How does the reviewer signal completion? → A: Returns `approved` (terminal — advances lifecycle) or `changes_requested` (non-terminal — coordinare relays comments to the implementer and re-dispatches). Neither uses `pr_opened` — that was already done by the implementer.
- Q: How are reviewer comments delivered to the implementer? → A: Via `relay_feedback` in the coordinare's existing relay mechanism. The coordinare packages the reviewer's comment list as the feedback payload.
- Q: Can the reviewer approve with non-blocking suggestions? → A: Yes. The reviewer can include `suggestions` alongside an `approved` outcome. Suggestions are informational and do not block lifecycle advancement.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Approve Clean Implementation (Priority: P1)

When the implementer's changes fully satisfy the architecture plan and acceptance criteria, the reviewer approves the PR and the lifecycle advances to the security performer.

**Why this priority**: Approval is the happy-path outcome. The reviewer must be able to confirm clean work so the lifecycle can continue without human intervention.

**Independent Test**: Can be tested by dispatching the reviewer with a branch that satisfies all acceptance criteria, verifying it returns `approved` and posts a GitHub review with the `APPROVE` event.

**Acceptance Scenarios**:

1. **Given** the reviewer performer receives a dispatch with a PR whose diff satisfies the architecture plan and all acceptance criteria, **When** the AI backend completes its review, **Then** the performer returns `{"status": "approved"}` and the GitHub PR shows an approved review from the reviewer.
2. **Given** the reviewer returns `approved`, **When** the coordinare processes the result, **Then** `performer_stage` advances to the next configured role (security) and the card remains in "In Progress".
3. **Given** the reviewer finds non-blocking style suggestions alongside an overall approval, **When** it returns its response, **Then** it returns `{"status": "approved", "suggestions": ["..."]}` and posts the suggestions as review comments but does NOT block lifecycle advancement.

---

### User Story 2 — Request Changes on Flawed Implementation (Priority: P1)

When the implementer's changes have substantive issues — logic errors, missing acceptance criteria, architecture deviation — the reviewer requests changes with specific, actionable comments referencing file names and line numbers.

**Why this priority**: The reviewer's blocking capability is essential — without it, the automated lifecycle cannot self-correct implementation errors before human review.

**Independent Test**: Can be tested by dispatching the reviewer with a branch that intentionally violates an acceptance criterion, verifying it returns `changes_requested` with at least one specific comment.

**Acceptance Scenarios**:

1. **Given** the PR diff has code that violates an acceptance criterion, **When** the reviewer backend identifies the issue, **Then** the performer returns `{"status": "changes_requested", "comments": [{"file": "...", "line": ..., "body": "..."}]}` and posts a `REQUEST_CHANGES` GitHub review.
2. **Given** the reviewer returns `changes_requested`, **When** the coordinare processes the result, **Then** the comments are relayed to the implementer performer via `relay_feedback` and the implementer is re-dispatched.
3. **Given** the implementer pushes a fix and the reviewer is re-dispatched, **When** the updated diff satisfies the reviewer's concerns, **Then** the reviewer returns `approved` and the lifecycle advances.
4. **Given** the reviewer has requested changes and the implementer fails to resolve them after a configurable number of cycles, **When** the maximum retry limit is reached, **Then** the reviewer returns `blocked` with a summary of unresolved issues for human attention.

---

### User Story 3 — Architecture Deviation Detection (Priority: P2)

The reviewer compares the implementation against the architect's plan. If the implementation deviates from the agreed architecture without justification, the reviewer flags the deviation specifically, distinguishing it from a functional bug.

**Why this priority**: Architecture drift caught early (by the reviewer) is cheaper to fix than architecture drift discovered by a human or during security/QA.

**Independent Test**: Can be tested by dispatching the reviewer with a branch that implements a different data model than the one in the architecture plan, verifying the review comment specifically mentions the architecture deviation.

**Acceptance Scenarios**:

1. **Given** the implementation uses a different data schema than the architecture plan, **When** the reviewer detects the discrepancy, **Then** the review comment references the architecture plan explicitly (e.g., "Architecture plan specified X but implementation uses Y").
2. **Given** the architecture deviation is intentional (the implementer added a comment explaining the change), **When** the reviewer processes the explanation, **Then** it may still flag the deviation but includes the implementer's reasoning in its output.

---

### Edge Cases

- What if the PR has no diff (no changes committed by the implementer)?
- What if the architecture plan file is absent from the branch (architect was skipped)?
- What if the GitHub API call to post the review fails?
- What if the reviewer has been through the maximum review cycles and still finds issues — should it escalate to the architect or directly to human?
- What if the implementer pushes changes that introduce new issues while fixing the reviewer's original comments?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The reviewer performer MUST implement the full coordinare wire protocol: dispatch, status, relay_feedback, and health actions.
- **FR-002**: On a `dispatch` action, the reviewer MUST clone the repository, check out the feature branch, read the PR diff, and begin AI-driven review against the architecture plan and acceptance criteria.
- **FR-003**: The reviewer MUST post its review to the GitHub Pull Request using the GitHub Reviews API (`POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`).
- **FR-004**: If the review outcome is approval, the reviewer MUST submit the review with event type `APPROVE` and return `{"status": "approved"}`.
- **FR-005**: If the review outcome requires changes, the reviewer MUST submit the review with event type `REQUEST_CHANGES` and return `{"status": "changes_requested", "comments": [...]}`. Each comment MUST include at minimum: `file`, `line`, and `body`.
- **FR-006**: The reviewer MAY include a `suggestions` list in an `approved` response for non-blocking notes. These are posted as review comments with no blocking effect.
- **FR-007**: When the coordinare relays reviewer comments to the implementer via `relay_feedback`, the payload MUST preserve the full comment structure (file, line, body) so the implementer can address each comment precisely.
- **FR-008**: The reviewer MUST be re-dispatchable: after the implementer pushes new commits, the reviewer re-reads the updated diff and re-evaluates.
- **FR-009**: The reviewer MUST enforce a configurable maximum review cycle limit (env var `REVIEWER_MAX_CYCLES`, default: 3). When this limit is reached without resolution, the reviewer MUST return `{"status": "blocked", "questions": ["..."]}`.
- **FR-010**: The reviewer MUST read the architecture plan file from the branch (if present) and use it as a review reference. If the file is absent, the review proceeds based on acceptance criteria alone.
- **FR-011**: The reviewer performer MUST be independently configurable in `config.yaml` under `performers.reviewer`.

### Key Entities

- **Review**: The structured output of a reviewer session — outcome (approved / changes_requested), list of inline comments with file/line/body, and optional non-blocking suggestions.
- **approved**: Terminal success state specific to the reviewer role — no blocking issues found.
- **changes_requested**: Non-terminal outcome — the reviewer has found issues; the coordinare relays comments to the implementer and re-dispatches.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of reviewer sessions that return `changes_requested` result in at least one comment posted to the GitHub PR via the Reviews API.
- **SC-002**: 100% of reviewer sessions that return `approved` post an APPROVE review to the GitHub PR.
- **SC-003**: When the reviewer requests changes, the coordinare relays those comments to the implementer within one polling cycle.
- **SC-004**: The reviewer correctly identifies when an implementation resolves all previously raised comments and returns `approved` on re-dispatch in at least 90% of test cases with intentionally fixed implementations.
- **SC-005**: The reviewer never advances the lifecycle past `changes_requested` without having received an updated diff (i.e., it does not auto-approve stale code).

## Assumptions

- The implementer performer has already opened a PR on the feature branch before the reviewer is dispatched. The reviewer does not open its own PR.
- The reviewer uses the same `github_token` from the dispatch payload for all GitHub API calls.
- The coordinare's `classify_human_feedback` node handles human PR comments separately from the reviewer's automated review — the two are not conflated.
- Each review cycle is a new performer session (new dispatch). The reviewer does not maintain in-memory state between cycles — it re-reads the current diff each time.
