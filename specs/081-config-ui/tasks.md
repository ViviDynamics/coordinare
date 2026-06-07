---
description: "Task list for 081-config-ui implementation"
---

# Tasks: Live Config Editing in the Dashboard UI

**Input**: Design documents from `/specs/081-config-ui/`
**Prerequisites**: plan.md (required), spec.md (user stories), research.md, data-model.md, contracts/config-api.md, quickstart.md

**Tests**: INCLUDED. Constitution v1.1.0 Principle II (Testing Discipline) is NON-NEGOTIABLE — unit, contract, and integration tests are mandatory even though the spec did not explicitly request TDD. Coverage must not decrease; test names are behavior-based; tests are deterministic (file fixtures, no live performer).

**Organization**: Tasks grouped by user story (US1 P1, US2 P2, US3 P3) so each is independently implementable and testable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1 / US2 / US3 for user-story phases; Setup/Foundational/Polish carry no story label
- Exact file paths included in every task

## Path Conventions

- Coordinare source: `src/coordinare/`
- Coordinare tests: `tests/unit/` (run with `.venv/bin/pytest`; coordinare and performer suites run **separately** — conftest collision)
- Performer spec-078 models reused from `agent/performer/src/performer/proxy/routing.py`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish the module skeletons and shared test fixtures all phases depend on.

- [X] T001 Create `src/coordinare/config_descriptors.py` module skeleton (module docstring, public `build_snapshot()` / `build_section()` stubs, typed signatures) per plan.md Structure Decision
- [X] T002 [P] Create `src/coordinare/services/config_write_service.py` module skeleton (atomic-write + optimistic-concurrency stubs, typed signatures) reusing the persona-service atomic pattern referenced in plan.md
- [X] T003 [P] Create `src/coordinare/routing_config_service.py` module skeleton (locate/read/write routing YAML stubs, typed signatures) per research D2
- [X] T004 [P] Add shared pytest fixtures in `tests/unit/conftest.py` (or local fixtures) for a representative temp `config.yaml`, a temp routing YAML, and a performer-endpoint config that mounts the routing YAML; ensure coordinare-suite isolation (no performer conftest import)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Cross-cutting machinery every user story relies on — the per-field annotation table, secret masking, content-hashing, and the atomic write-back primitive. No user story can be completed until these exist.

**⚠️ CRITICAL**: US1/US2/US3 all consume these; complete before starting Phase 3.

- [X] T005 Implement the per-field **annotation table** in `src/coordinare/config_descriptors.py` keyed by dotted path, carrying `editable` / `restart_required` / `secret` flags for every config field (research D1, D3, D7); cover ProjectConfiguration, CoordinareConfiguration, PersonasConfig, PerformersConfig, Endpoint, ModelEndpoint, Mode, SymphonyConfig
- [X] T006 Implement pydantic-model introspection in `src/coordinare/config_descriptors.py`: derive `type`, `label`, `help` (`Field(description=...)`), `default`, `range` (ge/le/gt/lt, min/max_length), `enum` (`Literal`/`Enum`) via `model_fields` / `model_json_schema()` into a `ConfigSetting` descriptor (data-model E1)
- [X] T007 Implement **secret masking + `${VAR}` literal preservation** in `src/coordinare/config_descriptors.py`: mask secret-flagged literal values to `••••••`, return `${VAR}` placeholders as raw literals, set `is_env_placeholder` (research D3); single choke-point serializer
- [X] T008 [P] Implement SHA-256 **content-hash** helper and the `If-Match`-style **optimistic-concurrency guard** in `src/coordinare/services/config_write_service.py` (re-read + recompute + 409-on-mismatch, immediately before swap) (research D4)
- [X] T009 [P] Implement the **atomic write-back** primitive in `src/coordinare/services/config_write_service.py`: `tempfile.mkstemp(dir=parent)` → `yaml.safe_dump` → `os.chmod` (preserve mode) → `os.replace`, shared by config.yaml and routing-YAML writers (research D5)
- [X] T010 [P] [Foundational tests] Unit tests for masking, `${VAR}` preservation, content-hash determinism, and concurrency-guard rejection in `tests/unit/test_config_descriptors.py` and `tests/unit/test_config_write_service.py`

**Checkpoint**: Descriptor derivation, masking, hashing, concurrency guard, and atomic write all exist and are unit-tested.

---

## Phase 3: User Story 1 - See and understand the whole configuration (Priority: P1) 🎯 MVP

**Goal**: An operator opens the Config view and sees the entire editable surface — global tuning, personas, symphonies, the spec-080 catalogs (endpoints/model_endpoints/modes), and the routing table — each setting with label, help, type, current value, default, range/enum, with editable vs read-only and secrets masked.

**Independent Test**: `GET /api/config/all` returns all 7 sections with descriptors and `content_hashes`; secrets masked, `${VAR}` raw; the dashboard Config view renders every section (quickstart §1). Verifiable with no write performed.

### Tests for User Story 1 ⚠️ (write first, ensure they FAIL)

- [X] T011 [P] [US1] Contract test for `GET /api/config/all` and `GET /api/config/section/{id}` (shape, all 7 sections present, masking, content_hashes, 404 on unknown section) in `tests/unit/test_dashboard_config_api.py`
- [X] T012 [P] [US1] Unit tests for section grouping (7 sections per data-model E2) and CatalogItem `referenced_by`/`deletable` projection in `tests/unit/test_config_descriptors.py`
- [X] T051 [P] [US1] Test invalid-on-disk handling in `tests/unit/test_config_descriptors.py`: given a `config.yaml` with one field failing validation, assert `GET /api/config/all` returns 200 with that field rendered read-only + an "invalid value on disk" marker and a section-level banner, while valid fields render normally (edge case: partial/invalid existing config, research D8)

### Implementation for User Story 1

- [X] T013 [US1] Implement `ConfigSection` grouping + `ConfigSnapshot` assembly in `src/coordinare/config_descriptors.py` (7 sections: global, personas, symphonies, endpoints, model_endpoints, modes, routing; data-model E2/E5)
- [X] T014 [US1] Implement `CatalogItem` projection with `referenced_by`/`deletable` derived from `_validate_orchestration_catalogs()` invariants in `src/coordinare/config_descriptors.py` (data-model E3, research D6 read-side)
- [X] T015 [US1] Add `routing_available` detection in `src/coordinare/routing_config_service.py` (resolve host path from performer-endpoint `volumes` + `SELFHOSTED_ROUTING_CONFIG`; false when unmounted) (research D2)
- [X] T016 [US1] Implement `GET /api/config/all` endpoint in `src/coordinare/dashboard.py` returning `ConfigSnapshot` (descriptors + content_hashes + config_version + routing_available); preserve existing endpoints unchanged (contracts §GET /api/config/all)
- [X] T017 [US1] Implement `GET /api/config/section/{section_id}` endpoint in `src/coordinare/dashboard.py` (404 on unknown id, same masking) (contracts §GET section)
- [X] T018 [US1] Add the dashboard **Config view** frontend in `src/coordinare/dashboard.py`: render all 7 sections, label/help/type/current/default/range-enum, visually distinguish editable vs read-only and masked secrets; source visual values from existing design tokens, WCAG 2.1 AA, loading state (Constitution III; quickstart §1)
- [X] T019 [US1] Add an explanatory **read-only empty state** for the routing section when `routing_available` is false (guidance to mount a routing YAML), not an error (contracts §routing; quickstart §5.1)

**Checkpoint**: US1 fully functional — whole config viewable and understandable, secrets safe, no edit path required yet. MVP.

---

## Phase 4: User Story 2 - Edit a configuration value safely and apply it live (Priority: P2)

**Goal**: An operator edits a setting, gets server-side pydantic validation with actionable inline errors, an atomic optimistic-concurrency-guarded write, hot-reload where safe (else flagged restart-required) — including **full CRUD on the spec-080 model catalogs** (FR-018) with referential integrity and delete-protection.

**Independent Test**: Edit a hot-reloadable global → `applied: hot_reloaded`, config_version increments; out-of-range → inline 422, no write; external on-disk edit → 409; catalog create/update/delete with referential integrity + delete-protection (quickstart §2–4).

### Tests for User Story 2 ⚠️ (write first, ensure they FAIL)

- [X] T020 [P] [US2] Contract tests for `PUT /api/config/section/{id}` (200 hot_reloaded, 422 validation, 409 conflict) in `tests/unit/test_dashboard_config_api.py`
- [X] T021 [P] [US2] Contract tests for catalog CRUD `GET/POST/PUT/DELETE /api/config/catalog/{endpoints|model_endpoints|modes}` incl. 422 unknown-reference and 409 referenced (delete-protection) in `tests/unit/test_dashboard_config_api.py`
- [X] T022 [P] [US2] Unit tests for referential-integrity validation + delete-protection (D6) and hot-reload-vs-restart classification (D7) in `tests/unit/test_config_write_service.py`
- [X] T023 [P] [US2] Integration test for the write→reload→reflect cycle (edit global → reload trigger → GET reflects new value + bumped config_version) in `tests/unit/test_dashboard_config_integration.py`

### Implementation for User Story 2

- [X] T024 [US2] Implement section save in `src/coordinare/services/config_write_service.py`: parse → apply `changes` to the model → full pydantic validation of the whole proposed config → atomic write; masked-unchanged secrets treated as no-op (data-model E6, research D3/D5)
- [X] T025 [US2] Implement **referential-integrity validation + delete-protection** for catalog writes in `src/coordinare/services/config_write_service.py` reusing `_validate_orchestration_catalogs()` invariants; reject delete-while-referenced with actionable error naming the referrer (research D6)
- [X] T026 [US2] Implement **hot-reload vs restart-required** decision in `src/coordinare/services/config_write_service.py` from the annotation table; trigger existing `POST /api/config/reload` path on hot-reloadable config.yaml saves, else return `staged_restart` (research D7)
- [X] T027 [US2] Implement `PUT /api/config/section/{section_id}` endpoint in `src/coordinare/dashboard.py` consuming `SaveRequest`, returning `SaveResult` with `applied`/`new_hash`/`errors` (contracts §PUT section)
- [X] T028 [US2] Implement catalog **GET** `/api/config/catalog/{catalog}` in `src/coordinare/dashboard.py` (items + `referenced_by`/`deletable` + content_hash) (contracts §Catalog CRUD)
- [X] T029 [US2] Implement catalog **POST** (create) `/api/config/catalog/{catalog}` in `src/coordinare/dashboard.py` (422 on unknown reference, e.g. model_endpoint.endpoint) (contracts §Catalog CRUD)
- [X] T030 [US2] Implement catalog **PUT** (update) `/api/config/catalog/{catalog}/{id}` in `src/coordinare/dashboard.py` (contracts §Catalog CRUD)
- [X] T031 [US2] Implement catalog **DELETE** `/api/config/catalog/{catalog}/{id}` in `src/coordinare/dashboard.py` (409 `referenced` with referrer name) (contracts §Catalog CRUD, research D6)
- [X] T032 [US2] Map pydantic/validation failures to the secret-free, actionable, stack-trace-free **error model** (409/422/403, `errors[].{key,code,message}`) in `src/coordinare/dashboard.py` (contracts §Error model, Constitution III)
- [X] T033 [US2] Add edit UI to the Config view in `src/coordinare/dashboard.py`: per-field inline validation errors, save-state feedback, hot-reloaded-vs-staged-restart visual distinction, optimistic-concurrency 409 reload-and-reapply prompt, catalog create/edit/delete controls with delete-protection messaging (Constitution III; quickstart §2–4)
- [X] T047 [US2] Reload-failure integrity in `src/coordinare/services/config_write_service.py` and `src/coordinare/dashboard.py`: when `POST /api/config/reload` raises after a successful atomic write, retain the prior in-memory `CoordinareConfiguration` (no partial swap), return `SaveResult{ok:true, applied:"staged_restart"}` with an operator-readable message, and log the failure without secrets (FR-015)
- [X] T048 [P] [US2] Test reload-failure integrity in `tests/unit/test_dashboard_config_integration.py`: inject a reload that raises, assert (1) the in-memory `config_version`/values are unchanged, (2) the on-disk write persisted, (3) response is `applied="staged_restart"` with an actionable, stack-trace-free message (FR-015)

**Checkpoint**: US1 + US2 work — whole config viewable AND safely live-editable, including full spec-080 catalog CRUD with referential integrity.

---

## Phase 5: User Story 3 - Manage the spec-078 self-hosted routing config (Priority: P3)

**Goal**: An operator performs full CRUD on the spec-078 routing table from the dashboard; edits validate against the spec-078 models (normalize/reroute rules) and are written to the host-side YAML the performer mounts; the UI clearly flags "applies to the next performer job".

**Independent Test**: When `routing_available`, create/update/delete routing entries; reroute-with-normalizers → inline validation error; saves land in the mounted host YAML and return `applied: staged_next_job` (quickstart §5).

### Tests for User Story 3 ⚠️ (write first, ensure they FAIL)

- [X] T034 [P] [US3] Contract tests for `GET /api/config/routing` and `POST/PUT/DELETE /api/config/routing/entry[/{index}]` (incl. `applied: staged_next_job`, routing_available false read-only) in `tests/unit/test_dashboard_config_api.py`
- [X] T035 [P] [US3] Unit tests for routing read/write/validate against spec-078 models, absent-config (`routing_available=false`) handling, and the reroute/normalize validation rules in `tests/unit/test_routing_config_service.py`

### Implementation for User Story 3

- [X] T036 [US3] Implement routing YAML read/write in `src/coordinare/routing_config_service.py`: resolve host path from performer-endpoint volumes, read entries, validate proposed edits by constructing spec-078 `RoutingTable`/`RoutingEntry`/`TargetDescriptor` from `agent/performer/src/performer/proxy/routing.py`, atomic write via `config_write_service` (research D2, data-model E4)
- [X] T037 [US3] Implement `GET /api/config/routing` endpoint in `src/coordinare/dashboard.py` (`routing_available`, entries, content_hash) (contracts §Routing CRUD)
- [X] T038 [US3] Implement `POST /api/config/routing/entry` (create) in `src/coordinare/dashboard.py`, returning `SaveResult{applied: staged_next_job}` (contracts §Routing CRUD)
- [X] T039 [US3] Implement `PUT /api/config/routing/entry/{index}` (update) in `src/coordinare/dashboard.py` (contracts §Routing CRUD)
- [X] T040 [US3] Implement `DELETE /api/config/routing/entry/{index}` (delete) in `src/coordinare/dashboard.py` (contracts §Routing CRUD)
- [X] T041 [US3] Surface inline routing validation (reroute ⇒ empty normalizers; normalize ⇒ non-empty, all in `NORMALIZER_REGISTRY`; `wire_format` ∈ {openai,anthropic}; non-empty base_url) and the "applies to next performer job" staged-state label in the Config view in `src/coordinare/dashboard.py` (contracts §Routing validation, research D2/D7, Constitution III)

**Checkpoint**: All three user stories independently functional.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Performance budgets, accessibility gate, docs, and final validation across all stories.

- [X] T042 [P] Add CI benchmark for SC-010 in `tests/unit/test_dashboard_config_integration.py` (or a `tests/benchmarks/` module): `GET /api/config/all` < 300 ms p95 server-side; single save round-trip < 500 ms p95 (contracts; quickstart §7)
- [X] T043 [P] Complete the **UX accessibility checklist** (WCAG 2.1 AA, design-token sourcing, inline-error/loading-state coverage) required by Constitution III before implementation sign-off; record under `specs/081-config-ui/checklists/`
- [X] T044 [P] Verify **no secret leakage**: grep dashboard logs and `GET /api/config/all` payload for any plaintext secret; assert masked-unchanged-secret save is a no-op (quickstart §6) — add as a test in `tests/unit/test_config_write_service.py`
- [X] T045 Run `quickstart.md` end-to-end validation (all 7 steps) against a running dashboard and record results
- [X] T046 [P] Update operator docs noting `safe_dump` strips config.yaml comments (dashboard labels/help + tracked `config.example*.yaml` are the documentation surface) and routing edits bind at next performer job (research D5/D2; quickstart notes)
- [X] T049 [P] Regression test in `tests/unit/test_dashboard_config_api.py`: assert the pre-existing endpoints (`GET/PUT /api/config/global` 11-field slice, `GET /api/config/effective`, `GET/PUT/DELETE /api/personas/{role}`, `POST /api/config/reload`) retain their exact request/response contract after the new endpoints are mounted (FR-014)
- [X] T050 [P] Fault-injection atomicity test in `tests/unit/test_config_write_service.py`: simulate a crash between tempfile write and `os.replace` (patch `os.replace` to raise), then assert the original `config.yaml` is byte-for-byte intact (no truncation, no partial/temp file left adjacent), proving the write is all-or-nothing (SC-007)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately.
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories.
- **User Stories (Phase 3–5)**: All depend on Foundational. US1 → US2 → US3 in priority order, or in parallel if staffed (see independence notes).
- **Polish (Phase 6)**: Depends on the targeted user stories being complete.

### User Story Dependencies

- **US1 (P1)**: After Foundational. No dependency on US2/US3. Read-only — the MVP.
- **US2 (P2)**: After Foundational. Reuses US1's descriptor layer for save targets but is independently testable (write endpoints + catalog CRUD). Logically builds on US1's read surface.
- **US3 (P3)**: After Foundational. Independent of US2 (separate `routing_yaml` store + `routing_config_service`); reuses the shared atomic-write primitive. Independently testable.

### Within Each User Story

- Tests written and FAILING before implementation.
- Descriptor/section read-model before endpoints (US1); write service before endpoints (US2); routing service before endpoints (US3).
- Endpoints before the UI wiring that consumes them.

### Parallel Opportunities

- Setup: T002, T003, T004 in parallel (different files) after T001.
- Foundational: T008, T009, T010 in parallel (T005–T007 share `config_descriptors.py`, so sequential among themselves).
- US1 tests T011/T012 in parallel; US2 tests T020–T023 in parallel; US3 tests T034/T035 in parallel.
- Within US2, the four catalog endpoints (T028–T031) touch the same `dashboard.py` file → sequential; the write-service logic (T024–T026) precedes them.
- After Foundational, US1/US2/US3 can be staffed in parallel by different developers (distinct primary modules: config_descriptors / config_write_service / routing_config_service), coordinating on shared `dashboard.py` edits.

---

## Parallel Example: User Story 1

```bash
# Tests first (parallel, different concerns in two files):
Task: "Contract test GET /api/config/all + section in tests/unit/test_dashboard_config_api.py"
Task: "Unit test section grouping + CatalogItem projection in tests/unit/test_config_descriptors.py"

# Then read-model implementation (config_descriptors.py is one file → sequential),
# followed by the two GET endpoints, then the Config view UI.
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 Setup → 2. Phase 2 Foundational (CRITICAL) → 3. Phase 3 US1 → **STOP & VALIDATE**: whole config viewable, secrets masked, routing empty-state correct. Ship the read-only MVP.

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. US1 (view everything) → demo MVP.
3. US2 (edit + spec-080 catalog CRUD, live/staged) → demo.
4. US3 (spec-078 routing CRUD) → demo.
5. Polish (perf benchmark, a11y gate, secret-leak check, quickstart validation).

### Parallel Team Strategy

After Foundational: Dev A → US1 (config_descriptors + GET endpoints + view), Dev B → US2 (config_write_service + write/catalog endpoints), Dev C → US3 (routing_config_service + routing endpoints). Coordinate on shared `dashboard.py` route additions.

---

## Notes

- [P] = different files, no incomplete-task dependency.
- Tests are mandatory (Constitution Principle II, non-negotiable) — verify they FAIL before implementing.
- Existing `/api/config/effective`, `/api/config/global`, `/api/personas/*`, `/api/config/reload` are preserved unchanged (contracts preamble).
- No coordinare→performer dispatch-payload fields are added (research D2; contracts Field Registry).
- Run coordinare and performer test suites separately (`.venv/bin/pytest tests/unit/` for coordinare) — conftest collision.
- Commit after each task or logical group; never defer nits ("No Follow-Ups" discipline).
