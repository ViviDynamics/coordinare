# Implementation Plan: Env-Cache Toolchain-Readiness Dispatch Gate

**Branch**: `093-env-cache-readiness-gate` | **Date**: 2026-06-18 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/093-env-cache-readiness-gate/spec.md`

## Summary

Code-running performer dispatch (qa, implementer, reviewer) can start against an env-cache
whose declared toolchain for the **current spec sha** has not finished building, because the
dispatch-readiness guard keys on `last_bootstrap_succeeded` alone. This plan extends the
existing dispatch guard so readiness is gated on a **real, manifest-driven `verify.sh`
checklist** re-run on every dispatch (toolchain binary resolvable via `activate.sh` + version
match; native extensions loadable; coordinare-managed services RUNNING/healthy; test/qa-only
niceties as non-blocking WARN). The check reuses the existing `_verify_env_cache_clean`
tri-state seam (True/False/None; None never blocks). On FAIL, the cache is kicked back to
env-bootstrap for the current spec sha and the dispatch falls into the existing
`env_cache_not_current` / `bootstrap_in_flight` hold, bounded by the existing
`env_bootstrap_max_attempts` budget so a genuinely-broken environment surfaces an actionable
env-blocked verdict instead of thrashing. All toolchain-specific probing lives in the generated
shell; coordinare Python stays toolchain-agnostic. Secret invariant preserved: only env-var
NAMES and file PATHS in manifest/logs/state.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod on 3.14.5 via uv); generated
readiness shell is POSIX/bash sourced inside the Debian-based performer container.
**Primary Dependencies**: pydantic 2.x (manifest/result/state models + validators); Jinja2
(`StrictUndefined`, custom `shq` shell-quote filter) for the `verify.sh` templater
(`render_verify_sh`); langgraph (coordinare graph nodes — `dispatch_performer`); structlog
(observability, keys/paths only); docker CLI (clean-context container exec for `verify.sh`).
The existing `_verify_env_cache_clean` seam, `EnvCacheService.check_and_trigger` /
`on_bootstrap_complete`, and `env_manifest.render_verify_sh` are reused, not replaced. **No new
external dependencies.**
**Storage**: JSON snapshot via `state_store.py` (existing single-host single-process). **No new
persisted coordinare state** — readiness is re-evaluated each dispatch and never cached across
dispatches (FR-006). Service runtime state (running/healthy) is observed live by `verify.sh`
inside the container, not persisted.
**Testing**: pytest via `.venv/bin/pytest`; ruff via `.venv/bin/ruff check`. Unit tests pin
the `_verify_env_cache_clean` tri-state contract (existing `test_daemon_snapshot_persistence.py`),
the `render_verify_sh` checklist output (services/manifest test suites), and the
`dispatch_performer` readiness-gate decision.
**Target Platform**: Linux performer containers (Debian-based image) orchestrated from a
single-host coordinare (macOS/Linux dev host).
**Project Type**: Single project — `src/coordinare/` (coordinare daemon, graph nodes, services,
models) plus `packages/service_inference/` (manifest inference, unaffected by this change
except as a manifest source).
**Performance Goals**: One clean-context container exec per code-running dispatch; the existing
`_verify_env_cache_clean` exec carries a 300s timeout. Readiness adds no steady-state polling.
**Constraints**:
- **Toolchain-agnostic (NON-NEGOTIABLE)**: no hardcoded rbenv/nvm/asdf in coordinare Python;
  the checklist is derived from the manifest and all toolchain-specific probing lives in the
  generated `activate.sh` / `verify.sh`.
- **Secret invariant (NON-NEGOTIABLE)**: manifest, logs, and persisted state carry only env-var
  NAMES and file PATHS, never literal secret values.
- **Reuse existing seams**: `_verify_env_cache_clean` (tri-state), the `env_cache_not_current` /
  `bootstrap_in_flight` hold, and `env_bootstrap_max_attempts` — no new dispatch path, no new
  budget knob.
**Scale/Scope**: Per-symphony env-caches on one host; a handful of code-running stages per card
lifecycle. The change touches one graph-node decision (`dispatch_performer`), the `verify.sh`
templater (checklist semantics: toolchain version match, native-extension load, service health,
WARN tier), and tests.

## Seam Confirmation (T001/T002) — Drift Record

Confirmed during implementation that all four seams resolve, with these corrections to the
line/signature references the design docs assumed:

1. **`_verify_env_cache_clean` return type**: returns `tuple[bool | None, str]` —
   `(passed, detail)`, not a bare tri-state. The tri-state (`True`/`False`/`None`) is the first
   element; `detail` carries the verify FAIL lines for the dashboard. Body's only `self`
   dependency is `self._state.get("env_cache")`; everything else derives from `symphony_name`
   and `svc`. Located at `daemon.py:1738`.
2. **`ROLE_TO_STAGE` path**: `src/coordinare/lifecycle.py:8` (NOT `src/coordinare/graph/lifecycle.py`).
   Gated code-running stages and the `env_bootstrap` exemption match
   contracts/readiness-gate.md (T002 confirmed).
3. **Dispatch node has no `daemon` handle.** The graph node (`_dispatch_performer_body`) reaches
   no daemon instance, so it cannot call `daemon._verify_env_cache_clean` directly (T009 crux).
   **Resolution**: extract the method body into a module-level free function
   `verify_env_cache_clean(state, symphony_name, svc) -> tuple[bool | None, str]` in
   `services/env_cache.py` (which already imports `asyncio`, `EnvCacheState`,
   `DEFAULT_DEVENV_ROOT`, `Path`, `Any`, and has a `logger`). The daemon method delegates to it
   (`return await verify_env_cache_clean(self._state, symphony_name, svc)`), keeping its
   signature and the `test_daemon_snapshot_persistence.py` tri-state contract unchanged. The
   dispatch node calls the same free function with the `state`, `_symphony_name_for_ec`, and
   `service` handles already in hand. **No new dispatch-payload field and no new state field** —
   a shared pure function over existing seams (single source of truth, no drift between the two
   call sites).
4. `render_verify_sh` and `EnvCacheService.check_and_trigger` resolve as documented (in
   `services/env_manifest.py` and `services/env_cache.py` respectively).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution v1.1.0 — five principles evaluated:

- **I. Code Quality First** — PASS. Extends named existing seams rather than adding parallel
  machinery; toolchain-agnostic constraint keeps coordinare Python free of version-manager
  specifics (the checklist is data-driven from the manifest).
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS (with obligations). New/changed behavior is
  unit-tested: the tri-state gate decision in `dispatch_performer`, the `verify.sh` checklist
  semantics (version-match FAIL, native-extension load FAIL, service-health FAIL, nicety WARN,
  aggregate exit code), and the FAIL→re-bootstrap→budget-exhaustion path. Existing contract tests
  for `_verify_env_cache_clean` (None/True/False) must continue to pass unchanged.
- **III. UX Consistency** — PASS. Readiness lines are human-readable OK/FAIL/WARN (FR-004); the
  env-blocked verdict on budget exhaustion is actionable and consistent with spec-088 wording.
- **IV. Performance by Design** — PASS. One clean-context exec per dispatch under the existing
  300s timeout; no new polling. Performance budget is reflected in SC (single re-run per dispatch).
- **V. Clarity Before Action** — PASS. Spec carries explicit Assumptions & Dependencies bounding
  manifest *realization* (this spec) vs. *completeness* (inference/score.json) vs. 088 backstop.

**Quality Gates**: branch `093-env-cache-readiness-gate` from main ✓; Conventional Commits ✓;
ruff + pytest green before handoff (per Performer CI Ownership).

No violations → Complexity Tracking left empty.

## Project Structure

### Documentation (this feature)

```text
specs/093-env-cache-readiness-gate/
├── plan.md              # This file (/speckit.plan command output)
├── spec.md              # Feature specification
├── checklists/
│   └── requirements.md  # Spec quality checklist (complete)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
src/coordinare/
├── graph/nodes/
│   └── dispatch_performer.py     # THE GATE: extend readiness guard (lines ~973-1000)
│                                 #   so FAIL/degraded keys on verify.sh, not last_bootstrap_succeeded alone
├── daemon.py                     # _verify_env_cache_clean seam (lines ~1738-1812) — reused as-is
├── services/
│   ├── env_cache.py              # check_and_trigger / on_bootstrap_complete — re-bootstrap trigger + budget
│   └── env_manifest.py           # render_verify_sh (~318-361) — checklist semantics (version/load/health/WARN)
├── models/
│   ├── env_manifest.py           # EnvManifest / ManifestItem — readiness-relevant fields
│   └── env_cache.py              # EnvCacheState — no new persisted readiness field (re-run each dispatch)
├── config.py                     # env_bootstrap_max_attempts (line 937) — reused, not changed
└── lifecycle.py                  # ROLE_TO_STAGE — defines code-running stages the gate applies to

packages/service_inference/        # manifest source (score.json override else LLM inference) — unaffected

tests/unit/
├── test_daemon_snapshot_persistence.py   # existing _verify_env_cache_clean tri-state contract (must stay green)
├── graph/nodes/                          # dispatch_performer readiness-gate decision tests
└── services/                             # render_verify_sh checklist-semantics tests
```

**Structure Decision**: Single-project coordinare layout. The feature is an extension of
established seams, not a new subsystem: the gate decision changes in `dispatch_performer.py`,
the checklist semantics change in `env_manifest.render_verify_sh`, and the FAIL→re-bootstrap
wiring reuses `env_cache.py`'s existing trigger/budget path. No new modules, no new persisted
state, no new external dependencies.

## Complexity Tracking

> No constitution violations — section intentionally empty.
