# Implementation Plan: Containerized Performer Execution

**Branch**: `056-performer-containerization` | **Date**: 2026-04-28 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/056-performer-containerization/spec.md`

## Summary

Add a containerized execution path for performers alongside the existing native subprocess transport. A performer runs as a long-running (`persistent`) or run-on-demand (`ephemeral`) container that exposes an HTTP/SSE job protocol on a configurable port. The coordinare maintains a `PerformerPool` that polls each registered performer for status, dispatches jobs only to idle candidates whose advertised capabilities match the role, falls over to alternates on busy responses, and excludes endpoints that fail health checks past a configurable threshold (default 5). Three published image variants — `full`, `slim-<backend>`, `base` — plus a documented BYO contract let operators trade size against capability. Secrets reach the container via three precedence-ordered sources (job-init payload > env > mounted creds), and the coordinare authenticates to the performer with an optional per-performer bearer token.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: FastAPI + Starlette (performer HTTP server — already used in coordinare's dashboard), httpx (coordinare → performer client — existing), pydantic + pydantic-settings (config + payload models — existing), structlog (logging — existing), Docker Engine API via the local `docker` CLI invoked through `asyncio.create_subprocess_exec` (no new SDK dependency); Playwright + browser binaries baked into `full` image
**Storage**: N/A — performer registry, pool state, and last-known status held in memory; resets on coordinare restart. Existing `CoordinareState` extended with `performer_endpoints: dict[str, PerformerEndpointState]`
**Testing**: pytest (existing); contract tests for the HTTP job protocol under `tests/contract/`; integration tests that boot the performer image via `docker run` and exercise dispatch/cancel/status/stream end-to-end under `tests/integration/`
**Target Platform**: Linux/macOS coordinare host with Docker Engine ≥ 24 reachable locally (or over a Docker-compatible socket); performer images target `linux/amd64` for v1
**Project Type**: single (existing layout — `src/coordinare/` for orchestrator code, `agent/performer/` for the performer image and runtime)
**Performance Goals**: status poll round-trip p95 < 250ms on localhost; job dispatch acknowledgement p95 < 500ms; ephemeral container cold-start to ready ≤ 60s for `slim-*` variants and ≤ 120s for `full` (matching FR-004a default readiness timeout); two persistent performers run two cards in parallel with no serialization (SC-002)
**Constraints**: no regression in subprocess card throughput (SC-008); zero new mandatory dependencies in the coordinare process; secrets MUST never be logged at any level including DEBUG; bearer-token auth optional (dev opt-out) but the disabled state MUST be visible via existing observability surfaces
**Scale/Scope**: v1 targets up to 8 registered performers per coordinare (mix of subprocess, ephemeral, persistent), each handling at most one in-flight job; pool poll cycle aligned with the existing coordinare poll interval (2–10s)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Compliance | Notes |
|---|---|---|
| I. Code Quality First | PASS | Re-uses existing transport abstraction (spec 004); HTTP transport added as a peer of subprocess transport. Performer HTTP server is a single small FastAPI app. No new heavyweight dependencies in the coordinare process. |
| II. Testing Discipline (NON-NEGOTIABLE) | PASS | Plan adds contract tests for the four HTTP endpoints, integration tests that exercise a real container, unit tests for `PerformerPool` selection/fallover/exclusion. TDD applied to the dispatch state machine. |
| III. User Experience Consistency | PASS | No new dashboard area added (FR-013); pool state surfaces through existing dashboard widgets, logs, metrics, and notification channels. Capability mismatches are reported up front (SC-006), preserving the existing actionable-error pattern. |
| IV. Performance by Design | PASS | Performance budgets captured under Performance Goals above and as success criteria SC-002, SC-003, SC-008. Container readiness is bounded (FR-004a). Status polling is constant-time per performer. |
| V. Clarity Before Action | PASS | Five clarifications resolved in `## Clarifications` of spec.md (auth, protocol modes, failure threshold, readiness timeout, capability granularity). No `NEEDS CLARIFICATION` markers remain. |

**Result**: All gates pass. No complexity-tracking entries required.

## Project Structure

### Documentation (this feature)

```text
specs/056-performer-containerization/
├── plan.md              # This file (/speckit.plan output)
├── spec.md              # Feature specification (with Clarifications)
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   └── performer-http.openapi.yaml
└── checklists/
    └── requirements.md
```

### Source Code (repository root)

```text
src/coordinare/
├── transports/
│   ├── __init__.py
│   ├── base.py                       # PerformerTransport ABC (existing — extend if needed)
│   ├── subprocess_transport.py       # existing
│   └── http_transport.py             # NEW — HTTP/SSE client over httpx
├── services/
│   ├── performer_pool.py             # NEW — PerformerPool service: owns `registrations` dict, status poll, capability-aware dispatch, fallover, exclusion
│   └── performer_lifecycle.py        # NEW — start/stop ephemeral containers via docker CLI; manage persistent registrations
├── models/
│   └── performer_endpoint.py         # NEW — pydantic models: PerformerEndpointConfig, PerformerEndpointState, Capabilities
├── config/
│   └── performer_config.py           # extend existing performer config with mode/image/endpoint/auth/secret-source schema
└── graph/nodes/
    └── dispatch_performer.py         # extend dispatch node to consult PerformerPool when mode != subprocess

agent/performer/
├── Dockerfile.full                   # NEW — every backend + universal toolchain + Playwright/browser
├── Dockerfile.slim                   # NEW — parameterized by BACKEND build arg
├── Dockerfile.base                   # NEW — toolchain-only (no agent CLIs)
├── src/performer/
│   ├── server/
│   │   ├── __init__.py               # NEW — FastAPI app factory
│   │   ├── routes.py                 # NEW — /status, /jobs, /jobs/{id}, /jobs/{id}/cancel, /jobs/{id}/stream
│   │   ├── auth.py                   # NEW — bearer-token middleware; optional
│   │   ├── job_runner.py             # NEW — single-slot async job executor wrapping existing performer main loop
│   │   └── secrets.py                # NEW — three-source resolver with deterministic precedence
│   ├── capabilities.py               # NEW — capability detection at startup + advertisement
│   └── main.py                       # extend: add `--serve --port N` mode that boots the FastAPI server
└── tests/
    ├── unit/                         # extend with server unit tests
    ├── contract/                     # NEW — schema-validation tests against contracts/performer-http.openapi.yaml
    └── integration/                  # NEW — boot container, run a fake card end-to-end

tests/                                # coordinare-side
├── unit/services/
│   └── test_performer_pool.py        # NEW
├── unit/transports/
│   └── test_http_transport.py        # NEW
├── contract/
│   └── test_performer_http_contract.py  # NEW — runs against a stub server matching the OpenAPI schema
└── integration/
    └── test_containerized_performer.py  # NEW — gated on Docker availability
```

**Structure Decision**: Single-project layout (matches existing repo). The coordinare side adds three new modules under `src/coordinare/` (transport, pool, lifecycle) plus extensions to dispatch and config. The performer side adds a FastAPI server alongside the existing main loop and ships three Dockerfiles. No frontend/backend split; dashboard surfaces are reused via spec 052/049 hooks.

## Complexity Tracking

No constitution violations to justify.
