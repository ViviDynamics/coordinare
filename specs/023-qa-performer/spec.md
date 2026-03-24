# Feature Specification: QA Performer

**Feature Branch**: `023-qa-performer`
**Created**: 2026-03-18
**Status**: Complete

## Overview

The QA performer is the seventh role in the sequential performer lifecycle (after security). Unlike the reviewer (which analyses code structure) and the security performer (which analyses code for vulnerabilities), the QA performer validates the feature from a behavioural perspective: it runs the application or its test suite, exercises the feature end-to-end, and verifies each acceptance criterion is satisfied by the running system.

The QA performer's feedback channel is distinct from the reviewer's. Reviewer findings concern code quality and architecture; QA findings concern runtime behaviour and acceptance criterion satisfaction. All QA failures route to the implementer for remediation.

## Clarifications

### Session 2026-03-18

- Q: Does the QA performer run existing tests, write new tests, or both? → A: Both. It runs the existing test suite and, if needed, writes additional tests to exercise scenarios not covered by existing tests. New tests are committed to the branch as part of the QA output.
- Q: How does the QA performer know what acceptance criteria to validate? → A: The acceptance criteria are included in the dispatch payload (same as for the implementer). The QA performer maps each criterion to one or more test scenarios.
- Q: What if the QA performer cannot start the application (missing runtime, missing config)? → A: Returns `blocked` with specific missing-dependency details. This is distinct from a QA failure — it is an environment failure.
- Q: Does QA route findings to the architect? → A: No. QA findings are always about runtime behaviour, which is the implementer's responsibility. Architecture-level concerns are the reviewer's and security performer's domain.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Validate All Acceptance Criteria Pass (Priority: P1)

When the feature branch satisfies all acceptance criteria when run, the QA performer returns `qa_passed` and the lifecycle advances to the tech writer.

**Why this priority**: The pass case is the normal completion path. Without it the lifecycle cannot reach the tech writer or human review.

**Independent Test**: Can be tested by dispatching the QA performer with a branch that correctly implements all acceptance criteria, verifying it returns `qa_passed` and the test report shows all criteria satisfied.

**Acceptance Scenarios**:

1. **Given** the QA performer receives a dispatch with a branch that satisfies all acceptance criteria, **When** the AI backend runs the test suite and exercises the feature, **Then** the performer returns `{"status": "qa_passed", "report": {"criteria_checked": N, "criteria_passed": N}}`.
2. **Given** the QA performer returns `qa_passed`, **When** the coordinare processes the result, **Then** `performer_stage` advances to the next configured role (documenting) and the card remains "In Progress".
3. **Given** existing tests pass but a criterion has no existing coverage, **When** the QA backend writes a new test, **Then** the new test is committed to the branch and runs as part of the validation.

---

### User Story 2 — Report Failures and Route to Implementer (Priority: P1)

When one or more acceptance criteria are not satisfied by the running feature, the QA performer returns a structured failure report and the coordinare relays it to the implementer for remediation.

**Why this priority**: QA failures must be caught and corrected before human review. Without routing, failed features reach human reviewers or, worse, production.

**Independent Test**: Can be tested by dispatching the QA performer with a branch that intentionally fails one acceptance criterion, verifying it returns `qa_failed` with a specific failure referencing that criterion.

**Acceptance Scenarios**:

1. **Given** the branch fails an acceptance criterion (e.g., a required feature is missing or broken), **When** the QA backend identifies the failure, **Then** the performer returns `{"status": "qa_failed", "failures": [{"criterion": "...", "expected": "...", "actual": "...", "test": "..."}]}`.
2. **Given** the QA performer returns `qa_failed`, **When** the coordinare processes the result, **Then** the failure details are relayed to the implementer via `relay_feedback` and the implementer is re-dispatched.
3. **Given** the implementer fixes the issue and the QA performer is re-dispatched, **When** all acceptance criteria are now satisfied, **Then** the performer returns `qa_passed` and the lifecycle advances.
4. **Given** failures persist after a configurable maximum fix cycle limit, **When** the limit is reached, **Then** the QA performer returns `{"status": "blocked", "questions": ["..."]}` for human attention.

---

### User Story 3 — Block on Environment Failure (Priority: P2)

When the QA performer cannot start the application due to a missing runtime, missing environment variable, or broken dependency, it returns `blocked` with a clear description of what is missing — distinct from a QA acceptance criterion failure.

**Why this priority**: Environment failures are configuration problems, not implementation bugs. Distinguishing them prevents the coordinare from routing environment issues to the implementer as if they were code defects.

**Independent Test**: Can be tested by dispatching the QA performer without a required environment variable, verifying it returns `blocked` with a message describing the missing configuration rather than `qa_failed`.

**Acceptance Scenarios**:

1. **Given** a required environment variable is absent from the dispatch payload, **When** the QA backend attempts to start the application, **Then** the performer returns `{"status": "blocked", "questions": ["Missing required environment variable: DATABASE_URL"]}`.
2. **Given** a required runtime (e.g., Node.js) is not available in the performer container, **When** the backend attempts to run the test suite, **Then** the performer returns `{"status": "blocked", "questions": ["Runtime 'node' not found. Use coordinare-performer:full or add Node.js to your custom image."]}`.

---

### Edge Cases

- What if the test suite takes longer than the QA performer's timeout?
- What if the application requires a live database or external service — should the QA performer spin one up, use a mock, or skip those tests?
- What if the test suite itself is broken (syntax error in test file)?
- What if no acceptance criteria are specified on the card — what does the QA performer validate?
- What if the implementer adds a new test that makes other tests fail?
- What if the QA performer writes a test that is technically correct but overly strict (false positive failure)?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The QA performer MUST implement the full coordinare wire protocol: dispatch, status, relay_feedback, and health actions.
- **FR-002**: On a `dispatch` action, the QA performer MUST clone the repository, check out the feature branch, and begin runtime validation of the feature.
- **FR-003**: The QA performer MUST run the existing test suite (if present) as part of its validation.
- **FR-004**: The QA performer MUST map each acceptance criterion from the dispatch payload to at least one test scenario, writing new tests if no existing tests cover that criterion.
- **FR-005**: Any new tests written MUST be committed to the feature branch before the performer returns its result.
- **FR-006**: If all acceptance criteria are satisfied, the performer MUST return `{"status": "qa_passed", "report": {"criteria_checked": N, "criteria_passed": N, "new_tests_added": N}}`.
- **FR-007**: If any acceptance criterion is not satisfied, the performer MUST return `{"status": "qa_failed", "failures": [...]}`. Each failure MUST include: `criterion` (text of the failing criterion), `expected` (what was expected), `actual` (what occurred), and `test` (the test name or description that exposed it).
- **FR-008**: If the application or test suite cannot be started due to an environment problem (missing runtime, missing env var), the performer MUST return `{"status": "blocked", "questions": ["..."]}` rather than `qa_failed`.
- **FR-009**: When the QA performer returns `qa_failed`, the coordinare MUST relay the failure details to the implementer performer via `relay_feedback`.
- **FR-010**: The QA performer MUST enforce a configurable maximum fix cycle limit (env var `QA_MAX_CYCLES`, default: 3). When the limit is reached, it MUST return `{"status": "blocked", "questions": ["..."]}`.
- **FR-011**: The QA performer MUST be independently configurable in `config.yaml` under `performers.qa`.
- **FR-012**: The QA performer MUST use the full performer image (or a custom image that includes the required runtimes) — the base performer image alone is insufficient for runtime validation.

### Key Entities

- **Acceptance Criterion Failure**: A record of a single acceptance criterion that was not satisfied at runtime — includes the criterion text, expected behaviour, actual behaviour, and the test that exposed it.
- **qa_passed**: Terminal success state — all acceptance criteria satisfied; lifecycle advances to tech writer.
- **qa_failed**: Non-terminal outcome — one or more criteria not satisfied; coordinare relays failures to implementer and re-dispatches.
- **Environment Failure**: A blocked state caused by missing infrastructure (runtime, config) rather than a code defect.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of QA sessions with one or more unsatisfied acceptance criteria return `qa_failed` with a structured failure report referencing the specific failing criteria.
- **SC-002**: 100% of new tests written by the QA performer are committed to the feature branch before the performer returns its result.
- **SC-003**: When the QA performer returns `qa_failed`, the coordinare relays the failure details to the implementer within one polling cycle.
- **SC-004**: The QA performer correctly distinguishes environment failures (→ `blocked`) from acceptance criterion failures (→ `qa_failed`) in 100% of cases in the test suite.
- **SC-005**: After a fix cycle resolves all failing acceptance criteria, the QA performer returns `qa_passed` on re-dispatch in at least 90% of test cases with intentionally fixed implementations.
- **SC-006**: The maximum fix cycle limit prevents infinite QA-remediation loops in 100% of cases.

## Assumptions

- The QA performer runs tests locally within the container — it does not deploy the application to an external environment for testing.
- The QA performer does not have access to production databases or live external services. Tests that require external services must use mocks or stubs provided in the repository.
- The full performer image (or a custom image) is used for QA because runtime validation requires language runtimes not present in the base performer image.
- The QA performer is not responsible for performance testing or load testing — only functional acceptance criterion validation.
- The coordinare's lifecycle ensures the QA performer is always dispatched after the security performer has passed, so there is no risk of QA passing a feature with known blocking security issues.
