# Tasks: Env-Bootstrap Delivers Runnable Service Server Binaries

**Input**: Design documents from `/specs/102-service-server-package-install/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/service-server-install.md, quickstart.md

**Tests**: INCLUDED — Constitution II (TDD).

**Organization**: by user story (US1 P1 robust closure fetch → US2 P2 version-resilient → US3 P3 general). Coordinare-side persona-render fix; reuses the 091 mapping + spec-101 gate. **No new deps, no schema change.**

## Path Conventions

Coordinare: `src/coordinare/services/`. Tests: `tests/`.

---

## Phase 1: Setup

- [X] T001 Confirm injection point by reading `src/coordinare/services/http_performer_service.py` `_render_system_services_install` (~L868-912: the fragile `apt-get download $(apt-cache depends --recurse … -i <pkgs> | grep '^\w')` instruction + the `pkgs` derivation from `derive_service_install_items`); `src/coordinare/services/env_manifest.py` `_SERVICE_KIND_PACKAGES` (postgres/redis sets — unchanged). Note the exact string to replace + the existing persona-render test(s).

---

## Phase 2: User Story 1 — Robust closure fetch delivers the server (Priority: P1) 🎯 MVP

**Goal**: the rendered service-install instruction uses a closure-resolving download so the versioned server (postgresql-NN) lands, not just the meta.

- [X] T002 [US1] Write FAILING tests `tests/unit/services/test_service_server_install_render.py` (find existing `_render_system_services_install` tests first; extend/mirror): the rendered block for a declared postgres service (a) uses `apt-get install` with `--download-only` and `-o Dir::Cache::archives=<cache>/debs/` (closure-resolving), (b) does NOT use the fragile `apt-cache depends … | grep` download pipeline, (c) names both `postgresql` and `postgresql-client`, (d) still instructs `dpkg-deb -x` extraction. MUST fail before T003.
- [X] T003 [US1] In `http_performer_service._render_system_services_install`, replace the `apt-get download $(apt-cache depends …|grep)` command with `apt-get update && apt-get install -y --download-only -o Dir::Cache::archives={cache}/debs/ {pkgs}` (then the existing extraction). Keep the rest of the block (intro + extract + "coordinare owns the recipe"). Make T002 pass.

**Checkpoint**: US1 — the rendered fetch resolves the meta→server closure; the MVP that makes the server binaries actually land.

---

## Phase 3: User Story 2 — Version-resilient (Priority: P2)

**Goal**: no hard-pinned distro version; apt resolves the current server.

- [X] T004 [US2] Write FAILING test in the render test file: the rendered block contains NO hard-coded versioned server literal (no `postgresql-17`/`postgresql-NN` numeric pin) — it names only the version-agnostic meta (`postgresql`) so apt resolves the current distro's server. MUST pass with T003 (the meta name is retained).

---

## Phase 4: User Story 3 — General across kinds + no-services unchanged (Priority: P3)

**Goal**: the fix renders for any coordinare-known kind; no-services is byte-for-byte unchanged.

- [X] T005 [US3] Write tests: (a) the same closure-resolving command renders for a `redis` service (names `redis-server`); (b) NO declared services → `_render_system_services_install` returns "" (empty, unchanged); (c) a mixed/external-required service derives nothing extra. Make pass.

---

## Phase 5: Polish & Cross-Cutting

- [X] T006 [P] Full coordinare suite green (http_performer_service / env_manifest / persona-render suites); confirm no regression to the 091 service-install derivation or the rest of the env_bootstrap persona.
- [X] T007 `.venv/bin/ruff check` on edited files; walk `quickstart.md` A–F; confirm each SC has a covering test; verify the instruction is secret-free + no new dependency.
- [ ] T008 A couple of adversarial review rounds (diverse-lens + refute-verify) before merge — focus: the apt command actually resolves the closure (correctness of `--download-only` + `Dir::Cache::archives`), version-resilience (no pin), no-services-unchanged/no-regression, the extraction still works with the new download, redis/other-kind generality, secret-free.

---

## Dependencies & Execution Order

- **US1 (P1)** = MVP (robust closure command). 
- **US2 (P2)** = version-resilience (retained meta name; a test guard).
- **US3 (P3)** = generality + no-services-unchanged.
- **Polish** last.

## Implementation Strategy

MVP-first: **US1** — replace the fragile fetch pipeline with `apt-get install --download-only` closure resolution in the one rendered instruction. That alone makes the versioned `postgresql-NN` server land so the binaries exist (and spec-101's gate then passes). **US2/US3** are test-guards on the same change (no version pin; renders for every kind; no-services unchanged). It's a focused coordinare-side persona-render fix; the performer executes the robust command, and spec-101 is the runtime backstop.
