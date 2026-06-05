# Implementation Plan: Dual-Model Planner/Executor Orchestration

**Branch**: `080-dual-model-orchestration` | **Date**: 2026-06-05 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/080-dual-model-orchestration/spec.md`

## Summary

Add a configurable per-performer planner/executor capability to coordinare's performers, plus a unified model-selection config model. Two halves:

1. **Config unification (coordinare side).** Introduce root-level `endpoints`, `model_endpoints`, and `modes` catalogs. Every performer references one `mode` (which carries the strategy + the model_endpoint refs). `strategy: single` reproduces today's single-model behavior; `always` / `conditional` / `think_once` drive the dual-model proxy. Inline `performers.<role>.model` is removed (hard cut) and resolution flows `performer.mode → modes → model_endpoints → endpoints`. All references validated at load.

2. **Dual-model proxy (performer side).** Generalize the proven `ClaudeCodeShim` reverse-proxy pattern into a `DualModelProxy` that sits on the CLI→provider seam (the `<PREFIX>_PROVIDER_BASE_URL` override the backend already honors). For non-`single` strategies the proxy intercepts each completion the CLI sends to its provider, runs the configured `OrchestrationStrategy` (think→act) against two independently-configured upstreams, and returns one recombined, wire-correct response. Plan is injected as a system message; orchestration observability (decision, plan, timings) is captured to durable job artifacts.

The proxy reuses the existing seam, so swapping single↔dual or self-hosted↔native is a config edit. It deliberately stays off LiteLLM's orchestration path (077's failure source).

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (config models + validation), aiohttp (the existing `ClaudeCodeShim` reverse-proxy server; reused for `DualModelProxy`), httpx (upstream client calls), structlog (observability). No new external dependencies anticipated.
**Storage**: N/A for coordinare state. Orchestration observability (FR-021) written to the job's existing durable artifact/capture dir (same `capture_dir` mechanism `ClaudeCodeShim` already accepts) — not a new store.
**Testing**: pytest (`.venv/bin/pytest`); coordinare unit at `tests/unit/`, coordinare integration at `tests/integration/`, contract at `tests/contract/`; performer unit at `agent/performer/tests/unit/`.
**Target Platform**: Linux performer container (`coordinare-performer:full`); coordinare daemon on Linux/macOS.
**Project Type**: Two deployable units in one repo — coordinare (`src/coordinare/`) and performer (`agent/performer/`). Config models live in coordinare; the proxy lives in the performer. They share no module (mirrors the 077 attribution duplication constraint).
**Performance Goals** (Principle IV budgets; spec deferred these to plan):
- `strategy: single` MUST add **0** proxy overhead — no proxy process is launched (SC-003).
- `DualModelProxy` per-turn overhead **excluding** upstream model time MUST be < 50 ms (parse → strategy dispatch → assemble), asserted by a benchmark.
- `strategy: always` wall-clock ≈ 2× the single-model upstream latency (one added model call); this is expected and acceptable given the sequential Spark and correctness-first goal. `think_once` amortizes the think call across a stage; `conditional` adds one cheap classifier call only on escalated turns.
**Constraints**:
- Spark serves ~one request at a time (sequential) — the proxy MUST NOT issue think and act concurrently against the same upstream; calls are serialized and each is timeout-bounded (FR-018).
- No secrets/bodies in INFO logs (FR-019, inherited from 073).
- **hermes has no `<PREFIX>_PROVIDER_BASE_URL` override** today, so it cannot be pointed at the proxy; hermes is therefore limited to `strategy: single` until provider routing is added (tracked as a constraint, not in 080 scope).
**Scale/Scope**: 9 lifecycle roles × 7 backends; ~12 in-repo `config.example.*.yaml` files to migrate (hard cut). Catalogs are small operator-authored lists (tens of entries).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. The design decomposes into single-responsibility units (`DualModelProxy` transport shell, `OrchestrationStrategy` protocol + 3 impls, `Upstream` client, `DifficultyClassifier`, `ResponseAssembler`); each describable in one sentence. Config models are typed pydantic. No dead code; the hard-cut migration removes the old inline `model:` field rather than leaving a dual path.
- **II. Testing Discipline** — PASS (TDD required). Unit tests per strategy + assembler (format × transport matrix) + config validation; contract tests for the config reference-resolution schema and the proxy wire contract; integration test driving a fake CLI through the proxy. Coverage must not regress below the 90% gate (SC-007 enumerates the matrix).
- **III. User Experience Consistency** — PARTIAL/N-A for core; the proxy is invisible to agents. The catalogs are explicitly designed to back dashboard CRUD forms, but no dashboard UI ships in 080 (config-file only). Error communication: config validation errors MUST be actionable (FR-005/FR-006/FR-008) — that is the user-facing surface and is covered.
- **IV. Performance by Design** — PASS. Measurable budgets defined above and reflected in SC-003 (zero single-mode overhead) and a new proxy-overhead benchmark; sequential-Spark constraint explicit.
- **V. Clarity Before Action** — PASS. Three clarifications recorded in the spec's Clarifications section; one deferred item (multi-model latency budget) is now resolved here. No `NEEDS CLARIFICATION` tags remain.

No violations requiring Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/080-dual-model-orchestration/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── config-catalogs.md      # endpoints/model_endpoints/modes schema + validation rules
│   └── proxy-wire-contract.md  # LLMTurn canonical rep + per-format adapter contract
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
# Coordinare (config unification)
src/coordinare/
├── config.py                      # add Endpoint, ModelEndpoint, Mode models; rework
│                                  #   PerformerRoleConfig (drop inline model) + resolved_role
│                                  #   to resolve via mode→model_endpoint→endpoint
├── config_validation.py           # reference-resolution validation; reject inline model:;
│                                  #   strategy/field-consistency checks; endpoint kind rules
└── services/
    └── http_performer_service.py  # _build_job_payload: emit resolved endpoint(s)+strategy
                                   #   into secrets/metadata for the container

tests/
├── unit/config/                   # endpoints/model_endpoints/modes model + resolution tests
├── unit/test_config_validation.py # reference + hard-cut + consistency validation tests
└── contract/                      # config-catalog reference-resolution contract test

# Performer (dual-model proxy)
agent/performer/src/performer/
├── proxy/                         # NEW package (generalizes claude_code_shim)
│   ├── dual_model_proxy.py        # reverse-proxy transport shell (aiohttp, 127.0.0.1:0)
│   ├── llm_turn.py                # canonical LLMTurn rep
│   ├── upstreams.py               # Upstream client + per-format (anthropic/openai) adapters
│   ├── assembler.py               # ResponseAssembler (JSON + SSE, plan merge)
│   ├── classifier.py              # DifficultyClassifier (conditional)
│   └── strategies.py              # OrchestrationStrategy protocol + single/always/conditional/think_once
└── backends/                      # each backend that supports an override: point provider
                                   #   base_url at the proxy when strategy != single
                                   #   (claude_code already does this for the shim)

agent/performer/tests/unit/
├── proxy/                         # strategy, assembler, upstream, classifier unit tests
└── ...                            # backend wiring tests (proxy launched iff strategy != single)
```

**Structure Decision**: Reuse the two-unit layout. Config catalogs and resolution are coordinare-only (`src/coordinare/config.py`, `config_validation.py`). The proxy is a new `performer/proxy/` package that generalizes `claude_code_shim.py` (which can later be re-expressed on top of it, but 080 leaves the working shim intact and builds the general proxy beside it). The CLI→proxy wiring reuses each backend's existing `<PREFIX>_PROVIDER_BASE_URL` mechanism.

## Complexity Tracking

> No Constitution violations requiring justification.
