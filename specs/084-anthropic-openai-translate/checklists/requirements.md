# Specification Quality Checklist: Anthropic→OpenAI Request-Translating Shim

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-06-08
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

- Like the 078 parent spec, this is an **infrastructure robustness layer** spec, so it
  deliberately names the concrete artifacts under repair — the Anthropic vs. OpenAI wire
  protocols, the LiteLLM proxy and its broken streaming harmony→tool_calls handling
  (LiteLLM #17246 / #13300), Ollama-direct gpt-oss, the 073 `ClaudeCodeShim`, and the 078
  `harmony_tool_calls` / `strip_reasoning` normalizers and SSE filter chain. These are the
  subject under repair, not incidental implementation choices. The WHAT is "let an
  Anthropic-wire backend reach an OpenAI-only upstream cleanly," which is inseparable from
  the named stack components.
- Translation behavior is expressed as observable round-trip outcomes (request fields reach
  the upstream; response role / content blocks / stop_reason / usage / structured tool_use
  blocks reach the CLI; zero leaked harmony markers; byte-for-byte no-op on unrouted pairs;
  fail-closed on broken tables) so requirements stay testable without prescribing internal
  class design.
- Scope is explicitly bounded: this spec builds the capability only. Flipping the live
  `qa` config to the translating path, re-architecting the 073 shim or LiteLLM, and adding
  new backends/models are all out of scope (the qa flip is a live-verified follow-on).
- No open [NEEDS CLARIFICATION] items: the request directive fixed the strategy surface
  (a per-entry translating choice in the existing routing table), the security invariants
  (FR-078-10 / FR-011), and the activation model (per `(backend, model)`), so no config-schema
  or operator-behavior decision is left unresolved at the spec level.
