# Implementation Plan: Implementer CI Gate

**Branch**: `075-implementer-ci-gate` | **Date**: 2026-05-28 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/075-implementer-ci-gate/spec.md`

## Summary

Insert a **CI gate** at the implementer→reviewer handoff that blocks the lifecycle advance until the card's PR shows green on the required check set. Today the implementer can mark itself terminal-success and hand off while CI is still red or pending; reviewers then look at code that the project's own pipeline rejects. This gate evaluates the PR's checks at the moment of handoff and either advances (all required checks green, FR-001), holds the card in `monitoring_performer` (any required check still pending, FR-002), or bounces back to the implementer with structured `relay_feedback` listing each failed check (FR-003).

The gate reuses 064's existing primitives — `services/pr_checks_service.CheckRollup` (GraphQL fetch, capped at 100 checks) and `services/pr_checks_policy.decide()` (the same fail-conclusions set `{failure, cancelled, timed_out, action_required, stale, startup_failure}` and pending-timeout policy) — parameterized by a per-card **required-checks list** rather than 064's "all checks" rollup. Resolution order for the required list (FR-006): (1) a new `services/required_checks_resolver.py` that combines the operator-defined `persona_scope.persona_check_map` (persona name → check name patterns) with the active card's `PersonaScope`; (2) GitHub branch-protection's required-status-checks set; (3) all checks present on the current HEAD. Falling back to (3) preserves "ship something useful even with zero config" (FR-014) while letting an operator narrow the gate without re-prompting 074's classifier — **no LLM contract churn**, stack-agnostic, operator-controlled.

The bounce is rate-limited by a per-card per-head-SHA **`BounceCounter`** (FR-015): on a new HEAD the counter resets; after `max_bounces_per_head` (default 3) the gate escalates to `needs_human_review` instead of looping. The counter lives on `CardSession` and round-trips through the canonical `_SESSION_FIELDS` pattern (the same drift trap fixed four times on 073 and once again on 074). Persistence schema bumps v4→v5 with an additive optional `bounce_counter` on `PersistedSession`; v4 snapshots load unaffected and recompute on next cycle. When `persona_scope` is disabled, the resolver falls through to branch-protection then all-checks (FR-009 soft dependency on 074).

The gate sits at `graph/nodes/monitor_performer.py`'s implementing→reviewing transition (different boundary from 064's merge-time gate; both gates coexist). When the gate decides HOLD, the existing `{"phase": "monitoring_performer"}` return is reused — no new state shape. When it decides BOUNCE, the node composes a `relay_feedback` entry (`{check_name, conclusion, html_url, last_log_line}` per failed check) and the existing `relay_feedback` dispatch path (070) delivers it on the next implementer turn. A `notify.py` rollup comment surfaces the gate decision on the PR for human review (FR-012), deduped by `(head_sha, decision-signature)` mirroring 064's dedup pattern. The no-PR / no-HEAD path defers entirely to 070's existing checkpoint logic (FR-013).

## Technical Context

**Language/Version**: Python 3.11 (coordinare)
**Primary Dependencies**: existing — LangGraph (graph), pydantic v2 (config + bounce-counter schema), structlog (observability), coordinare's existing `services/pr_checks_service.py` + `services/pr_checks_policy.py` (from 064), `services/github_service.py` for branch-protection lookups. No new runtime deps.
**Storage**: `BounceCounter` lives on `CardSession`; persisted via the existing `WorkflowSnapshot` / `PersistedSession` JSON snapshot. Schema bump v4→v5: new optional `bounce_counter: dict[str, int]` (head_sha → count) on `PersistedSession`; v4 snapshots load with `bounce_counter={}` and recompute on next cycle.
**Testing**: pytest (existing) — unit tests for `RequiredChecksResolver` (persona_check_map combine, branch-protection fallback, all-checks fallback), gate decision (PASS / HOLD / BOUNCE / ESCALATE branches), `BounceCounter` reset-on-new-HEAD invariant, `_SESSION_FIELDS` round-trip regression test, structured-relay-feedback shape contract, notify-rollup dedup. Contract test for gate-decision schema and persona-check-map config schema. Integration test that walks a card through implementer→red-CI→bounce→fix→green→reviewer.
**Target Platform**: Linux (existing coordinare host)
**Project Type**: single (existing monorepo)
**Performance Goals**: gate evaluation ≤500 ms p50 / ≤3 s p95 (one GraphQL check rollup + one branch-protection fetch, both already in use by 064); per-cycle overhead negligible vs. existing implementer poll cadence.
**Constraints**: must never block the pipeline on gate evaluation failure — on any unexpected exception, log and advance (FR-011), matching 074's fail-open posture; must not mutate persona base prompts; must round-trip through `_SESSION_FIELDS` (canonical drift trap); must surface gate decision on PR (FR-012); stack-agnostic — zero hardcoded check names in coordinare source (FR-008 / SC-005); soft-dependency on 074 — works with or without `PersonaScope` (FR-009).
**Scale/Scope**: one gate eval per implementer→reviewer transition per card per cycle; ~5–20 required checks per card typical; one in-memory `BounceCounter` dict per active session. No new persistent indices.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First**: PASS — one new service (`required_checks_resolver.py`), one new pydantic model block (`PersonaCheckMapConfig`, `CIGateConfig`), ~50 LOC added to `monitor_performer.py` (gate eval at implementing→reviewing boundary), ~10 LOC added to `notify.py` (rollup dedup), 6 lines added to `_SESSION_FIELDS`. Reuses 064's `CheckRollup` + `decide()` rather than reimplementing. No new abstractions beyond what the feature requires; no speculative interfaces.
- **II. Testing Discipline (NON-NEGOTIABLE)**: PASS — unit coverage for resolver (3 fallback layers), gate decision (4 branches), bounce-counter reset, relay-feedback shape, session round-trip regression, notify-dedup. Contract test for gate-decision + persona-check-map schemas. Integration test for end-to-end bounce→fix→pass. No coverage regression.
- **III. UX Consistency**: PASS — config surface follows existing `symphony.persona_scope.*` nesting (extends 074's block rather than introducing a new top-level key); reuses 064's PR-comment surface and dedup pattern; structured logs follow existing `structlog` conventions; relay-feedback uses the same channel as 070.
- **IV. Performance by Design**: PASS — gate fetches are already-cached primitives from 064 (CheckRollup is GraphQL one-shot); branch-protection lookup is cached per-cycle. No new polling loops. Gate runs once per implementer→reviewer transition, not per implementer turn.
- **V. Clarity Before Action**: PASS — spec has zero NEEDS CLARIFICATION markers; fail-open fallback explicit (FR-011); resolver fallback ladder explicit (FR-006); bounce escalation explicit (FR-015); no-PR path delegation explicit (FR-013); 074 soft-dependency explicit (FR-009).

**Result**: No violations. No entries in Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/075-implementer-ci-gate/
├── plan.md                            # This file
├── research.md                        # Phase 0: resolver design, 064 reuse rationale, integration points
├── data-model.md                      # Phase 1: CIGateDecision, BounceCounter, RequiredChecksList, config shape, CardSession field
├── quickstart.md                      # Phase 1: adopting on a new project (persona_check_map + branch-protection notes)
├── contracts/
│   ├── gate-decision.md               # Phase 1: CIGateDecision schema, relay_feedback entry shape, PR-rollup-comment template
│   └── config-schema.md               # Phase 1: persona_scope.persona_check_map + ci_gate.* config block schema (pydantic + YAML example)
└── tasks.md                           # Phase 2 output (/speckit.tasks — NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
src/coordinare/
├── config.py                          # +CIGateConfig, +PersonaCheckMapConfig (nested under PersonaScopeConfig)
├── session.py                         # +bounce_counter field on CardSession + _SESSION_FIELDS
├── state_store.py                     # +optional bounce_counter on PersistedSession (schema v5, MIN unchanged)
├── services/
│   └── required_checks_resolver.py    # New: resolve required check list (persona_check_map → branch-protection → all-checks)
└── graph/
    ├── nodes/
    │   ├── monitor_performer.py       # +CI gate at implementing→reviewing transition; BOUNCE composes relay_feedback
    │   └── notify.py                  # +emit PR-comment gate rollup, deduped by (head_sha, decision-signature)
    └── routing.py                     # No change — HOLD reuses existing monitoring_performer phase return

tests/
├── contract/
│   ├── test_gate_decision_schema.py   # New: CIGateDecision + relay_feedback entry shape
│   └── test_persona_check_map_schema.py  # New: config schema contract
├── integration/
│   └── test_implementer_ci_gate_e2e.py   # New: red-CI bounce → fix → green → reviewer walk
└── unit/
    ├── services/
    │   └── test_required_checks_resolver.py  # New: 3-layer fallback, persona_check_map combine
    ├── graph/nodes/
    │   ├── test_monitor_performer_ci_gate.py # New: PASS/HOLD/BOUNCE/ESCALATE branches, bounce-counter reset
    │   └── test_notify_ci_gate_rollup.py     # New: dedup by (head_sha, decision-signature)
    └── test_session.py                # +regression test: bounce_counter round-trips through _SESSION_FIELDS
```

**Structure Decision**: Single project, existing `src/coordinare/` layout. The change adds one service (`required_checks_resolver.py`), extends `monitor_performer.py` with the gate-eval block at the implementing→reviewing boundary, extends `notify.py` with a rollup comment, and adds three small pydantic config additions. Reuses 064's `pr_checks_service` + `pr_checks_policy` and 070's `relay_feedback` channel without modification. No new top-level packages; integration matches the existing graph-node + service-layer pattern, mirroring 074's structure.

## Complexity Tracking

> No constitution violations — table intentionally empty.
