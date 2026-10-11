# Tasks: MCP management layer

**Input**: Design documents from `/specs/551-mcp-management-layer/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/mcp-tools.md, quickstart.md

**Tests**: The spec mandates a test suite per domain (contract, unit, transport smoke) and the constitution requires test-first. Tests are written FIRST within each story and must FAIL before their implementation tasks run.

**Organization**: Tasks are grouped by user story. All six stories are P1 (they ship as one phase-1 release); the story order below is the dependency order from the contract.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to
- Include exact file paths in descriptions

## Path Conventions

- Single project: `src/coordinare/`, `tests/unit/`, `deploy/helm/coordinare/`
- The MCP package is `src/coordinare/mcp/` (plan.md structure)

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Dependency, package skeleton, and boot configuration

- [X] T001 Add `mcp==2.3.0` to dependencies in `pyproject.toml`, run `uv lock`, then run `make typecheck` to surface any SDK type gaps under the strict mypy 433 ratchet early (record needed per-module allowlist entries in `pyproject.toml` if any)
- [X] T002 [P] Create the package skeleton `src/coordinare/mcp/`: `__init__.py`, `__main__.py` (arg/env parsing, transport selection stub, exit codes), `server.py` stub, `auth.py` stub, `client.py` stub, `audit.py` stub, `tools/__init__.py`, `tools/config_tools.py`, `tools/symphony_tools.py`, `tools/persona_tools.py`, `tools/lifecycle_tools.py`, `tools/state_tools.py`, each with `from __future__ import annotations` and a module docstring stating its single purpose
- [X] T003 [P] Implement `McpServerConfig` in `src/coordinare/mcp/config.py` exactly per `data-model.md` (transport, listen_host, listen_port 8091, agent_token/dashboard_token as SecretStr min length 32, dashboard_url, health_url, read_timeout_seconds 10.0, lifecycle_timeout_seconds 60.0, server_name; pydantic v2 validation; `${VAR}` env expansion consistent with `CoordinareConfiguration`)
- [X] T004 [P] Write `tests/unit/test_551_mcp_config.py`: defaults, bad port (0, 65536), token shorter than 32, unparseable dashboard_url, out-of-range timeouts, env-var expansion

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The seam every tool runs on. No story work before this phase is complete.

**CRITICAL**: No user story work can begin until this phase is complete

- [X] T005 Implement `ToolResult` and the error-mapping table in `src/coordinare/mcp/client.py` per `data-model.md` (`ok`, `data`, `error.kind` in `auth_failed|conflict|validation|daemon_unreachable|daemon_error`, `error.status`, `error.detail`, `error.recovery_hint`; 401/403->auth_failed, 409->conflict with verbatim body plus re-read hint, 400/422->validation with field detail, 5xx->daemon_error truncated body, transport timeout/connection error->daemon_unreachable status 0)
- [X] T006 Implement `DashboardClient` in `src/coordinare/mcp/client.py`: per-call httpx AsyncClient (no pooling), base_url + health_url from config, dashboard token as `Authorization: Bearer` on the internal hop, domain-based timeout (read vs lifecycle), methods `get_json`, `send_json`, `get_text`; never logs or echoes the token
- [X] T007 [P] Write `tests/unit/test_551_mcp_error_mapping.py`: one case per row of the mapping table using a mocked httpx transport; assert `kind`, `status`, `detail` fidelity and the recovery hint on conflict
- [X] T008 [P] Implement the bearer check in `src/coordinare/mcp/auth.py`: constant-time compare (mirroring `dashboard_auth.py`'s pattern), accept only the `Authorization: Bearer` scheme, credential fingerprint helper (`sha256(token)[:8]` hex)
- [X] T009 [P] Write `tests/unit/test_551_mcp_auth.py`: missing header, wrong scheme, wrong token, correct token, fingerprint stability, token never present in the fingerprint
- [X] T010 Implement the audit emitter in `src/coordinare/mcp/audit.py`: structlog event `mcp.tool_invoked` with keys `tool`, `action`, `outcome`, `error_kind`, `credential_fingerprint`, `duration_ms` (snake_case.dot vocabulary)
- [X] T011 Implement `ToolDefinition` and the registry in `src/coordinare/mcp/tools/__init__.py` per `data-model.md`: name, domain enum, description, input_model (pydantic), MCP annotations, `rest_calls` tuple, handler; `DEFERRED_REST_PREFIXES` constant per the defer-list in `contracts/mcp-tools.md`; registry lookup helpers
- [X] T012 [P] Write `tests/unit/test_551_mcp_tool_registry.py` asserting the full contract from `contracts/mcp-tools.md`: exactly 16 tools, correct domain and annotations per tool, every rest_call matching the contract table, and no dashboard route outside the defer-list left uncovered (this test FAILS until all stories land; it is the execution map)

**Checkpoint**: Foundation ready: config, client seam, auth, audit, registry skeleton and the contract-driven registry test all exist. User story implementation can now begin.

---

## Phase 3: User Story 1 - Agent connects remotely (Priority: P1) MVP

**Goal**: An agent points at the MCP endpoint with a bearer token and lists tools; bad credentials are refused.

**Independent Test**: In-process MCP client over streamable HTTP connects, receives tool list, unauthorized/wrong-token requests get 401. Runnable via `tests/unit/test_551_mcp_transport_smoke.py`.

### Tests for User Story 1

- [X] T013 [P] [US1] Write the FAILING smoke test `tests/unit/test_551_mcp_transport_smoke.py`: spin the server app in-process, connect with the official `mcp` client (streamable HTTP), assert tool listing succeeds with a correct bearer token, assert 401 for missing and wrong tokens

### Implementation for User Story 1

- [X] T014 [US1] Implement server assembly in `src/coordinare/mcp/server.py`: build the MCP server from the registry, wrap every handler with the auth check and the audit hook (no-op for read-only in this phase), mount streamable HTTP transport on `listen_host:listen_port`; wire `python -m coordinare.mcp --transport http` in `src/coordinare/mcp/__main__.py` (depends on T008, T011)
- [X] T015 [US1] Implement `get_metrics` in `src/coordinare/mcp/tools/state_tools.py`: pass-through of GET `{health_url}/metrics` (read-only, idempotent); register it with `readOnlyHint: true` (depends on T014)
- [X] T016 [US1] Contract test for `get_metrics` against the real health app via FastAPI TestClient in `tests/unit/test_551_mcp_contract.py` (depends on T015)
- [X] T017 [US1] Checkpoint: `make lint && make typecheck && make test` green; smoke test green; MVP manually verifiable by following quickstart.md steps 1-2

**Checkpoint**: MVP. An agent can connect and read metrics. Stories 2-6 are additive on this seam.

---

## Phase 4: User Story 2 - Agent manages config (Priority: P1)

**Goal**: Config read/validate/update with optimistic concurrency and applied-classification.

**Independent Test**: Contract tests drive `get_config` -> mutate payload -> `validate_config` -> `update_config` with a stale `base_hash` (expect conflict) and a fresh one (expect applied) against the real dashboard app.

### Tests for User Story 2

- [X] T018 [P] [US2] Write FAILING contract tests in `tests/unit/test_551_mcp_contract_config.py`: every `get_config` scope from the contract (all/effective/global/section/catalog/routing) with `base_hash` presence where required; `update_config` each action -> correct REST verb/path; stale `base_hash` -> 409 conflict result naming `get_config`; `validate_config` writes nothing

### Implementation for User Story 2

- [X] T019 [P] [US2] Add the additive `POST /api/config/validate` route in `src/coordinare/dashboard/routers/config.py`, delegating to the existing `config_validation.validate_config` preflight; no write, no reload; covered by the dashboard auth middleware automatically
- [X] T020 [US2] Implement `get_config`, `update_config`, `validate_config` in `src/coordinare/mcp/tools/config_tools.py` per `contracts/mcp-tools.md` (scope/action enums, `base_hash` extraction and pass-through, `applied: hot_reloaded | staged_restart | staged_next_job` passthrough) and register with annotations (`get_config`, `validate_config` read-only; `update_config` destructive) (depends on T018, T019)
- [X] T021 [US2] Write `tests/unit/test_551_mcp_tools_config.py`: `base_hash` extraction from each scope shape, action-to-REST mapping table fidelity, timeout budget selection (read for get/validate) (depends on T020)

**Checkpoint**: US2 fully functional: inspect-validate-update-reload round trip passes contract tests.

---

## Phase 5: User Story 3 - Agent inspects live state (Priority: P1)

**Goal**: Snapshot, performer logs, activity feed, and metrics readable by an agent.

**Independent Test**: Contract tests read state/logs/activity against the real dashboard app and metrics against the health app; the two additive routes pass their own unit tests.

### Tests for User Story 3

- [X] T022 [P] [US3] Write FAILING contract tests in `tests/unit/test_551_mcp_contract_state.py`: `get_state` returns the snapshot shape, `get_performer_logs` returns the buffered tail, `get_activity` returns newest-first entries with optional `card_id` filter

### Implementation for User Story 3

- [X] T023 [P] [US3] Add the additive `GET /api/state` route in `src/coordinare/dashboard/routers/state.py`: JSON snapshot mirroring the `state_update` SSE payload (reuse `DashboardStore` snapshot building; no new state)
- [X] T024 [P] [US3] Add the additive `GET /api/activity` route in `src/coordinare/dashboard/routers/state.py`: newest-first JSON read of the in-memory `ActivityLog` with optional `card_id` filter (read-only; mirrors the SSE backfill payload)
- [X] T025 [US3] Implement `get_state`, `get_performer_logs`, `get_activity` in `src/coordinare/mcp/tools/state_tools.py` and register all three (plus T015's `get_metrics`) with `readOnlyHint: true` (depends on T023, T024)
- [X] T026 [US3] Unit tests for the additive routes following existing dashboard route test conventions (auth-covered, no SSE) in `tests/unit/test_551_mcp_additive_routes.py` (depends on T023, T024)

**Checkpoint**: US3 fully functional: full state-inspection domain readable via MCP.

---

## Phase 6: User Story 4 - Agent drives lifecycle controls (Priority: P1)

**Goal**: One `control` tool covering force_poll, cancel, skip_role, restart_from, veto, dry_run.

**Independent Test**: Contract tests drive every action enum value against the real dashboard app, asserting verb/path mapping, the 60 s budget, and status fidelity (202->ok, 409->conflict).

### Tests for User Story 4

- [X] T027 [P] [US4] Write FAILING contract tests in `tests/unit/test_551_mcp_contract_lifecycle.py`: every action -> REST verb/path from the contract; force-poll 202->ok and 409->conflict; required-argument validation (cancel/veto/dry_run need card_id; restart_from needs role)

### Implementation for User Story 4

- [X] T028 [US4] Implement `control` in `src/coordinare/mcp/tools/lifecycle_tools.py`: action enum -> REST mapping per contract, argument validation, lifecycle timeout budget, registered with `destructiveHint: true` and not read-only (depends on T005, T011)
- [X] T029 [US4] Write `tests/unit/test_551_mcp_tools_lifecycle.py`: mapping table fidelity and budget selection (depends on T028)

**Checkpoint**: US4 fully functional: all six lifecycle actions invokable.

---

## Phase 7: User Story 5 - Agent manages symphonies and personas (Priority: P1)

**Goal**: Symphony list/get/upsert/delete/validate (+ optional bootstrap chaining) and persona get/upsert/delete.

**Independent Test**: Contract tests drive the symphony and persona tools against the real dashboard app including the delete round trip and the bootstrap chaining sequence.

### Tests for User Story 5

- [X] T030 [P] [US5] Write FAILING contract tests in `tests/unit/test_551_mcp_contract_symphonies.py`: list/get/upsert/delete/validate mappings; `upsert_symphony` with `bootstrap: env` and `bootstrap: wiki` chains POST `/api/symphonies/{name}/env-bootstrap` or `/wiki-init` after the PUT, in order, surfacing both results
- [X] T031 [P] [US5] Write FAILING contract tests in `tests/unit/test_551_mcp_contract_personas.py`: get/upsert/delete mappings with delete returning an empty success result

### Implementation for User Story 5

- [X] T032 [US5] Implement `symphony_tools.py` and `persona_tools.py` per the contract (upsert/delete destructive; list/get/validate read-only) and register all eight tools (depends on T030, T031)
- [X] T033 [US5] Write `tests/unit/test_551_mcp_tools_symphonies.py` for bootstrap-chaining orchestration logic (mocked client): chain order, abort-on-first-error surfacing the bootstrap result (depends on T032)

**Checkpoint**: US5 fully functional: symphony and persona domains complete; registry test (T012) now passes all 16 tools.

---

## Phase 8: User Story 6 - Audit trail (Priority: P1)

**Goal**: Every mutating tool call emits exactly one `mcp.tool_invoked` line; read-only calls emit none.

**Independent Test**: Unit tests capture structlog output around a mutating call and a read-only call through the server wrapper.

### Tests for User Story 6

- [X] T034 [P] [US6] Write FAILING unit tests in `tests/unit/test_551_mcp_audit.py`: mutating call emits exactly one `mcp.tool_invoked` with correct keys and credential fingerprint (token itself absent); read-only call emits none; error outcome sets `outcome: error` and `error_kind`

### Implementation for User Story 6

- [X] T035 [US6] Wire the audit emitter into the handler wrapper in `src/coordinare/mcp/server.py`: mutate-only emission, duration measurement, fingerprint from the request credential (depends on T010, T014)

**Checkpoint**: US6 complete: audit coverage matches the annotations contract exactly.

---

## Phase 9: Polish & Cross-Cutting Concerns

**Purpose**: Deployment, documentation, and full-gate verification

- [X] T036 [P] Add the Helm sidecar in `deploy/helm/coordinare/templates/statefulset.yaml` (same daemon image, command `python -m coordinare.mcp`, env `COORDINARE_MCP_TOKEN`, `COORDINARE_MCP_DASHBOARD_TOKEN`, `COORDINARE_MCP_DASHBOARD_URL=http://localhost:8090`, `COORDINARE_MCP_HEALTH_URL=http://localhost:8080`), the 8091 MCP port in `templates/service.yaml`, and the `/mcp` route in `templates/ingress.yaml`, gated on a new `mcp.enabled` block in `values.yaml` with secrets sourced like existing secrets
- [X] T037 [P] Chart template tests for the sidecar, Service port, and Ingress routing following existing chart test conventions, rendering with `mcp.enabled: true` and `false`
- [X] T038 [P] Add `docs/mcp.md` (architecture summary from plan.md, security notes from the auth contract in `contracts/mcp-tools.md`) and a README.md section pointing to it plus quickstart.md
- [ ] T039 Validate `quickstart.md` end to end manually (local daemon + MCP server + a real agent tool-call round trip), fixing any drift between doc and implementation
- [ ] T040 [P] Measure the performance budgets from plan.md during the T039 run: tool-call p95 < 2 s for read tools and server cold start < 5 s, using a simple timing harness over the local MCP endpoint; record the numbers in `specs/551-mcp-management-layer/research.md` (appended R9)
- [X] T041 Final gates: `make lint && make typecheck && make test` all green; coverage ratchet not regressed (new code meets the minimum); confirm no `NEEDS CLARIFICATION` remains in the spec directory

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies, start immediately
- **Foundational (Phase 2)**: Depends on Setup; BLOCKS all user stories
- **US1 (Phase 3)**: Depends on Foundational; delivers the MVP seam
- **US2-US5 (Phases 4-7)**: All depend on US1 only; mutually independent (different tool files, different test files), runnable in parallel or any order
- **US6 (Phase 8)**: Depends on all mutating tools existing (US2, US4, US5) so the wrapper covers everything
- **Polish (Phase 9)**: Depends on all stories complete

### User Story Dependencies

- **US1 (connect remotely)**: first; everything else rides the seam it creates
- **US2 (config)**, **US3 (state)**, **US4 (lifecycle)**, **US5 (symphonies/personas)**: independent of each other; each independently testable against the real dashboard app
- **US6 (audit)**: last of the stories; the wrapper must see every mutating handler

### Within Each User Story

- Failing contract tests first (TDD), then additive routes, then tool implementation, then unit tests
- Registry registration with annotations completes each story (T012's completeness test advances per story)

### Parallel Opportunities

- Setup: T002, T003, T004 in parallel
- Foundational: T007, T008+T009, T010, T011, T012 in parallel (different files)
- After US1: US2, US3, US4, US5 entirely parallelizable (distinct tool files and test files)
- Within US3: the two additive routes (T023, T024) are parallel
- Within US5: symphony and persona test files (T030, T031) are parallel

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1 + Phase 2 (foundation)
2. Complete Phase 3 (US1)
3. STOP and VALIDATE: smoke test plus quickstart.md steps 1-2
4. Deployable as a metrics-reading agent seam; demo if ready

### Incremental Delivery

1. Setup + Foundational -> foundation ready
2. US1 -> validate independently -> MVP
3. US2 -> config round trip validated -> each further story stacks value
4. US6 closes the audit gap once every mutating tool exists
5. Polish ships deployment and docs

### Parallel Team Strategy

With multiple implementers after US1 lands:

- Implementer A: US2 (config)
- Implementer B: US3 (state)
- Implementer C: US4 (lifecycle) then US5 (symphonies/personas)
- Converge on US6 (audit), then polish

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps tasks to user stories for traceability; US1-US6 all map to P1 stories in the spec
- T012 is the execution map: the registry completeness test fails until the last story lands; per-story progress is measured by which of the 16 tools it finds registered
- Tests must fail before the implementation tasks they gate (constitution: test-first when specified)
- Commit after each task or logical group (Conventional Commits); the repo workflow gates merges on Lint, Test, Build Success
- Stop at any checkpoint to validate the story independently
