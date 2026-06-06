# Tasks: Self-Hosted Backend Robustness Layer

**Input**: Design documents from `/specs/078-selfhosted-backend-shim/`
**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: INCLUDED. The spec mandates TDD regression coverage — Constitution Principle II
(Testing Discipline, NON-NEGOTIABLE) and the quickstart test→SC mapping both require the
077-derived JSON+SSE harmony/reasoning fixtures and deterministic mocked upstreams
(`httpx.MockTransport`, mirroring `test_dual_model_proxy.py`).

**Organization**: Tasks are grouped by user story to enable independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

Single project, extending the existing `performer` package. Source under
`agent/performer/src/performer/proxy/`; tests under `agent/performer/tests/unit/proxy/`.
Run the performer suite separately from coordinare (conftest collision):
`.venv/bin/pytest agent/performer/tests/unit/proxy/ -q`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Package scaffolding and regression fixtures shared across all stories

- [X] T001 [P] Create the `normalizers/` package with an empty `agent/performer/src/performer/proxy/normalizers/__init__.py`
- [X] T002 [P] Add 077-derived regression fixtures (leaked `<|channel|>commentary` harmony in non-streaming JSON and split across SSE deltas; claude/qwen reasoning/thinking blocks in JSON and SSE) under `agent/performer/tests/unit/proxy/fixtures/`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Routing models, the normalizer framework, the shim transport, and the launch dispatch seam that ALL user stories build on

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T003 Define `RoutingEntry`, `TargetDescriptor`, and `RoutingTable` pydantic 2.x models in `agent/performer/src/performer/proxy/routing.py` (fields per data-model.md; load-time validation: `reroute` ⇒ `normalizers` empty, `normalize` ⇒ ≥1 key each present in `NORMALIZER_REGISTRY`, valid `wire_format`, required `base_url`)
- [X] T004 Implement `RoutingTable.resolve(backend, model) -> TargetDescriptor | None` with kebab→snake backend normalization (cf. 080 `test_kebab_case_backend_normalized`) in `agent/performer/src/performer/proxy/routing.py`; missing `(backend, model)` returns `None`
- [X] T005 [P] Define the `Normalizer` protocol (`key`, `normalize_json(body)`, `sse_filter() -> StatefulSSEFilter`) and the `StatefulSSEFilter` base (buffers across chunk boundaries, never emits a half-parsed tool call) in `agent/performer/src/performer/proxy/normalizers/base.py`
- [X] T006 Declare `NORMALIZER_REGISTRY: dict[str, Normalizer]` in `agent/performer/src/performer/proxy/normalizers/__init__.py` (initially empty; populated by US1)
- [X] T007 Implement the `SelfHostedShim` transport shell in `agent/performer/src/performer/proxy/shim.py` — same-process loopback aiohttp reverse proxy (reuse `dual_model_proxy.py` shell + `upstreams.py` render/parse) that forwards to `target.base_url` and is the seam where declared normalizers are applied to JSON + SSE
- [X] T008 Extend `maybe_launch_proxy(...)` in `agent/performer/src/performer/proxy/launch.py` to resolve the routing table at job start; on no matching entry, perform the byte-for-byte no-op (no proxy launched, no provider-env override) and otherwise dispatch on `target.strategy` (`normalize` | `reroute`), reusing 080's `PROVIDER_BASE_URL_ENV` map + env-restore lifecycle
- [X] T009 [P] Write `test_routing.py` in `agent/performer/tests/unit/proxy/` — `(backend, model)` resolution, kebab→snake normalization, missing entry → `None`, and load-time validation errors (reroute-with-normalizers, unknown normalizer key, missing `base_url`, bad `wire_format`)
- [X] T010 Extend `test_launch.py` in `agent/performer/tests/unit/proxy/` — no routing entry ⇒ no proxy launched and no provider-env override set (byte-for-byte no-op, **SC-003**)

**Checkpoint**: Routing resolution, the normalizer framework, the shim transport, and launch dispatch exist — user stories can now begin

---

## Phase 3: User Story 1 - Harmony tool-call normalization for self-hosted gpt-oss (Priority: P1) 🎯 MVP

**Goal**: A format-keyed `harmony_tool_calls` normalizer reassembles leaked `<|channel|>commentary` text into structured `tool_calls` (JSON + SSE) so a tool-using agent on self-hosted gpt-oss completes its lifecycle; ship `strip_reasoning` (the 073 generalization) on the same normalize path.

**Independent Test**: Point a tool-using backend at a `normalize` gpt-oss target, feed the leaked-harmony JSON and SSE fixtures through the layer, assert well-formed `tool_calls` with zero raw `<|channel|>` markers and that the agent reads/edits a file.

### Tests for User Story 1 ⚠️ (write FIRST, ensure they FAIL)

- [X] T011 [P] [US1] Write `test_normalizer_harmony.py` in `agent/performer/tests/unit/proxy/` — leaked-harmony JSON fixture → structured `tool_calls`, and SSE fixture reassembled across deltas with no leaked `<|channel|>` markers and no half-parsed tool call (**SC-001 / SC-002**, FR-078-7)
- [X] T012 [P] [US1] Write `test_normalizer_reasoning.py` in `agent/performer/tests/unit/proxy/` — reasoning/thinking blocks stripped from JSON + SSE (073 regression, **SC-002**, FR-078-8)
- [X] T013 [P] [US1] Write `test_shim.py` in `agent/performer/tests/unit/proxy/` — only the target's explicitly-declared normalizers run; unknown/unrecognized response format passes through unchanged (fail-open on normalization, **SC-006**, FR-078-9); reuse across two backends proves shared normalizer (**SC-006**)

### Implementation for User Story 1

- [X] T014 [US1] Implement the `harmony_tool_calls` JSON path (`normalize_json`) — reassemble `<|channel|>commentary to=<tool> …` into structured `tool_calls`, leaking zero harmony markers — in `agent/performer/src/performer/proxy/normalizers/harmony.py` (FR-078-7)
- [X] T015 [US1] Implement the `harmony_tool_calls` stateful SSE filter (buffer across deltas, emit well-formed streaming `tool_calls`, never a half-parsed call) in `agent/performer/src/performer/proxy/normalizers/harmony.py` (FR-078-3, FR-078-7)
- [X] T016 [P] [US1] Implement `strip_reasoning` (JSON + SSE) folding the 073 `ClaudeCodeShim` thinking-block strip in `agent/performer/src/performer/proxy/normalizers/reasoning.py` (FR-078-8)
- [X] T017 [US1] Register `harmony_tool_calls` and `strip_reasoning` in `NORMALIZER_REGISTRY` in `agent/performer/src/performer/proxy/normalizers/__init__.py` (depends on T014–T016)
- [X] T018 [US1] Wire the `normalize` strategy in `agent/performer/src/performer/proxy/shim.py` — launch the loopback shim, set the backend provider base URL to it, apply the target's declared normalizers (JSON + SSE), pass unknown formats through unchanged; INFO logging limited to method/path/status/latency + normalizer decision, never tokens/bodies (FR-078-2, FR-078-9, FR-078-10)

**Checkpoint**: A self-hosted gpt-oss `normalize` target delivers well-formed `tool_calls`; US1 is independently testable and is the MVP

---

## Phase 4: User Story 2 - Reroute around a broken middleware layer (Priority: P1)

**Goal**: A `reroute` target repoints the backend's provider env directly at a clean upstream and skips the shim entirely (no normalizer, no added translation), capturing the openclaw → Ollama-direct 077 fix as a config-selectable strategy.

**Independent Test**: Configure a `(backend, model)` with `strategy: reroute` to a clean upstream; assert the backend provider env points at the clean upstream (not LiteLLM), no proxy is launched, and no normalizer runs.

### Tests for User Story 2 ⚠️ (write FIRST, ensure they FAIL)

- [X] T019 [P] [US2] Extend `test_launch.py` in `agent/performer/tests/unit/proxy/` — `reroute` repoints the correct `PROVIDER_BASE_URL_ENV` var to the clean upstream, launches no shim, applies no normalizer, and restores env after the job (**SC-004**, FR-078-4)
- [X] T020 [P] [US2] Add a `test_launch.py` case — a backend with no `PROVIDER_BASE_URL_ENV` mapping (e.g. hermes / `UNSUPPORTED_BACKENDS`) appearing in a routing entry surfaces a clear "cannot route" error, never a silent no-op into a broken path (FR-078-4, Edge Case)

### Implementation for User Story 2

- [X] T021 [US2] Implement the `reroute` branch in `maybe_launch_proxy(...)` in `agent/performer/src/performer/proxy/launch.py` — set the backend's provider base URL env to `target.base_url` via `PROVIDER_BASE_URL_ENV` + env-restore, launch no proxy and apply no normalizer (FR-078-4, **SC-004**)
- [X] T022 [US2] Raise a clear "cannot route" error (mirroring 080 `ProxyLaunchError("no provider-base-URL override")`) when a routing entry targets a backend absent from `PROVIDER_BASE_URL_ENV`, in `agent/performer/src/performer/proxy/launch.py` (FR-078-4, Edge Case)

**Checkpoint**: Both `normalize` (US1) and `reroute` (US2) strategies work independently

---

## Phase 5: User Story 3 - Startup health/smoke-test gating (Priority: P2)

**Goal**: A timeout-bounded startup tool-calling smoke test gates routing — healthy proceeds; unhealthy with a declared `reroute_upstream` auto-reroutes; unhealthy with none fails closed with a clear error before any card is assigned. Never fail-open.

**Independent Test**: Start the layer against (a) a healthy probe target → routing gated open; (b) an unhealthy target with `reroute_upstream` → auto-reroute and proceed; (c) an unhealthy target without one → fail-closed with a specific error; (d) a wedged target → probe times out → unhealthy.

### Tests for User Story 3 ⚠️ (write FIRST, ensure they FAIL)

- [X] T023 [P] [US3] Write `test_health.py` in `agent/performer/tests/unit/proxy/` — healthy → `proceed`; unhealthy + `reroute_upstream` → `rerouted`; unhealthy + none → `fail_closed` (clear error, card not accepted); probe timeout → `unhealthy` then gated per the auto-reroute-then-fail-closed path (**SC-005**, FR-078-5)

### Implementation for User Story 3

- [X] T024 [US3] Implement the timeout-bounded tool-calling smoke probe and `HealthResult` (`status`, `reason`, `resolved_action`) in `agent/performer/src/performer/proxy/health.py` — bounded `httpx` timeout so a wedged upstream surfaces as `unhealthy` rather than hanging startup (FR-078-5, Edge Case)
- [X] T025 [US3] Implement the gating decision (`healthy`→proceed; `unhealthy`+`reroute_upstream`→auto-reroute; `unhealthy`+none→fail-closed; never fail-open) in `agent/performer/src/performer/proxy/health.py` (FR-078-5, **SC-005**)
- [X] T026 [US3] Invoke the health gate from `maybe_launch_proxy(...)` (after target resolution, before `backend.start()`) in `agent/performer/src/performer/proxy/launch.py`; emit the health decision to the job `capture_dir`, never tokens/bodies (FR-078-5, FR-078-10)

**Checkpoint**: All three user stories independently functional

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Verification, lint, and observability hardening across all stories

- [X] T027 [P] Run the performer proxy suite green: `.venv/bin/pytest agent/performer/tests/unit/proxy/ -q`
- [X] T028 [P] Lint the package: `.venv/bin/ruff check agent/performer/src/performer/proxy/`
- [X] T029 Audit all `proxy/` INFO logging — confirm no auth tokens or request/response bodies are logged anywhere (FR-078-10), only method/path/status/latency + normalizer/strategy/health decisions
- [X] T030 Execute the `quickstart.md` verify + integration-smoke steps and confirm the test→SC mapping table holds

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories
- **User Stories (Phase 3–5)**: All depend on Foundational completion
  - US1 (P1) and US2 (P1) are independent of each other once Foundational is done
  - US3 (P2) depends only on Foundational (it gates whatever strategy resolves)
- **Polish (Phase 6)**: Depends on all targeted stories being complete

### User Story Dependencies

- **US1 (P1)**: After Foundational. Needs `NORMALIZER_REGISTRY` (T006) and the shim shell (T007)
- **US2 (P1)**: After Foundational. Needs launch dispatch (T008); independent of US1
- **US3 (P2)**: After Foundational. Needs launch dispatch (T008); composes with US1/US2 outcomes but is independently testable

### Within Each User Story

- Tests are written FIRST and must FAIL before implementation
- Normalizer JSON/SSE paths (T014/T015) before registration (T017) before shim wiring (T018)
- `health.py` probe (T024) before gating (T025) before launch wiring (T026)

### Parallel Opportunities

- Setup: T001 ∥ T002
- Foundational: T005 ∥ T009 (different files); T003→T004 sequential (same file); T010 after T008
- US1 tests: T011 ∥ T012 ∥ T013; impl: T016 ∥ (T014→T015); then T017→T018
- US2: T019 ∥ T020 (tests); T021 then T022 (same file)
- Once Foundational is done, US1 and US2 can be staffed in parallel

---

## Parallel Example: User Story 1

```bash
# Tests first (different files, run together):
Task: "test_normalizer_harmony.py — JSON+SSE harmony reassembly, no leaked markers"
Task: "test_normalizer_reasoning.py — strip reasoning JSON+SSE (073 regression)"
Task: "test_shim.py — declared normalizers only; unknown format pass-through"

# Then independent implementations:
Task: "strip_reasoning in normalizers/reasoning.py"   # [P] — different file
Task: "harmony_tool_calls JSON path in normalizers/harmony.py"  # then SSE in same file
```

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1 Setup → 2. Phase 2 Foundational (CRITICAL — blocks all) → 3. Phase 3 US1
4. **STOP and VALIDATE**: harmony normalize target delivers well-formed `tool_calls` (SC-001/002/006)
5. Demo: the 077 openclaw "can't read files" failure no longer reproduces

### Incremental Delivery

1. Setup + Foundational → no-op guarantee verified (SC-003)
2. US1 → harmony + reasoning normalization (MVP)
3. US2 → reroute strategy (SC-004) — the actual openclaw→Ollama-direct fix
4. US3 → startup health gating (SC-005)

---

## Notes

- [P] = different files, no incomplete dependencies
- [Story] label maps each task to its user story for traceability
- Verify tests fail before implementing
- Run the performer suite separately from coordinare (conftest collision)
- This feature adds **no new dispatch-payload field** — the routing table is performer-side config; provider-env overrides reuse 080's `PROVIDER_BASE_URL_ENV` unchanged
