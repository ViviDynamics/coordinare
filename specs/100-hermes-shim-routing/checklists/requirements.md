# Specification Quality Checklist: Route the hermes (tech_writer) Backend Through the Self-Hosted Normalizer Shim

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-06-21
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

- Grounded in a live reproduction (2026-06-21): cards blocked at documenting on `malformed_output` because hermes/tech_writer (a JSON-only role) talks its provider directly, bypassing the normalizer shim that fronts the other contract-bound roles; a reasoning model's wrapped output fails its strict parse. The spec keeps the mechanism abstract; the concrete substrate (launch.py UNSUPPORTED_BACKENDS / PROVIDER_BASE_URL_ENV, the SelfHostedShim, the 099 completion probe, the HERMES_BASE_URL wire-path convention) is named only in Dependencies/Assumptions.
- The coordinare-side 098 resilience and the doc-commit gitignored-path filter are explicitly out of scope (separate, already shipped). Model choice is operator-owned.
- No clarifications outstanding. The exact wire-path-suffix mechanism + which normalizer keys to declare are implementation details for planning, not scope ambiguities.
- Ready for `/speckit.plan`.
