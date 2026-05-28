# Implementation Plan: Persona Scope Tiering

**Branch**: `074-persona-scope-tiering` | **Date**: 2026-05-27 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/074-persona-scope-tiering/spec.md`

## Summary

Insert a **classifier step** between card pickup and persona dispatch that produces a `PersonaScope` mapping each persona (reviewer, security, qa, tech_writer, closer) to `{ depth: skim|normal|full|skip, focus: <free-text>, overrides: [...] }`. The classifier consumes only **deterministic inputs** — file paths, ±LOC per file, the project's path-class globs, and the project's CLAUDE.md/AGENTS.md context — never the raw diff body (FR-002). It runs on **coordinare's existing `conducting` backend** (no new backend slot — FR-014), with a configurable latency budget (default 30 s — FR-016) and a hard fallback to depth=`full` everywhere on any failure (FR-006).

`PersonaScope` rides on `CardSession`, round-trips through `_SESSION_FIELDS` (FR-011 — guarding against the recurring drift pattern fixed four times on 073), and is recomputed **every cycle** (FR-003) so a card that grows from skim-sized to full-sized between dispatches gets re-scoped on the next cycle. Each persona's dispatch is augmented with a **structured scope slice** delivered as a separate input field (not by mutating the persona base prompt — FR-007); personas without a `scope_behavior` block in project config silently opt out and run as today (FR-010 additive default). Path-class matches under `persona_scope.forced_full_on_path_classes` deterministically override the LLM's judgment for risk classes (FR-005), so a 6-line auth-path change can't be classified `skim` regardless of what the classifier says.

The change is localized: one new service module (`services/persona_classifier.py`), one new node (`graph/nodes/classify_scope.py`) inserted in the graph between `dispatch_card` and `dispatch_performer`, a new `PersonaScopeConfig` pydantic model under `src/coordinare/config.py`, the `_SESSION_FIELDS` extension, and a small modification to `dispatch_performer.py` to read the per-persona slice and apply `max_tool_calls` / `prompt_addon` from the project's `scope_behavior` block when present. Personas with `depth: skip` short-circuit at the routing layer — the existing routing.py decision tree gains one branch that advances the lifecycle past skipped stages without invoking the performer.

## Technical Context

**Language/Version**: Python 3.11 (coordinare)
**Primary Dependencies**: existing — LangGraph (graph), pydantic v2 (config + scope schema), structlog (observability), coordinare's existing `ConductingBackend` Protocol in `src/coordinare/services/conducting.py`. No new runtime deps.
**Storage**: `PersonaScope` lives on `CardSession`; persisted via the existing `WorkflowSnapshot` / `PersistedSession` JSON snapshot. Schema bump: new optional `persona_scope` field on `PersistedSession` (defaults to `None`; v1/v2 snapshots load unaffected and recompute on next cycle — FR-011).
**Testing**: pytest (existing) — unit tests for classifier prompt rendering + response parsing, forced-full overrides, fallback path, skip-routing, `_SESSION_FIELDS` round-trip regression test (mirrors the four 073-branch regressions), contract test for classifier-prompt schema, integration test that walks a docs-only card and a security-sensitive card through the graph.
**Target Platform**: Linux (existing coordinare host)
**Project Type**: single (existing monorepo)
**Performance Goals**: per-cycle classifier latency ≤2 s p50 / ≤10 s p95 on coordinare's existing backend (SC-006); end-to-end docs-only card ≥40% faster than baseline (SC-001).
**Constraints**: must never block the pipeline on classifier failure (FR-006 / SC-003); must not mutate persona base prompts (FR-007); must round-trip through `_SESSION_FIELDS` (FR-011); must surface scope on the PR for human review (FR-012 / SC-004); stack-agnostic — zero hardcoded path patterns in coordinare source (FR-004 / SC-005).
**Scale/Scope**: per-card classifier call once per cycle; ~5–9 personas per card; one in-memory `PersonaScope` dict per active session. No new persistent indices.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First**: PASS — one new service, one new graph node, one new pydantic model, ~30 LOC added to `dispatch_performer.py` (read scope slice, apply `max_tool_calls`/`prompt_addon`), 5 lines added to `_SESSION_FIELDS`. No new abstractions beyond what the feature requires; no speculative interfaces.
- **II. Testing Discipline (NON-NEGOTIABLE)**: PASS — unit coverage for prompt rendering, response parsing, forced-full overrides, fallback path, skip routing, session round-trip regression. Contract test for classifier I/O schema. Integration test for docs-only + security-sensitive card paths. No coverage regression.
- **III. UX Consistency**: PASS — config surface follows existing `symphony.personas.<name>` nesting; reuses `ConductingConfig` (no new backend slot); structured logs follow existing `structlog` conventions; PR-visible surface uses the existing PR-comment mechanism (same channel as 064's rollup).
- **IV. Performance by Design**: PASS — classifier sees deterministic inputs only (path stats, not raw diff), so prompt tokens are bounded by file count, not diff size. SC-006 budget (2 s p50 / 10 s p95) is generous given the small prompt. Per-cycle recompute is amortized across the cycle's existing tool-call latency.
- **V. Clarity Before Action**: PASS — spec has zero NEEDS CLARIFICATION markers; all fallback paths explicit (FR-006, FR-015); structured-vs-prompt-mutation boundary explicit (FR-007); persona opt-out default explicit (FR-010); closer scope-invariance explicit (FR-009).

**Result**: No violations. No entries in Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/074-persona-scope-tiering/
├── plan.md                            # This file
├── research.md                        # Phase 0: input schema, prompt design rationale, integration points
├── data-model.md                      # Phase 1: PersonaScope, PathClass, ScopeBehavior, config shape, CardSession field
├── quickstart.md                      # Phase 1: adopting on a new project (path_classes + scope_behavior)
├── contracts/
│   ├── classifier-prompt.md           # Phase 1: full system+user prompt template, input schema, output JSON schema
│   └── config-schema.md               # Phase 1: persona_scope.* config block schema (pydantic + YAML example)
└── tasks.md                           # Phase 2 output (/speckit.tasks — NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
src/coordinare/
├── config.py                          # +PersonaScopeConfig, +PathClassConfig, +ScopeBehavior on PersonaConfig
├── session.py                         # +persona_scope field on CardSession + _SESSION_FIELDS
├── state_store.py                     # +optional persona_scope on PersistedSession (schema v4, MIN unchanged)
├── services/
│   └── persona_classifier.py          # New: classifier prompt rendering + ConductingBackend call + parse + fallback
└── graph/
    ├── nodes/
    │   ├── classify_scope.py          # New: graph node — compute PersonaScope, write to session
    │   ├── dispatch_performer.py      # +read scope slice; +apply max_tool_calls/prompt_addon when scope_behavior set
    │   └── notify.py                  # +emit PR-comment scope rollup on first compute per card
    ├── routing.py                     # +skip-past-persona branch when depth=skip
    └── builder.py                     # +wire classify_scope node between dispatch_card and dispatch_performer

tests/
├── contract/
│   └── test_persona_classifier_io.py  # New: input/output schema contract
├── integration/
│   └── test_persona_scope_e2e.py      # New: docs-only + security-sensitive cards walk the graph
└── unit/
    ├── services/
    │   └── test_persona_classifier.py # New: prompt rendering, parse, fallback, forced-full overrides
    ├── graph/nodes/
    │   ├── test_classify_scope.py     # New: node behavior + session write
    │   └── test_dispatch_performer_scope.py  # New: scope slice consumption
    └── test_session.py                # +regression test: persona_scope round-trips through _SESSION_FIELDS
```

**Structure Decision**: Single project, existing `src/coordinare/` layout. The change adds one service, one graph node, three small files of pydantic config, and extends three existing files (`dispatch_performer.py`, `routing.py`, `builder.py`). No new top-level packages; integration matches the existing graph-node + service-layer pattern.

## Complexity Tracking

> No constitution violations — table intentionally empty.
