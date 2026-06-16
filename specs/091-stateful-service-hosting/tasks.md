# Tasks: Stateful Service Hosting in the QA Env-Cache

**Branch**: `091-stateful-service-hosting` | **Date**: 2026-06-15
**Input**: Design documents from `/specs/091-stateful-service-hosting/`
**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/), [quickstart.md](./quickstart.md)

**Tests**: INCLUDED — Constitution II (NON-NEGOTIABLE) and plan.md require contract tests for the extended schema, golden-render tests for the rendered scripts, unit tests for init idempotency + the derivation bridge, and an integration test for the score.json → render → start path. Tests are deterministic (assert on rendered script content and idempotency logic — no live DB).

## Conventions

- Run tests with `.venv/bin/pytest`; lint with `.venv/bin/ruff check <files>`.
- All generated shell stays POSIX-safe, never aborts a sourced shell, and `shq`-quotes every interpolated value (contract C-1..C-3).
- `[P]` = parallelizable (different file, no dependency on an incomplete task).
- `[USn]` labels map to the spec's user stories.

---

## Phase 1: Setup

- [X] T001 Confirm the working tree is on branch `091-stateful-service-hosting` and that `.venv/bin/pytest` collects `tests/unit/services/test_service_inference_schema.py`, `tests/unit/services/test_service_inference_templater.py`, `tests/unit/test_env_manifest.py` (note: actual path is `tests/unit/`, not `tests/unit/services/`), and `tests/integration/test_service_inference_manual_override.py` (baseline green) before making changes. *(126 passed.)*

---

## Phase 2: Foundational (BLOCKING — schema is the prerequisite for every story)

**Purpose**: The extended `ServiceEntry`/`ServiceInit` contract is consumed by the templater (US1), the install-derivation bridge (US2), and the manual-override path (US3). It must land first.

- [X] T002 [P] Add the `kind: Literal["generic", "postgres", "redis"] = "generic"` field and the `init: ServiceInit | None = None` field to `ServiceEntry` in `packages/service_inference/src/coordinare_service_inference/schema.py`, with field docstrings matching `contracts/services-manifest.schema.json` (kind selects the coordinare-owned recipe; init carries first-run parameters). Keep both defaults so an entry without them renders identically to today (VR-3, D2).
- [X] T003 Add the new `ServiceInit` pydantic model to `packages/service_inference/src/coordinare_service_inference/schema.py`: `superuser: str` (required), `databases: list[str] = []`, `password_env_var: str | None = None`. Reuse/define identifier-safe (`^[a-z][a-z0-9_]*$`) and env-var-safe (`^[A-Z_][A-Z0-9_]*$`) patterns already present in the module (VR-5, VR-6, VR-7, VR-8). No literal-secret field exists (VR-8). (Depends on T002 being in the same file.)
- [X] T004 Add cross-field validators in `packages/service_inference/src/coordinare_service_inference/schema.py`: a `ServiceEntry` model validator enforcing VR-2 (`init is not None` ⇒ `kind == "postgres"`; declaring `init` on `generic`/`redis` is a load-time error) and VR-1 (closed-`Literal` kind is enforced by the type). Confirm the existing `_unique_service_names`, `_agent_version_safe`, and `_check_external_entries_have_env_vars` validators are unaffected (data-model "ServicesManifest unchanged shape"). (Depends on T002, T003.)
- [X] T005 Update `manifest_json_schema()` / `manifest_json_schema_str()` output in `packages/service_inference/src/coordinare_service_inference/schema.py` so the emitted JSON schema includes `kind` and `init` (and the `init`-requires-`kind=postgres` conditional), matching `contracts/services-manifest.schema.json`. (Depends on T002, T003, T004.)

**Checkpoint**: Schema accepts the extended entry, rejects `init` on a non-initializing kind, and emits a schema matching the contract. Stories can now proceed.

---

## Phase 3: User Story 1 — Stateful service initialized and reachable (Priority: P1) 🎯 MVP

**Goal**: The templater renders a `kind`-keyed, idempotent init phase before launch (postgres: `initdb` → create superuser → create databases), binds the declared port, and `services-health.sh` runs a `kind`-aware readiness probe — turning "Connection refused" into a successful connection. The coordinare owns the recipe; the declaration supplies parameters.

**Independent Test**: Declare a postgres `ServiceEntry` with an `init` block, render via `coordinare_service_inference.templater.render`, and assert the rendered `services-start.sh` performs sentinel-guarded init then launches on the declared port, and `services-health.sh` uses a connect-level probe (per `contracts/services-start.contract.md` C-5..C-12). Re-render/re-run assertions prove idempotency. No live DB.

### Tests for User Story 1 (write first — they encode the contract)

- [X] T006 [P] [US1] In `tests/unit/services/test_service_inference_schema.py` add cases: a valid postgres entry with `init` loads; `init` on `kind="generic"` and `kind="redis"` raises (VR-2); a literal-password key in `init` is rejected (VR-8, `additionalProperties=false`); `superuser`/`databases`/`password_env_var` pattern violations raise (VR-5/6/7); `kind="generic"`+`init=None` is unchanged (VR-3).
- [X] T007 [P] [US1] In `tests/unit/services/test_service_inference_templater.py` add golden/behavior tests asserting contract C-1..C-3 (no `set -e`/`set -u` abort, no non-zero exit on source, every interpolated value `shq`-quoted, no credential literal — only `${<password_env_var>}`).
- [X] T008 [P] [US1] In `tests/unit/services/test_service_inference_templater.py` add the postgres-init render tests: C-5 (init runs before launch: `initdb` → create superuser role → create each database), C-6/C-7 (sentinel `<data_dir>/PG_VERSION` guards `initdb`; role and db creation are independently create-if-missing so a present sentinel does not short-circuit them — convergent on partial init), C-8 (server launches bound to the declared `port`), C-9 (re-uses `_is_running`/`_port_bound` + init sentinel so a second invocation no-ops), C-10 (`data_dir` resolves under the `$XDG_RUNTIME_DIR` services root).
- [X] T009 [P] [US1] In `tests/unit/services/test_service_inference_templater.py` add the unchanged-behavior golden test: `kind in {"generic","redis"}` with `init=None` renders the start block byte-for-byte equivalent to the pre-091 output (C-4, VR-3, FR-013).
- [X] T010 [P] [US1] In `tests/unit/services/test_service_inference_templater.py` add the readiness-probe tests: C-11 (postgres ⇒ `pg_isready`-style connect probe on the declared host/port, not a bare port-bound check; redis/generic retain the existing port/liveness check) and C-12 (a service not reachable within the health budget surfaces an environment-attributed timeout, not a hang).

### Implementation for User Story 1

- [X] T011 [US1] Extend `packages/service_inference/src/coordinare_service_inference/templates/services-start.sh.j2`: at the per-service init point (the "Project-specific init (initdb, etc.)" location), add a `{% if svc.kind == "postgres" %}` block that, when `<data_dir>/PG_VERSION` is absent, runs `initdb` on the `data_dir`, then create-if-missing the `init.superuser` role and each `init.databases` entry; then launch postgres bound to `svc.port` (C-5..C-9). Reference the admin secret only as `${<password_env_var>}` when `init.password_env_var` is set — never a literal (C-3). `shq`-quote every value (C-2). Keep `generic`/`redis` paths untouched (C-4). (Depends on T002–T005.)
- [X] T012 [US1] Ensure `data_dir` for an initializing service resolves under the writable `$XDG_RUNTIME_DIR` services root in `services-start.sh.j2`, never the read-only cache mount (C-10, FR-009, D8). Confirm the init lock/sentinel layering reuses the existing `_is_running`/`_port_bound` guards (C-9, FR-010, D7). (Depends on T011.)
- [X] T013 [US1] Extend `packages/service_inference/src/coordinare_service_inference/templates/services-health.sh.j2`: add a `{% if svc.kind == "postgres" %}` connect-level readiness probe against the declared port on loopback (`pg_isready`-style), retaining the existing port/liveness check for `redis`/`generic` (C-11, FR-004, D6). The probe MUST invoke only binaries guaranteed by the T018 install set (`pg_isready` from the client package) — do not assume any image-baked client. Keep the script source-safe (C-1) and `shq`-quote interpolations (C-2). (Depends on T002–T005.)
- [X] T014 [US1] Wire init/start/readiness failure surfacing in the rendered scripts so each failure carries a reason and is attributable to the environment via the same channel the existing services-start invocation already uses (C-13, FR-005). (Depends on T011, T013.)
- [X] T014b [US1] Extend `packages/service_inference/src/coordinare_service_inference/templates/services-stop.sh.j2`: for `kind == "postgres"` stop the server with a clean `pg_ctl stop -m fast -D <data_dir>` rather than the existing SIGTERM→SIGKILL `_stop_pid` path (which triggers a smart-shutdown stall then a crash-recovery-forcing SIGKILL); retain `_stop_pid` for `redis`/`generic`. Keep source-safe (C-1) and `shq`-quote interpolations (C-2) (C-13b, FR-012). (Depends on T011, T012.)
- [X] T015 [US1] Run `.venv/bin/pytest tests/unit/services/test_service_inference_schema.py tests/unit/services/test_service_inference_templater.py` and `.venv/bin/ruff check` on the changed files until green.

**Checkpoint**: A declared postgres service renders an idempotent init+launch+readiness pipeline; redis/generic render unchanged. US1 is independently testable and is the MVP.

---

## Phase 4: User Story 2 — Env-bootstrap installs the binary into the cache (Priority: P2)

**Goal**: A declared service with a service `kind` contributes a service-binary install item to the env-bootstrap checklist, reusing the existing deb-into-`<cache>/debs/` delivery. The base image gains nothing (FR-006, FR-007, SC-004, D3).

**Independent Test**: Given a manifest with a declared stateful service, the env_manifest derivation produces a service-binary install item and `_build_env_bootstrap_payload` emits it in the bootstrap checklist; given no stateful service, the checklist is unchanged from today.

### Tests for User Story 2

- [X] T016 [P] [US2] In `tests/unit/services/test_env_manifest.py` add tests: a declared service with a service `kind` derives a service-binary install item (C-14, FR-006); a manifest with no stateful service derives no such item and behavior is unchanged (FR-013 / US2 acceptance #3).
- [X] T017 [P] [US2] In `tests/unit/services/test_http_performer_service.py` add a test asserting `_build_env_bootstrap_payload` emits the service-install (deb-into-`<cache>/debs/`) checklist block for a declared stateful service, reusing the existing system-package delivery path, and emits nothing extra when no stateful service is declared (SC-004).

### Implementation for User Story 2

- [X] T018 [US2] Add service-install derivation to `src/coordinare/services/env_manifest.py`: turn a declared `ServiceEntry` with a service `kind` into a service-binary install item alongside the existing language-runtime derivation, keeping one source of truth for "what the bootstrap must install" (D3). For `kind == "postgres"` the derived install set MUST also include the client package providing `pg_isready` (the readiness probe T013 invokes), so the probe binary is guaranteed present in the cache rather than assumed image-baked (C-11, C-14). (Depends on T002–T005.)
- [X] T019 [US2] Extend `_build_env_bootstrap_payload` in `src/coordinare/services/http_performer_service.py` to emit the derived service-install item into the existing SYSTEM PACKAGES / deb block (slotting after the pinned-language-runtime block, before the activate block), reusing the deb-into-`<cache>/debs/` delivery — no new delivery mechanism, base image untouched (FR-006, FR-007, C-14). (Depends on T018.)
- [X] T020 [US2] Run `.venv/bin/pytest tests/unit/services/test_env_manifest.py tests/unit/services/test_http_performer_service.py` and `.venv/bin/ruff check` on the changed files until green.

**Checkpoint**: A declared service makes the bootstrap fetch the binary into the cache; image stays agnostic. US1 + US2 = end-to-end on a fresh project.

---

## Phase 5: User Story 3 — Durable declaration trusted over inference (Priority: P3)

**Goal**: The `.coordinare/score.json` manual-override path expresses and hosts stateful services verbatim, taking precedence over LLM inference (even when inference would return `services: []`), and init/start/readiness failures are environment-attributed (FR-008, FR-005, SC-005, reusing spec-088 wiring).

**Independent Test**: Provide a manual-override score.json declaring a stateful postgres service; confirm `apply_manual_override` yields the declared service verbatim (agent_version forced to `manual-override`), it survives end-to-end into a rendered services-start, and a simulated init/start/readiness failure is attributed to the environment.

### Tests for User Story 3

- [X] T021 [P] [US3] In `tests/integration/test_service_inference_manual_override.py` add the end-to-end case: a `.coordinare/score.json` with `agent_version="manual-override"` declaring a postgres service with an `init` block flows through `apply_manual_override` → `render` → `services-start.sh` containing the postgres init block, and is preferred over (empty) LLM inference (FR-008, SC-005, quickstart §1/§3).
- [X] T022 [P] [US3] In `tests/integration/test_service_inference_manual_override.py` (or the nearest existing attribution test) add a case asserting an init/start/readiness failure for a declared stateful service is surfaced as environment-attributed, not code-under-test (FR-005, SC-003, US3 acceptance #3).

### Implementation for User Story 3

- [X] T023 [US3] Confirm `apply_manual_override` in `packages/service_inference/src/coordinare_service_inference/manual_override.py` carries the new `kind`/`init` fields through verbatim (it parses score.json into `ServicesManifest`, so this should follow from Phase 2); add handling only if a field is dropped. Verify precedence over inference is unchanged (`agent_version="manual-override"`). (Depends on T002–T005.)
- [X] T024 [US3] Confirm the env-attribution channel used by `_start_env_cache_services` / `_run_env_cache_health_check` in `src/coordinare/services/workspace.py` covers the new init/readiness failure points (reuse spec-088 wiring; no new attribution mechanism). Add wiring only if a new failure point bypasses the existing channel (C-13, FR-005). (Depends on T014, T013.)
- [X] T025 [US3] Run `.venv/bin/pytest tests/integration/test_service_inference_manual_override.py` and `.venv/bin/ruff check` on the changed files until green.

**Checkpoint**: A durable declaration reliably hosts stateful services on every run; failures are environment-attributed. All three stories complete.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T026 [P] Validate the quickstart locally: run the four `.venv/bin/pytest` commands listed in `quickstart.md` §"Validate locally" and confirm each passes.
- [X] T027 [P] Run the full suite `.venv/bin/pytest` and confirm coverage does not regress (Constitution II).
- [X] T028 [P] Run `.venv/bin/ruff check` across all changed files (`packages/service_inference/...`, `src/coordinare/services/env_manifest.py`, `src/coordinare/services/http_performer_service.py`, and the touched tests) and resolve any findings.
- [X] T029 Re-read `contracts/services-start.contract.md` C-1..C-14 (including C-13b kind-aware teardown) against the final rendered output and confirm every invariant is asserted by a test; close any gap inline (no follow-ups).

---

## Dependencies & Execution Order

- **Setup (Phase 1)** → **Foundational (Phase 2)** must complete before any user story.
- **Phase 2 internal order**: T002 → T003 → T004 → T005 (same file, sequential).
- **User stories after Phase 2**:
  - **US1 (Phase 3, P1)** depends only on Phase 2 — the MVP.
  - **US2 (Phase 4, P2)** depends only on Phase 2 — independent of US1 (different files: coordinare `env_manifest`/`http_performer_service`).
  - **US3 (Phase 5, P3)** depends on Phase 2 for the schema; its failure-attribution confirmation (T024) depends on US1's rendered failure points (T013/T014).
- **Polish (Phase 6)** after all targeted stories.

## Parallel Opportunities

- **Phase 2**: T002 is `[P]` to start; T003–T005 follow in the same file.
- **US1 tests**: T006–T010 are all `[P]` (T006 edits the schema test file; T007–T010 edit the templater test file — keep T007–T010 grouped if one author, or split the file by `pytest` class).
- **Across stories**: once Phase 2 is done, US1 (Phase 3) and US2 (Phase 4) can be built in parallel by different people — they touch disjoint files.
- **Polish**: T026, T027, T028 are `[P]`.

## Implementation Strategy

- **MVP = US1 (Phase 3)**: delivering only US1 turns the hard "Connection refused" failure into a successful connection on a cache that already contains the binary — the core value. Ship/validate this first.
- **Incremental**: add US2 (Phase 4) to make it self-contained on a fresh project (bootstrap fetches the binary), then US3 (Phase 5) for the reliability guarantee (durable declaration trusted over inference). Each story is independently testable at its checkpoint.

---

## Summary

- **Total tasks**: 30 (T001–T029 + T014b)
- **Per phase**: Setup 1 (T001) · Foundational 4 (T002–T005) · US1 11 (T006–T015 + T014b) · US2 5 (T016–T020) · US3 5 (T021–T025) · Polish 4 (T026–T029)
- **Per user story**: US1 = 11, US2 = 5, US3 = 5
- **FR-012 (teardown)** → T014b (C-13b kind-aware `pg_ctl stop -m fast`)
- **Test tasks**: 9 (T006–T010, T016–T017, T021–T022) — contract/schema, golden-render + idempotency, derivation bridge, integration
- **MVP scope**: User Story 1 (Phase 3)
