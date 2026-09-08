# Specification Quality Checklist: The advocate and the curator become performer runs

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-09-07
**Feature**: [spec.md](../spec.md)

## Content Quality

- [X] No implementation details (languages, frameworks, APIs)
- [X] Focused on user value and business needs
- [X] Written for non-technical stakeholders
- [X] All mandatory sections completed

## Requirement Completeness

- [X] No [NEEDS CLARIFICATION] markers remain
- [X] Requirements are testable and unambiguous
- [X] Success criteria are measurable
- [X] Success criteria are technology-agnostic (no implementation details)
- [X] All acceptance scenarios are defined
- [X] Edge cases are identified
- [X] Scope is clearly bounded
- [X] Dependencies and assumptions identified

## Feature Readiness

- [X] All functional requirements have clear acceptance criteria
- [X] User scenarios cover primary flows
- [X] Feature meets measurable outcomes defined in Success Criteria
- [X] No implementation details leak into specification

## Notes

An earlier draft of this feature, scoped only to the persona text and the
documentation, was abandoned once the decision was taken to move both roles into
performer runs: its first user story was a refactor of the very code the move
retires, so writing it would have been work we expected to delete.

Three requirements exist because a code reading found risks no user story would
have surfaced, and each is stated as an outcome rather than as a mechanism:

FR-006, that each role returns its own terminal outcome. The performer's status
handling ends in a shared path that runs lint, pushes the branch and opens a
pull request, and its own comment records that every role before now returns
before reaching it. A new role added without its own terminal outcome would
silently open pull requests. This is the one failure in this feature that would
appear first in a live run rather than in review, so it is a requirement and a
success criterion, not a note.

FR-004 and FR-005, on the in-flight marker's ordering and its deliberate
non-durability. The existing card-less run paths document a stale-marker
deadlock they hit by setting the marker after starting the run, and a separate
decision not to persist it so a crash cannot wedge the role permanently. Both
are recorded here so the same two bugs are not rediscovered.

FR-008, that an outcome is flushed rather than waiting on a lifecycle change.
A card-less run moves no lifecycle signature, and the existing save path is
gated on such a signature, so an outcome recorded in that window is lost to a
restart. This has already been fixed once elsewhere in the system.

No [NEEDS CLARIFICATION] markers were needed. Six decisions that would otherwise
have warranted them were settled before drafting: that both roles run in a
performer with coordinare only starting them, that the performer performs the
GitHub work, that the curator judges each issue with one guarded model
judgement, that a qualifying issue goes to the backlog for a human to promote,
that card selection narrows to exclude only escalated issues, and that the two
jobs stay separate roles. All six are reflected in the requirements, and the
consequences that were not decided but follow from them are recorded under
Assumptions.
