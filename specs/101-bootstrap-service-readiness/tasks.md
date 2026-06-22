# Tasks: Env-Bootstrap Service-Readiness Completion Gate

**Input**: Design documents from `/specs/101-bootstrap-service-readiness/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/service-readiness-gate.md, quickstart.md

**Tests**: INCLUDED — Constitution II (TDD, non-negotiable).

**Organization**: by user story (US1 P1 fail-on-unconnectable → US2 P1 complete-on-connectable / no-regression → US3 P2 observability). Performer-side gate; reuses 091 scripts + the 088/093/095 path. **No new deps, no breaking schema change.**

## Path Conventions

Performer: `agent/performer/src/performer/`. Performer tests: `agent/performer/tests/`. Coordinare: `src/coordinare/`, tests `tests/`.

---

## Phase 1: Setup

- [X] T001 Confirm injection points by reading: `agent/performer/src/performer/main.py` env_bootstrap completion (`if perf.role == "env_bootstrap"`, ~L2810-2891: verify.sh gate, `_run_service_inference`, the `env_bootstrap_complete` returns); `agent/performer/src/performer/workspace.py` (`run_env_cache_verify`, `run_services_start`/services-start.sh invocation ~L424, the services-health non-zero flag ~L356, `consume_services_start_failure` ~L394); `_run_service_inference` return shape (`inference_succeeded`/`inference_services`/`services.json.rejected`); `EnvCacheState.declared_services` (`src/coordinare/models/env_cache.py`); `on_bootstrap_complete(success=False)` (`src/coordinare/services/env_cache.py`). Note exactly where the readiness gate hooks + the required-vs-optional source.

---

## Phase 2: Foundational (the readiness helper — blocks the gate)

- [X] T002 Write FAILING test `agent/performer/tests/unit/test_service_readiness.py` for a `run_service_readiness(env_cache_path, cache_env, declared_services, inference_result)` helper (in workspace.py): returns a result {ok: bool, failures: [{service, reason}]} where — (a) no declared services → ok=True (no-op); (b) required services declared + inference rejected/empty → ok=False, failure reason mentions the service + "manifest rejected"; (c) required services + services-start/health succeed (stubbed) → ok=True; (d) required service + services-health non-zero (stubbed) → ok=False naming the service; (e) optional service failing → ok=True (warn). MUST fail before T003.
- [X] T003 Implement `run_service_readiness(...)` in `agent/performer/src/performer/workspace.py`: for required declared services, if no usable manifest (inference not succeeded / rejected) return ok=False (manifest-rejected reason); else run `services-start.sh` then `services-health.sh` (reuse `run_services_start` + the services-health invocation/flag) bounded with a brief retry, return ok=False naming any required service whose start/health fails; optional services warn-and-continue. Secret-free reasons. Make T002 pass.

**Checkpoint**: the readiness decision is unit-tested in isolation (stubbed scripts).

---

## Phase 3: User Story 1 — Unconnectable/required-rejected bootstrap fails (Priority: P1) 🎯 MVP

**Goal**: at env_bootstrap completion, a required service that isn't connectable (or a rejected manifest with required services) makes the bootstrap return error, not complete.

- [X] T004 [US1] Write FAILING test `agent/performer/tests/unit/test_main_bootstrap_readiness.py` (mirror existing main.py bootstrap-path tests): with `perf.role=="env_bootstrap"`, verify.sh passing, required services declared, and (a) `_run_service_inference` returning rejected/`inference_succeeded=False` → the bootstrap returns `PerformerResponse(status="error", ...)` whose reason names the service + "manifest rejected", NOT `env_bootstrap_complete`; (b) inference succeeded but `run_service_readiness` reports a required service not connectable → status="error" naming the service. MUST fail before T005.
- [X] T005 [US1] In `agent/performer/src/performer/main.py` env_bootstrap completion: after `_run_service_inference`, call `run_service_readiness(...)` with the declared services + inference result; if not ok → set `perf.state="error"`, `perf.error_reason="env-cache service not connectable: <…>"`, and `return PerformerResponse(status="error", reason=…)` (mirror the verify.sh-failure return at ~L2825-2840). Only reach `env_bootstrap_complete` when readiness ok. Make T004 pass.

**Checkpoint**: US1 — a broken required service fails the bootstrap; the MVP that stops "ready cache, dead DB".

---

## Phase 4: User Story 2 — Connectable completes / no-services unchanged (Priority: P1)

**Goal**: required services connectable → complete; service-less symphony → byte-for-byte unchanged.

- [X] T006 [US2] Write FAILING/REGRESSION tests in `test_main_bootstrap_readiness.py`: (a) required services declared + `run_service_readiness` ok → `env_bootstrap_complete` (with inference_state, as before); (b) NO declared services → `run_service_readiness` no-op (ok) → `env_bootstrap_complete` exactly as today (gate adds nothing); (c) the existing service-inference TIMEOUT path still returns complete (best-effort) UNLESS required services are declared-and-unverified — confirm timeout behavior is preserved for no-services and gated only when required services exist. MUST pass with T005.
- [X] T007 [US2] Verify no regression to the existing env_bootstrap tests (verify.sh pass/fail, inference timeout, inference success). Run the existing performer main bootstrap suite; all green. Adjust only if the gate changed a no-services path (it must not).

**Checkpoint**: US2 — green path + service-less symphonies unaffected.

---

## Phase 5: User Story 3 — Observable + coordinare no-dispatch (Priority: P2)

**Goal**: per-service readiness is logged secret-free; a readiness failure flows through the existing bootstrap-failure → cache-not-ready path.

- [X] T008 [US3] Add a secret-free `env_bootstrap.service_readiness` log record (service/required/installed/started/connectable/reason/outcome — NO secrets) at the gate; test (structlog capture) the fields + absence of any secret value (FR-008/SC-004).
- [X] T009 [US3] Coordinare-side test (`tests/`) that a bootstrap `status="error"` from a service-readiness failure flows through `on_bootstrap_complete(success=False)` → `readme_sha=None`, `cache_dir_ready` not set → cache not ready/dispatchable (reusing the verify.sh-failure handling; no new coordinare code expected). Confirm 093/088 behavior unchanged for the success path.

**Checkpoint**: US3 — operator-visible + cards not dispatched into a broken env.

---

## Phase 6: Polish & Cross-Cutting

- [X] T010 [P] Bounded-check test: `run_service_readiness` honors a timeout + brief retry (a slow-but-healthy service isn't false-failed; a down one surfaces within the bound, never hangs) (FR-006/SC-005).
- [X] T011 [P] Full performer suite + the coordinare env_cache/bootstrap suites green; confirm no regression to 088/091/093 behavior.
- [X] T012 `.venv/bin/ruff check` on all edited files; walk `quickstart.md` A–F; confirm each SC has a covering test; verify records secret-free + no new dependency.
- [X] T013 A couple of adversarial review rounds (diverse-lens + refute-verify) before merge — focus: no-services/no-regression (the gate must be a strict no-op when nothing is declared), required-vs-optional handling, the inference-timeout interaction (don't fail a no-services timeout), rejected-manifest detection correctness, bounded/no-hang, secret-free reasons, and that the failure correctly rides on_bootstrap_complete(success=False) (cache not left half-ready).

---

## Dependencies & Execution Order

- **Phase 2 (readiness helper)** blocks the gate.
- **US1 (P1)** = MVP (fail on unconnectable/rejected). Depends on Phase 2.
- **US2 (P1)** = green path + no-regression; same files as US1.
- **US3 (P2)** = observability + coordinare no-dispatch (reuses existing path).
- **Polish** last.

## Parallel Opportunities

- T010 / T011 [P] independent checks.
- US3 coordinare test (T009) parallel with performer gate work (different trees).

## Implementation Strategy

MVP-first: Phase 2 (`run_service_readiness` helper) + **US1** (wire it into the env_bootstrap completion to fail on an unconnectable required service / rejected manifest) — that alone closes the "ready cache, dead DB" hole, reusing the 091 scripts + the existing verify-failure→not-ready path. **US2** guards the green path + the critical no-services/no-regression invariant (the gate must be inert when nothing is declared). **US3** adds the secret-free signal + confirms the coordinare refuses to dispatch into an unready cache. The website Postgres deb-selection fix stays out of scope — this gate makes that gap *fail loudly* instead of silently completing.
