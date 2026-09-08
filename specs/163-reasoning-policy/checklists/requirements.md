# Specification Quality Checklist: Reasoning Policy and Truncation Classification

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-09-04
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

Validated 2026-09-04. What the pass changed:

1. **File paths, symbol names and parameter names removed.** The first draft named
   `assessor_failure.py`, `chat_template_kwargs`, `finish_reason`, `resolve_performer_dispatch_model`
   and specific model ids throughout. All are implementation or deployment detail and were
   replaced with capability language ("the structured finish reason", "a request-level
   instruction", "the model all roles currently use"). The concrete names belong in plan.md,
   where they now go.

2. **The measured evidence is retained deliberately**, in a table, without model names. It
   is the justification for the central design decision (per-model rather than global), and
   removing it would leave FR-013 and all of US3 looking arbitrary. Measurements are
   findings about the world, not implementation choices, so the "no implementation details"
   item is judged to pass.

3. **Zero [NEEDS CLARIFICATION] markers, deliberately.** The three decisions that would
   otherwise be marked — gateway versus request, both halves or one, and ordering — were all
   settled with the requester before drafting and are recorded in Clarifications.

4. **US3 was promoted from a footnote to its own user story.** The first draft treated
   "don't enable it for the current model" as a constraint on US2. It is separable, it is
   independently testable, and getting it wrong breaks the default model in a way that
   mimics the very bug being fixed — so it earns its own story and its own acceptance
   scenarios.

5. **Two edge cases added that the input did not state**: a response that is *both*
   truncated and unparseable (FR-006), and the interaction with retry (FR-008). An
   unchanged retry of a truncated request truncates identically, so without FR-008 the fix
   would convert a wrong verdict into an infinite loop.
