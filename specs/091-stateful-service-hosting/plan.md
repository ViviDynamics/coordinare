# Implementation Plan: Stateful Service Hosting in the QA Env-Cache

**Branch**: `091-stateful-service-hosting` | **Date**: 2026-06-15 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/091-stateful-service-hosting/spec.md`

## Summary

Make the QA env-cache able to host a stateful store (Postgres, Redis, …) that requires first-run initialization, without baking any service into the performer image. Three additive changes, one per user story:

1. **(P1) Init-capable service contract + templater init phase.** Extend `ServiceEntry` with a coordinare-owned service `kind` discriminator and an optional `init` parameter block (admin account, databases). The templater gains a `kind`-keyed, idempotent initialization phase in `services-start.sh` (for `postgres`: `initdb` → create superuser role → create database; for `redis`/`generic`: no init) plus a `kind`-aware readiness probe in `services-health.sh`. The coordinare owns the *recipe*; the durable declaration supplies the *parameters*.
2. **(P2) Bootstrap installs the service binary into the cache.** Bridge the durable services declaration into the env-bootstrap install checklist so the env-bootstrap performer downloads the service `.deb` into the cache (existing system-package delivery path). The base image stays agnostic.
3. **(P3) Durable declaration trusted over inference.** The `.coordinare/score.json` manual-override path (Phase 1) already takes precedence over LLM inference; this story extends that path to express and host stateful services and confirms environment-attribution of init/start/readiness failures (reusing spec-088 wiring).

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv); generated shell is POSIX/bash sourced inside the Debian-based performer.
**Primary Dependencies**: pydantic 2.x (manifest/entry models + validators), Jinja2 (`StrictUndefined`, custom `shq` shell-quote filter) for the services-{start,stop,health}.sh templater, the existing `manual_override` loader, `env_manifest` derivation, and `http_performer_service._build_env_bootstrap_payload` persona builder. No new external dependencies.
**Storage**: JSON snapshot via `state_store.py` (existing single-host single-process). No new persisted coordinare state — service runtime state (initialized/running) is on-disk in the service's `data_dir` (sentinel files) inside the container.
**Testing**: pytest (`.venv/bin/pytest`). Contract tests for the extended schema; golden-output tests for the rendered scripts; unit tests for init idempotency and the derivation bridge; integration test for the manual-override → render → start path.
**Target Platform**: Linux performer container (Debian-based, `python:3.12-slim` base) with the env-cache volume mounted (often read-only); coordinare on Linux/macOS.
**Project Type**: Single project — coordinare (`src/coordinare/`) plus the `packages/service_inference/` package and the `agent/performer/` runtime.
**Performance Goals**: A declared stateful service initializes, starts, and becomes reachable on its connection target within the existing services-start budget (120 s) and health budget (60 s); steady-state re-runs skip init and reach ready in the time of a liveness probe (≈ seconds).
**Constraints**: Generated activation/start scripts MUST stay POSIX-safe and never abort a sourced shell (no `set -e`/`set -u`, no non-zero exit on source) — same contract as `render_activate_sh`. All interpolated values stay shell-quoted via `shq`. Credentials never written to logs/argv. Mutable `data_dir` lives in a writable location (XDG_RUNTIME_DIR), never inside the read-only cache mount.
**Scale/Scope**: Per-project handful of services; single host, single process. Out of scope: tuning, replication, the consuming repo's own `database.yml`/`score.json` alignment.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. All changes are additive and single-responsibility: a new optional `init` model, a `kind` discriminator with a default that preserves current behavior, one `kind`-keyed recipe block per script. No dead code; type annotations on all new public models/functions; the coordinare-owned-recipe boundary keeps product specifics out of the declaration contract.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS (planned). Contract tests for the extended `ServiceEntry`/`ServiceInit`; golden-render tests proving the postgres init block is idempotent (sentinel-guarded) and that `redis`/`generic` render unchanged; unit tests for the install-derivation bridge; an integration test for score.json → render → services-start. Coverage must not regress. Tests are deterministic (no live DB — assert on rendered script content and on idempotency logic).
- **III. User Experience Consistency** — PASS (scoped). No UI. The user-facing surface is operator-authored `score.json` + diagnostic output; error messages for init/start/readiness failures MUST be actionable and environment-attributed (FR-005), consistent with existing services-start diagnostics.
- **IV. Performance by Design** — PASS. Budgets are defined above and as SC-006 (bounded readiness window). Idempotent re-runs skip the expensive `initdb`. No CI benchmark is added (the bound is a correctness timeout, not a tracked perf metric); justification: the operation is environment setup, not a hot path.
- **V. Clarity Before Action** — PASS. The one load-bearing decision (init-recipe ownership) is resolved in research.md: coordinare owns the deterministic recipe keyed by `kind`; the declaration supplies parameters. No `NEEDS CLARIFICATION` markers remain.

No violations — Complexity Tracking table omitted.

## Project Structure

### Documentation (this feature)

```text
specs/091-stateful-service-hosting/
├── plan.md              # This file
├── spec.md              # Feature spec (/speckit.specify)
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── services-manifest.schema.json   # Extended ServicesManifest JSON schema
│   └── services-start.contract.md      # Rendered-script behavior contract
├── checklists/
│   └── requirements.md  # Spec-quality checklist (PASS)
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
packages/service_inference/src/coordinare_service_inference/
├── schema.py                       # ServiceEntry + new ServiceInit model, kind field, validators
├── templater.py                    # render(); kind-aware helpers if needed
└── templates/
    ├── services-start.sh.j2        # NEW kind-keyed idempotent init phase before launch
    ├── services-health.sh.j2       # NEW kind-aware readiness probe (e.g. pg_isready)
    └── services-stop.sh.j2         # (unchanged)

src/coordinare/services/
├── env_manifest.py                 # derive service-install items so bootstrap installs the binary
└── http_performer_service.py       # _build_env_bootstrap_payload: emit service-install checklist block

tests/unit/services/
├── test_service_inference_schema.py            # extend: kind + init validation
├── test_service_inference_templater.py         # extend: postgres init render + idempotency, redis/generic unchanged
└── test_env_manifest.py                        # extend: service-install derivation bridge
tests/integration/
└── test_service_inference_manual_override.py   # extend: stateful score.json → render → start contract
```

**Structure Decision**: Single-project layout. The change is concentrated in the existing `service_inference` package (schema + templates) with a thin bridge in coordinare's `env_manifest`/`http_performer_service` for the install path. No new modules or packages; everything slots into existing files alongside their existing tests.

## Complexity Tracking

> No constitution violations — section intentionally empty.
