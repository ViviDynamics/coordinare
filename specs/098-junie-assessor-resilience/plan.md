# Implementation Plan: Junie Assessor Resilience

**Branch**: `098-junie-assessor-resilience` | **Date**: 2026-06-20 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/098-junie-assessor-resilience/spec.md`

## Summary

The assessor (junie) terminal-blocks a card whenever its strict parser can't build `issue.md` from a flaky upstream response (empty answer / control-chars / empty body — reproduced ~3–5 of 8). The fix is mostly **classification + reuse**, not new machinery:

- **US1 (MVP):** junie's parse/format/empty failure currently falls through `monitor_performer`'s error handling to the **default block** because its reason isn't recognized. Tag it as a backend-format / transient error so it routes through the **existing retryable system-error path** (`monitor_performer.py:3771` → `system_error_count` budget → blocks only after N consecutive fails). One flaky response → retry, not block. **No new state** (reuses `system_error_count`).
- **US2:** route junie's upstream through a `normalize`-mode `SelfHostedShim` (the existing `proxy/launch.py` launcher) instead of Ollama-direct, adding a **control-char-stripping normalizer** alongside the existing reasoning-strip/promote (#130) + envelope-completion (082). Most flaky responses become clean → retries rarely fire.
- **US3:** when the retries exhaust on **empty-body** failures (model overloaded/down), surface the block as **ENV_BLOCKED** (spec-095) — infrastructure, operator-actionable — not a card-fault terminal error.
- Plus shape-tagged, secret-free observability.

## Technical Context

**Language/Version**: Python 3.14 (project min 3.12; prod 3.14.5 via uv)  
**Primary Dependencies**: existing `monitor_performer` error path (`_FORMAT_ERROR_PREFIX`, `_is_transient_backend_error`, `handle_system_error`, `system_error_count`); the junie backend (`agent/performer/src/performer/backends/junie.py`) + reason extraction in `http_performer_service`; the `proxy/launch.py` `normalize`-mode `SelfHostedShim` + `proxy/normalizers/` (078/#130); the spec-095 ENV_BLOCKED classification. **No new external dependencies.**  
**Storage**: single-host single-process JSON snapshot via `state_store.py`. **No schema change** — US1 reuses the existing persisted `system_error_count`; no new per-card field.  
**Testing**: pytest (`.venv/bin/pytest`) for coordinare classification/retry/ENV-BLOCKED; performer proxy tests for the new control-char normalizer.  
**Target Platform**: Linux/macOS daemon + performer container.  
**Project Type**: single project (coordinare + performer).  
**Performance Goals**: a clean first-try assessment is unchanged (no extra attempts/latency); retries are bounded by the existing system-error budget.  
**Constraints**: secret-free (no raw model output in logs/state); no change to other stages' paths; bounded retries.  
**Scale/Scope**: assessor stage only.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. US1 is a classification tweak reusing the existing retry path; US2 adds one focused normalizer + a launch wiring; US3 reuses 095. No new deps, no new state.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. TDD: failing tests for (US1) junie format/empty reason → routed to system-error retry not block, and blocks only after the budget; (US2) control-char normalizer strips + reasoning-promote + envelope; (US3) exhausted empty-body → ENV_BLOCKED; (obs) shape-tagged secret-free record.
- **III. User Experience Consistency** — PASS. Reuses the system-error backoff/notify surface + 095 ENV_BLOCKED operator surface.
- **IV. Performance by Design** — PASS. Clean responses unchanged; the shim is a loopback already used by other backends.
- **V. Clarity Before Action** — PASS. Failure reproduced live; injection points identified; the one open detail (tag-at-source vs match-in-classifier) is resolved in research.md.

No violations → Complexity Tracking omitted.

## Project Structure

### Documentation (this feature)

```text
specs/098-junie-assessor-resilience/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── junie-resilience.md
└── tasks.md            # /speckit.tasks — not created here
```

### Source Code (repository root)

```text
src/coordinare/
├── graph/nodes/monitor_performer.py   # US1: recognize junie parse/format/empty reason → existing
│                                       #   transient/system-error retry branch (3771); US3: exhausted
│                                       #   empty-body → ENV_BLOCKED classification; shape observability
└── services/http_performer_service.py # US1: tag junie "Failed to build issue.md"/empty as
                                        #   BACKEND_FORMAT_ERROR / transient at the reason source

agent/performer/src/performer/
├── proxy/normalizers/                 # US2: NEW control-char-stripping normalizer (+ reuse strip_reasoning)
├── proxy/launch.py                    # US2: launch normalize-mode SelfHostedShim for the junie backend
└── backends/junie.py                  # US2: point JUNIE_PROVIDER_BASE_URL at the shim loopback

tests/
├── unit/graph/nodes/test_monitor_performer_*.py   # US1/US3 classification + retry + ENV_BLOCKED
└── (performer) tests/unit/proxy/test_normalizer_*  # US2 control-char normalizer
```

**Structure Decision**: Single project. US1/US3 are coordinare-side classification reusing existing retry + 095; US2 is performer-side (normalizer + shim wiring). No persistence change.

## Phase 0 — Research

See [research.md](research.md): tag-at-source vs extend-`_is_transient_backend_error`; reuse `system_error_count` vs a new assessor counter; how `normalize`-mode shim fronts Ollama for junie; control-char-strip placement; distinguishing empty-body (→ENV_BLOCKED) from malformed (→retry/block).

## Phase 1 — Design & Contracts

See [data-model.md](data-model.md) (no new persisted state; reuses system_error_count; failure-shape enum), [contracts/junie-resilience.md](contracts/junie-resilience.md) (classification → action decision table + invariants), and [quickstart.md](quickstart.md) (the #169 assessor-block replayed as acceptance scenarios).

## Phase 2 — Tasks

Created by `/speckit.tasks` (not here).
