# Implementation Plan: Env-Bootstrap Service-Readiness Completion Gate

**Branch**: `101-bootstrap-service-readiness` | **Date**: 2026-06-22 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/101-bootstrap-service-readiness/spec.md`

## Summary

The env_bootstrap completion path (`agent/performer/src/performer/main.py`, `if perf.role == "env_bootstrap"`, ~L2810-2891) runs `verify.sh` (toolchain) then `_run_service_inference` (091 — writes `services.json` + start/stop/health scripts, or `services.json.rejected`), then marks `env_bootstrap_complete` **regardless of whether the declared services are actually up**. Service inference is explicitly "best-effort": a timeout or a rejected manifest does NOT fail the bootstrap. So a cache can be marked ready with no usable database (the website case: server package missing → manifest rejected → nothing started → "connection refused" for every DB test).

This feature inserts a **service-readiness gate** at bootstrap completion:

- After service inference, if the symphony declares **required** services, the bootstrap MUST **start** them (091 `services-start.sh`) and confirm each is **connectable** (091 `services-health.sh` exit 0) within a bounded time.
- A **rejected/empty manifest** when required services are declared, or a required service that won't start/connect, makes the bootstrap **FAIL** (`perf.state = "error"`, reason naming the service) — routing through the existing `on_bootstrap_complete(success=False)` path (same as a `verify.sh` failure) so the cache is **not** marked ready and the condition surfaces (ENV_BLOCKED / 093 gate), instead of dispatching cards into a broken env.
- Symphonies with **no** declared services are unaffected (gate is declaration-driven). Optional services warn-and-continue.

## Technical Context

**Language/Version**: Python 3.14 (project min 3.12; prod 3.14.5 via uv); generated shell sourced in the Debian-based performer.
**Primary Dependencies**: the env_bootstrap completion path (`main.py` `env_bootstrap` branch, `run_env_cache_verify`, `_run_service_inference`); the 091 `services-start.sh`/`services-health.sh` templater + `declared_services` (score.json); `coordinare_service_inference` (manual_override / `services.json` / `.rejected`); the 088 persisted bootstrap-success state + `on_bootstrap_complete`; the 093 readiness/dispatch gate; the 095 ENV_BLOCKED surface. **No new external dependency.**
**Storage**: existing JSON snapshot (`state_store.py`) / `EnvCacheState` (`models/env_cache.py`); optionally extend with per-service readiness (backward-compatible). No new store.
**Testing**: pytest — performer tests for the readiness gate (required service connectable → complete; not connectable / rejected manifest → bootstrap error naming the service; no-services → unchanged; bounded retry/timeout); coordinare tests that a service-readiness bootstrap failure flows through `on_bootstrap_complete(success=False)` → cache not ready (no regression to 088/093).
**Target Platform**: performer container (bootstrap runs there) + coordinare (consumes bootstrap success).
**Project Type**: single project (performer-side gate + coordinare consumes existing success signal).
**Performance Goals**: bootstrap adds a bounded start+health step (seconds, with brief retry); no per-card cost (services already started per-job by 091 downstream).
**Constraints**: secret-free (no DB passwords in reasons/logs); required-service failure blocks, optional warns; bounded (timeout + brief retry); no-services symphonies unchanged.
**Scale/Scope**: the env_bootstrap completion gate only.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. Reuses the 091 start/health scripts + the existing verify-failure → `on_bootstrap_complete(success=False)` path; the new code is a focused readiness step in one completion branch + a small helper. No new deps.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. TDD: failing tests for (US1) required service unconnectable / rejected manifest → bootstrap error naming the service & cache not ready; (US2) connectable → complete, no-services → unchanged; (US3) secret-free per-service readiness record; bounded retry.
- **III. User Experience Consistency** — PASS. Reuses the ENV_BLOCKED / 093 readiness operator surface + the existing bootstrap-failure→retry path.
- **IV. Performance by Design** — PASS. One bounded start+health at bootstrap; no per-card overhead.
- **V. Clarity Before Action** — PASS. Failure reproduced live (website cache: rejected manifest + missing server + no PG_VERSION, yet "complete"); injection point + reused scripts identified; open detail (required-vs-optional source, where to read declared services) resolved in research.md.

No violations → Complexity Tracking omitted.

## Project Structure

### Documentation (this feature)

```text
specs/101-bootstrap-service-readiness/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── service-readiness-gate.md
└── tasks.md            # /speckit.tasks — not created here
```

### Source Code (repository root)

```text
agent/performer/src/performer/
├── main.py        # env_bootstrap completion: after _run_service_inference, run the
│                  #   service-readiness gate for REQUIRED declared services (start +
│                  #   health/connect, bounded); a rejected manifest or unconnectable
│                  #   required service → perf.state="error" (names the service) instead
│                  #   of env_bootstrap_complete. No-services / optional → unchanged.
└── workspace.py   # (likely) a run_service_readiness() helper running services-start.sh
                   #   + services-health.sh with bounded retry, mirroring run_env_cache_verify

src/coordinare/
└── (on_bootstrap_complete(success=False) + 093 gate + 088 state — reused; verify a
    service-readiness failure is handled identically to a verify.sh failure)

agent/performer/tests/ + tests/   # gate behavior + coordinare no-dispatch on failure
```

**Structure Decision**: Single project. The gate is performer-side at bootstrap completion (where services + their scripts live); the coordinare reuses its existing bootstrap-failure handling (no new coordinare mechanism — verify it treats a service-readiness failure like a verify failure).

## Phase 0 — Research

See [research.md](research.md): where/how declared **required** services are known at bootstrap (score.json `declared_services` / manifest `services` + a required flag, default required); reusing 091 `services-start.sh`/`services-health.sh` as the start+connect mechanism vs a bespoke probe; treating a rejected/empty manifest as a required-service failure; how the failure rides the existing `on_bootstrap_complete(success=False)` → cache-not-ready/ENV_BLOCKED path; bounded retry/timeout; secret-free reasons.

## Phase 1 — Design & Contracts

See [data-model.md](data-model.md) (declared service + required flag; per-service readiness result; the reused bootstrap-success/cache-ready state), [contracts/service-readiness-gate.md](contracts/service-readiness-gate.md) (the gate decision table + invariants), and [quickstart.md](quickstart.md) (the website Postgres failure replayed as acceptance scenarios).

## Phase 2 — Tasks

Created by `/speckit.tasks` (not here).
