# Feature Specification: Tech Writer Performer

**Feature Branch**: `024-tech-writer-performer`
**Created**: 2026-03-18
**Status**: Draft

## Overview

The tech writer performer is the eighth and final automated role in the sequential performer lifecycle (after QA). It runs after all functional and quality gates have passed, reviewing the full set of changes on the feature branch and producing comprehensive documentation updates. These include: README updates, a CHANGELOG entry, inline code documentation (docstrings and comments for new or changed public interfaces), and any project-specific documentation files declared in the repository's documentation conventions.

The tech writer commits all documentation changes to the feature branch before the card transitions to "In Review" for a human. The human therefore sees a feature that is complete — implemented, reviewed, security-checked, QA'd, and documented — requiring only human approval rather than documentation cleanup.

## Clarifications

### Session 2026-03-18

- Q: What documentation artefacts does the tech writer produce? → A: At minimum: CHANGELOG entry, README section updates (if applicable), and inline docstrings/comments for new or changed public interfaces. Additional docs (API reference, config reference) are produced if the repository has conventional locations for them.
- Q: Does the tech writer open a new PR? → A: No — it commits documentation changes to the existing feature branch and returns `docs_committed` as its terminal success state.
- Q: How does the tech writer know what changed? → A: It reads the feature branch diff and the architecture plan (if present) to understand what was built. The dispatch payload also includes the card title, description, and acceptance criteria.
- Q: What if the human reviewer finds the documentation insufficient? → A: Human feedback about documentation routes back to the tech writer via `classify_human_feedback`. The tech writer updates the docs and the PR is re-presented for human approval.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Produce Complete Documentation for a Feature (Priority: P1)

When a card reaches the tech writer, it reviews all changes on the branch and commits documentation updates. The human reviewer sees a fully documented feature in the PR.

**Why this priority**: Documentation is a required output of every feature. Without the tech writer, documentation is left to the human reviewer or skipped entirely.

**Independent Test**: Can be tested by dispatching the tech writer with a branch that has new public functions but no docstrings, verifying that docstrings are committed for each new function and a CHANGELOG entry is present.

**Acceptance Scenarios**:

1. **Given** the tech writer receives a dispatch with a branch that has new or modified public interfaces, **When** the AI backend completes its documentation pass, **Then** the feature branch contains: a CHANGELOG entry, updated README (where applicable), and inline docstrings for each new or changed public interface.
2. **Given** all documentation is committed, **When** the tech writer returns its completion status, **Then** the status is `docs_committed` and includes a list of files modified.
3. **Given** the tech writer returns `docs_committed`, **When** the coordinare processes the result, **Then** the card transitions to "In Review" and `performer_stage` is cleared (all automated roles complete).

---

### User Story 2 — Update Documentation After Human Feedback (Priority: P2)

When a human posts a comment on the PR requesting documentation improvements, the coordinare routes the feedback to the tech writer, which updates the docs and re-commits. The PR is re-presented for human approval.

**Why this priority**: Human feedback about documentation is a normal and expected part of the review process. The tech writer must be able to respond to it without requiring implementation changes.

**Independent Test**: Can be tested by posting a PR comment requesting clarification in the README, verifying the tech writer is re-dispatched, updates the README, and commits the change.

**Acceptance Scenarios**:

1. **Given** a human posts a PR comment requesting a README clarification, **When** `classify_human_feedback` routes it to the tech writer, **Then** the tech writer is re-dispatched with the comment as context and updates the README.
2. **Given** the tech writer commits the requested documentation update, **When** it returns `docs_committed`, **Then** the card returns to "In Review" for human re-approval.
3. **Given** the human approves the PR after the documentation update, **When** the coordinare detects the approval, **Then** the card advances to "Done" without re-entering any automated role.

---

### User Story 3 — Detect and Respect Existing Documentation Conventions (Priority: P2)

The tech writer discovers the project's documentation conventions from the repository (docstring style, CHANGELOG format, README structure) and follows them rather than imposing a generic format.

**Why this priority**: Documentation that does not match existing conventions looks foreign and will be rejected or rewritten by the human reviewer, defeating the purpose of the role.

**Independent Test**: Can be tested by dispatching the tech writer on a project with an existing CHANGELOG in Keep a Changelog format, verifying the new entry follows that format rather than a different convention.

**Acceptance Scenarios**:

1. **Given** the repository uses Google-style docstrings, **When** the tech writer adds docstrings, **Then** the new docstrings follow Google style.
2. **Given** the repository has a CHANGELOG in Keep a Changelog format, **When** the tech writer adds a new entry, **Then** the entry follows the Keep a Changelog section structure.
3. **Given** the repository has no existing documentation conventions, **When** the tech writer produces documentation, **Then** it uses sensible defaults (NumPy docstrings for Python, JSDoc for JavaScript, Keep a Changelog format).

---

### Edge Cases

- What if the branch has no new or changed public interfaces — what does the tech writer produce?
- What if the CHANGELOG file does not exist — should the tech writer create it?
- What if the commit of documentation changes fails (merge conflict)?
- What if the tech writer's documentation is incorrect (e.g., wrong parameter description) — can the human correct it directly on the PR?
- What if the repository has a documentation build step (e.g., Sphinx, MkDocs) — should the tech writer run it?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The tech writer performer MUST implement the full coordinare wire protocol: dispatch, status, relay_feedback, and health actions.
- **FR-002**: On a `dispatch` action, the tech writer MUST clone the repository, check out the feature branch, and analyse the full diff relative to the base branch.
- **FR-003**: The tech writer MUST produce a CHANGELOG entry for the feature and commit it to the branch. If no CHANGELOG file exists, the tech writer MUST create one.
- **FR-004**: The tech writer MUST update the README for any user-facing changes described in the card's acceptance criteria or detected in the diff (new configuration options, new CLI commands, new API endpoints, etc.).
- **FR-005**: The tech writer MUST add or update inline documentation (docstrings, JSDoc, or equivalent) for every new or modified public function, class, method, or module in the diff.
- **FR-006**: The tech writer MUST detect the repository's existing documentation conventions (docstring style, CHANGELOG format) and follow them. Where no convention exists, sensible defaults are used.
- **FR-007**: On successful documentation commit, the tech writer MUST return `{"status": "docs_committed", "files_modified": [...]}` as its terminal success response.
- **FR-008**: The coordinare lifecycle MUST treat `docs_committed` as the final automated success state, transitioning the card to "In Review" after this response.
- **FR-009**: The tech writer MUST be re-dispatchable via `relay_feedback` when a human requests documentation improvements, updating the relevant files and committing the changes.
- **FR-010**: If the tech writer cannot determine what to document (e.g., the diff is empty or the changes are purely configuration), it MUST return `docs_committed` with an empty `files_modified` list rather than blocking.
- **FR-011**: The tech writer performer MUST be independently configurable in `config.yaml` under `performers.tech_writer`.

### Key Entities

- **Documentation Commit**: The set of files modified or created by the tech writer in a single session — CHANGELOG entry, README updates, and inline documentation.
- **docs_committed**: Terminal success state specific to the tech writer role — documentation is committed and the lifecycle can transition to "In Review".

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of tech writer sessions that complete without error produce at least one committed documentation change (CHANGELOG entry, README update, or docstring addition) on the feature branch.
- **SC-002**: 100% of sessions on repositories with existing docstring conventions produce new docstrings that match the detected convention (validated against a corpus of known-convention test repositories).
- **SC-003**: When a human posts a documentation improvement request, the tech writer re-dispatches and commits an update within one coordinare polling cycle.
- **SC-004**: The card transitions to "In Review" within one coordinare polling cycle of `docs_committed` being returned.
- **SC-005**: A CHANGELOG entry is present in 100% of branches processed by the tech writer, whether the CHANGELOG file existed before or not.

## Assumptions

- The tech writer does not run any documentation build step (Sphinx, MkDocs, etc.). It only modifies source documentation files. Running builds is the responsibility of CI.
- The tech writer reads the architecture plan file (if present) to supplement its understanding of what was built, but does not modify the plan.
- Inline documentation is generated for all public interfaces in the diff. Private/internal interfaces are documented at the tech writer's discretion based on complexity.
- The human reviewer is the final judge of documentation quality. The tech writer aims for completeness and correctness; stylistic preferences are resolved in the human review stage.
- The coordinare's `classify_human_feedback` node correctly identifies documentation-concern PR comments and routes them to the tech writer.
