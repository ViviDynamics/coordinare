# Feature Specification: Architect Performer

**Feature Branch**: `020-architect-performer`
**Created**: 2026-03-18
**Status**: Draft

## Overview

The architect performer is the third role in the sequential performer lifecycle (after advocate and assessor). It receives the card's full context — title, description, acceptance criteria, and clarifications — analyses the existing codebase, and produces a structured technical plan committed directly to the feature branch. Downstream performer roles (implementer, reviewer, etc.) receive this plan as additional context in their dispatch payloads, ensuring implementation decisions are grounded in an explicit architecture review rather than implicit AI inference.

The architect performer follows the same wire protocol as all other performers (JSON stdin/stdout, dispatch/status/relay_feedback/health) and is deployed as a container image with the same base image as the general performer.

## Clarifications

### Session 2026-03-18

- Q: Where is the architecture plan stored? → A: As a committed file on the feature branch (e.g., `docs/coordinare-architecture.md`), so all downstream performers can read it from the repo.
- Q: What format is the architecture plan? → A: Markdown, structured with sections: Overview, Data Model, API Contracts, Component Breakdown, Implementation Approach, Open Questions.
- Q: Does the architect performer open a PR? → A: No — it commits the plan to the feature branch and returns `plan_committed` (a new terminal success state for this role). The lifecycle advances to the implementer without opening a PR.
- Q: What happens if the architect cannot produce a plan? → A: Returns `blocked` with specific questions for the human (missing business context, conflicting requirements, etc.).

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Produce an Architecture Plan (Priority: P1)

When a card reaches the architect performer, it analyses the codebase and requirements, then commits a structured technical plan to the feature branch. Downstream performers receive the plan's content as context in their dispatch payloads.

**Why this priority**: Without a committed architecture plan, the implementer performer operates without explicit design guidance, leading to inconsistent or incorrect implementations.

**Independent Test**: Can be tested by dispatching the architect performer with a real card and repository, verifying a plan file is committed to the branch, and confirming the plan contains all required sections.

**Acceptance Scenarios**:

1. **Given** the architect performer receives a dispatch with card context and a cloned repository, **When** the AI backend completes its analysis, **Then** a plan file is committed to the feature branch containing: Overview, Data Model, API Contracts, Component Breakdown, and Implementation Approach sections.
2. **Given** the plan file is committed, **When** the architect performer returns its completion status, **Then** the status is `plan_committed` (not `pr_opened`) and includes the committed file path.
3. **Given** the next performer role (implementer) is dispatched, **When** the coordinare builds its payload, **Then** the architecture plan's branch-relative path is included in the dispatch payload under an `architecture_plan_path` field.

---

### User Story 2 — Block on Insufficient Context (Priority: P2)

If the architect cannot produce a meaningful plan due to missing business context, conflicting requirements, or an ambiguous codebase structure, it returns `blocked` with specific questions for the human.

**Why this priority**: Without explicit blocking, the architect might produce a vague or incorrect plan that silently propagates bad decisions through all downstream roles.

**Independent Test**: Can be tested by dispatching the architect with a card that has incomplete or contradictory acceptance criteria, verifying it returns `blocked` with at least one concrete question.

**Acceptance Scenarios**:

1. **Given** the card's acceptance criteria are ambiguous or contradictory, **When** the architect backend cannot resolve the ambiguity, **Then** the performer returns `{"status": "blocked", "questions": ["..."]}` with specific, actionable questions.
2. **Given** the architect performer is blocked, **When** a human submits answers via `relay_feedback`, **Then** the performer resumes analysis and produces a plan.
3. **Given** the architect performer is blocked and the human declines to answer, **When** the card is cancelled, **Then** the stand is cleaned up and the session ends with `error` state.

---

### User Story 3 — Iterative Plan Refinement via Feedback (Priority: P3)

When the reviewer or security performer identifies architectural concerns during their review, human feedback is routed back to the architect. The architect updates the plan file and the lifecycle re-runs the affected downstream roles.

**Why this priority**: Architectural issues found late in the lifecycle should be correctable without discarding all downstream work — only the portions affected by the architectural change need to be re-run.

**Independent Test**: Can be tested by simulating a reviewer comment classified as an architecture concern, verifying the architect performer is re-dispatched with the comment as context, and that the plan file is updated on the branch.

**Acceptance Scenarios**:

1. **Given** the reviewer performer raises an architectural concern in its output, **When** `classify_human_feedback` routes to the architect, **Then** the architect performer receives the concern as `relay_feedback` context.
2. **Given** the architect updates the plan in response to feedback, **When** the updated plan is committed, **Then** the lifecycle re-dispatches the implementer (and all subsequent roles) rather than advancing to human review.

---

### Edge Cases

- What if the target repository has no existing code (greenfield project)? The architect must still produce a plan from requirements alone.
- What if the plan file already exists on the branch (re-run after feedback)? The architect MUST overwrite it rather than create a duplicate.
- What if the AI backend produces a plan that is too large to include in a dispatch payload? The downstream performer should read the plan from the branch rather than from the payload.
- What if the architect's plan contradicts the assessor's clarifications?
- What if committing the plan file fails (e.g., merge conflict on the branch)?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The architect performer MUST implement the full coordinare wire protocol: dispatch, status, relay_feedback, and health actions.
- **FR-002**: On a `dispatch` action, the architect MUST clone the repository, check out the feature branch, and begin AI-driven codebase analysis and plan generation.
- **FR-003**: On completion, the architect MUST commit a plan file to the feature branch at a fixed, predictable path (e.g., `docs/coordinare-architecture.md`).
- **FR-004**: The plan file MUST include all of the following sections: Overview, Data Model, API Contracts, Component Breakdown, Implementation Approach. An Open Questions section is optional.
- **FR-005**: On successful plan commit, the architect MUST return `{"status": "plan_committed", "plan_path": "..."}` as its terminal success response — not `pr_opened`.
- **FR-006**: The coordinare lifecycle MUST treat `plan_committed` as a success state that advances to the next performer role.
- **FR-007**: When the coordinare dispatches the next role after the architect, the dispatch payload MUST include the plan's branch-relative path under an `architecture_plan_path` field. Downstream performers read the plan content directly from the branch.
- **FR-008**: If the architect cannot produce a plan, it MUST return `{"status": "blocked", "questions": ["..."]}` with specific, actionable questions.
- **FR-009**: If the plan file already exists on the branch (re-run scenario), the architect MUST overwrite it rather than fail or append.
- **FR-010**: The architect performer MUST be packaged as a container image built on the base performer image, following the same two-tier image strategy as the general performer.
- **FR-011**: The architect performer's backend MUST be independently configurable in `config.yaml` under `performers.architect`.

### Key Entities

- **Architecture Plan**: A structured Markdown document committed to the feature branch describing the technical design for the card's implementation.
- **plan_committed**: A terminal success state specific to the architect role, signalling that the plan is committed and the lifecycle can advance.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of dispatched architect sessions that complete without error produce a committed plan file on the feature branch with all required sections.
- **SC-002**: The architecture plan content is present in the dispatch payload of the subsequent performer role in 100% of successful architect completions.
- **SC-003**: When re-dispatched after feedback, the architect overwrites the existing plan file in 100% of cases — no duplicate files are created.
- **SC-004**: The architect returns `blocked` with at least one concrete question in 100% of cases where the acceptance criteria are incomplete or contradictory (as determined by a test suite of known-ambiguous cards).
- **SC-005**: A developer unfamiliar with the codebase can configure and dispatch the architect performer locally — following only the README — and observe a plan file committed to a test branch within 20 minutes.

## Assumptions

- The architect performer uses the same base container image as the general performer (012-performer). No new base image is required.
- The architecture plan is written in Markdown and committed directly by the AI backend as a file on the branch — the performer does not post-process or validate the plan format.
- If the plan content exceeds a configurable payload size limit (default: 32 KB), the downstream performer reads the plan file from the branch rather than receiving it inline in the payload.
- The coordinare's `classify_human_feedback` node (019-performer-lifecycle) correctly identifies architecture-concern comments and routes them to the architect performer.
