<!--
  Sync Impact Report
  ==================
  Version change: 1.0.0 → 1.1.0
  Modified principles: None renamed
  Added sections:
    - Principle V: Clarity Before Action
  Removed sections: None
  Templates requiring updates:
    - .specify/templates/plan-template.md — ✅ compatible (Technical
      Context fields already use NEEDS CLARIFICATION pattern)
    - .specify/templates/spec-template.md — ✅ compatible (FR examples
      already demonstrate NEEDS CLARIFICATION markers)
    - .specify/templates/tasks-template.md — ✅ compatible (Prerequisites
      section already gates on required input documents)
    - .specify/templates/commands/*.md — no command files exist yet
  Follow-up TODOs: None
-->

# Coordinare Constitution

## Core Principles

### I. Code Quality First

All code committed to the repository MUST meet the following
non-negotiable standards:

- **Readability over cleverness**: Code MUST be written for humans
  first. Prefer explicit, self-documenting code over terse or
  "clever" solutions.
- **Single Responsibility**: Every module, class, and function MUST
  have one clear purpose. If a component cannot be described in a
  single sentence, it MUST be decomposed.
- **Consistent style**: All code MUST pass configured linting and
  formatting checks before merge. No exceptions.
- **No dead code**: Unused imports, variables, functions, and
  commented-out blocks MUST be removed. Do not commit code "for
  later."
- **Minimal dependencies**: Every external dependency MUST be
  justified. Prefer standard library solutions when they meet
  requirements without significant complexity.
- **Type safety**: Where the language supports it, type annotations
  MUST be used for all public interfaces. Internal code SHOULD use
  types where they improve clarity.

**Rationale**: Consistent, readable code reduces onboarding time,
minimizes bugs, and makes reviews faster. Quality at the source
prevents compounding technical debt.

### II. Testing Discipline (NON-NEGOTIABLE)

Testing is mandatory and follows a structured hierarchy:

- **Unit tests**: Every public function and method MUST have
  corresponding unit tests covering happy paths, edge cases, and
  error conditions.
- **Integration tests**: Cross-module interactions and external
  service boundaries MUST have integration tests.
- **Contract tests**: API endpoints and inter-service interfaces
  MUST have contract tests that verify request/response schemas.
- **Coverage threshold**: Test coverage MUST NOT decrease on any
  PR. New code MUST meet or exceed the project's configured
  coverage minimum.
- **Test-first when specified**: When a feature spec or plan
  requires TDD, the Red-Green-Refactor cycle MUST be followed:
  write tests first, verify they fail, then implement.
- **Tests MUST be deterministic**: No flaky tests. Tests that
  depend on external state MUST use mocks, stubs, or fixtures.
  Tests that intermittently fail MUST be fixed or removed.
- **Test naming**: Test names MUST describe the behavior being
  verified, not the implementation (e.g.,
  `test_returns_error_when_input_is_empty` not `test_validate`).

**Rationale**: Tests are the executable specification of the
system. They enable fearless refactoring, catch regressions early,
and serve as living documentation.

### III. User Experience Consistency

All user-facing interfaces MUST deliver a coherent, predictable
experience:

- **Consistent patterns**: Similar actions MUST behave the same way
  across the application. Navigation, feedback, error presentation,
  and data display MUST follow established patterns.
- **Error communication**: Error messages MUST be actionable and
  user-friendly. Users MUST always know what went wrong and what
  they can do about it. Internal error codes or stack traces MUST
  NOT be exposed.
- **Loading and state feedback**: Every asynchronous operation MUST
  provide visual feedback (loading indicators, progress bars, or
  status messages). Users MUST NOT face blank screens or
  unresponsive interfaces.
- **Accessibility**: All interfaces MUST meet WCAG 2.1 AA as a
  minimum. Keyboard navigation, screen reader support, and
  sufficient color contrast are required.
- **Responsive behavior**: Interfaces MUST function correctly
  across supported viewport sizes and input methods without layout
  breakage or feature loss.
- **Design tokens**: Colors, typography, spacing, and other visual
  properties MUST be sourced from a shared design token system.
  Hard-coded values are not permitted.

**Rationale**: Users build mental models based on consistency.
Breaking those models creates confusion, increases support burden,
and erodes trust. Accessibility is not optional — it is a quality
requirement.

### IV. Performance by Design

Performance is a first-class requirement, not an afterthought:

- **Budgets**: Every feature MUST define measurable performance
  budgets during the planning phase (e.g., response time, memory
  usage, bundle size, frame rate). These budgets MUST appear in the
  feature spec's Success Criteria.
- **Measurement**: Performance MUST be measured, not assumed. Load
  testing, profiling, or benchmarking MUST validate that budgets
  are met before a feature is considered complete.
- **Regression prevention**: Performance-critical paths MUST have
  automated benchmarks in CI. A PR that degrades a tracked metric
  beyond the defined threshold MUST NOT be merged.
- **Efficient by default**: Prefer lazy loading, pagination, and
  streaming over eager loading of large datasets. Avoid N+1
  queries, unnecessary re-renders, and redundant computations.
- **Caching strategy**: Cacheable data MUST be identified during
  design. Cache invalidation strategy MUST be documented before
  implementation.
- **Startup time**: Application cold-start time MUST be tracked
  and kept within the defined budget.

**Rationale**: Users perceive performance as quality. Slow software
feels broken. Proactive performance engineering is cheaper than
retroactive optimization.

### V. Clarity Before Action

When requirements, prompts, or instructions are ambiguous or
incomplete, clarification MUST be sought before proceeding:

- **Never assume intent**: If a requirement can be interpreted in
  more than one way, the implementer MUST stop and ask for
  clarification. Guessing leads to rework.
- **Mark unknowns explicitly**: Unclear requirements in specs and
  plans MUST be tagged with `NEEDS CLARIFICATION` followed by a
  description of what is missing and what options exist.
- **Block on ambiguity**: Implementation MUST NOT begin on any
  requirement tagged `NEEDS CLARIFICATION`. Ambiguous items MUST
  be resolved before work starts.
- **Ask specific questions**: Clarification requests MUST be
  targeted and actionable (e.g., "Should authentication use OAuth
  or email/password?"), not open-ended (e.g., "What do you want
  here?").
- **Document resolutions**: Once a clarification is resolved, the
  answer MUST be recorded in the relevant spec, plan, or task
  document. Verbal-only resolutions are not acceptable.
- **Scope boundaries**: If a request's scope is unclear, the
  implementer MUST confirm the boundary before writing code.
  Delivering more or less than intended wastes effort either way.

**Rationale**: The cost of asking a question is minutes. The cost
of building the wrong thing is days or weeks. Ambiguity is a risk
that compounds — catching it early is the cheapest form of quality
assurance.

## Quality Gates

All code changes MUST pass the following gates before merge:

1. **Lint & Format**: Automated style checks pass with zero
   warnings or errors.
2. **Type Check**: Static type analysis passes (where applicable).
3. **Unit Tests**: All unit tests pass. No skipped tests without
   a tracked issue.
4. **Integration Tests**: All integration tests pass for affected
   modules.
5. **Coverage Check**: Test coverage meets or exceeds the
   configured minimum; coverage does not regress.
6. **Performance Check**: Automated benchmarks pass (for
   performance-critical paths).
7. **Accessibility Check**: Automated accessibility scans pass
   (for UI changes).
8. **Code Review**: At least one approving review from a team
   member who did not author the change.

Changes that fail any gate MUST NOT be merged. Gate failures MUST
be resolved, not bypassed.

## Development Workflow

### Branch Strategy

- Feature branches MUST be created from `main`.
- Branch names MUST follow the pattern `###-feature-name` where
  `###` is the issue or feature number.
- Branches MUST be kept up to date with `main` via rebase before
  merge.

### Commit Standards

- Commit messages MUST follow Conventional Commits format
  (e.g., `feat:`, `fix:`, `test:`, `docs:`, `refactor:`).
- Each commit MUST represent a single logical change.
- Commits MUST NOT include unrelated changes.

### Review Process

- All changes MUST go through pull request review.
- PRs MUST include a description of what changed and why.
- PRs MUST reference the related issue or feature spec.
- Reviewers MUST verify compliance with this constitution's
  principles.

### Definition of Done

A feature is complete when:
- All acceptance criteria from the spec are met.
- All quality gates pass.
- Performance budgets are verified.
- No `NEEDS CLARIFICATION` tags remain in the feature's artifacts.
- Documentation is updated (if user-facing behavior changed).
- The feature is deployable independently.

## Governance

This constitution is the authoritative reference for development
standards in the Coordinare project. It supersedes informal
practices, verbal agreements, and outdated documentation.

### Amendment Procedure

1. Propose the change via a pull request modifying this file.
2. The PR description MUST explain the rationale for the change.
3. At least one project maintainer MUST approve the amendment.
4. The `CONSTITUTION_VERSION` MUST be incremented per semantic
   versioning:
   - **MAJOR**: Principle removed, redefined, or made incompatible
     with prior interpretation.
   - **MINOR**: New principle or section added, or existing
     guidance materially expanded.
   - **PATCH**: Wording clarification, typo fix, or non-semantic
     refinement.
5. `LAST_AMENDED_DATE` MUST be updated to the merge date.
6. All dependent templates and docs MUST be checked for
   consistency after amendment.

### Compliance

- All pull requests and code reviews MUST verify compliance with
  these principles.
- Violations discovered post-merge MUST be tracked as issues and
  remediated promptly.
- Complexity or scope that conflicts with a principle MUST be
  justified in writing (see plan template's Complexity Tracking
  table) and approved by a maintainer.

**Version**: 1.1.0 | **Ratified**: 2026-02-16 | **Last Amended**: 2026-02-16
