# Tasks: Dual-Model Planner/Executor Orchestration

**Feature**: `080-dual-model-orchestration` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)
**Approach**: Test-first (Constitution II — TDD required per plan Constitution Check). Each user-story phase is an independently testable increment.

**Conventions**: coordinare tests run with `.venv/bin/pytest`; performer tests via `bin/build` performer phase or `.venv/bin/pytest agent/performer`. `[P]` = parallelizable (distinct files, no incomplete deps).

---

## Phase 1: Setup

- [X] T001 Create the performer proxy package skeleton at `agent/performer/src/performer/proxy/` with `__init__.py` and empty modules `llm_turn.py`, `upstreams.py`, `assembler.py`, `classifier.py`, `strategies.py`, `dual_model_proxy.py`
- [X] T002 [P] Create test scaffolding dirs `agent/performer/tests/unit/proxy/` and `tests/unit/config/` (add `__init__.py`/conftest as the existing layout requires)

---

## Phase 2: Foundational (config backbone — BLOCKS all user stories)

**Goal**: the `endpoints`/`model_endpoints`/`modes` catalogs, the resolution chain, validation, and dispatch plumbing. Every story resolves models through this.

- [X] T003 [P] Write failing tests for `Endpoint`/`ModelEndpoint`/`Mode` pydantic models in `tests/unit/config/test_catalog_models.py` (native vs self-hosted `kind` base_url rules, `extra="forbid"`, unique names)
- [X] T004 [P] Write failing tests for resolution + consistency in `tests/unit/test_config_validation.py` (dangling refs, hard-cut inline `model:`, `single` rejects `thinking`/`classifier`, `conditional` requires `classifier`+`threshold`)
- [X] T005 Implement `Endpoint`, `ModelEndpoint`, `Mode` models in `src/coordinare/config.py` (per data-model.md)
- [X] T006 Modify `PerformerRoleConfig` in `src/coordinare/config.py`: remove inline `model`, add `mode`; rework `PerformersConfig.resolved_role()` to resolve `performer.mode → modes → model_endpoints → endpoints` and return backend + resolved strategy/leg config
- [X] T007 Implement validation in `src/coordinare/config_validation.py`: reference resolution (FR-005), hard-cut inline `model:` (FR-006), endpoint `kind` rules (FR-007), strategy/field consistency (FR-008), unique names
- [X] T008 Add a new nullable `orchestration` field (`OrchestrationConfig`: strategy + resolved `tool`/`thinking`/`classifier` UpstreamRefs + threshold/invalidate_*/error_pattern/expose_plan_as/on_think_error) to `JobInitPayload` in `agent/performer/src/performer/server/models.py`, and populate it in `_build_job_payload` (`src/coordinare/services/http_performer_service.py`) only when `strategy != single`. Auth tokens stay in the existing `secrets` dict (referenced by `auth_env` name); no secret goes in `orchestration`. Per `contracts/proxy-wire-contract.md` Field Registry.
- [X] T009 [P] Contract test for the catalogs in `tests/contract/test_config_catalogs_080.py` per `contracts/config-catalogs.md`

**Checkpoint**: config catalogs parse, resolve, and validate; dispatch carries resolved orchestration config.

---

## Phase 3: User Story 1 — Unified catalogs, single & native modes, migration (Priority: P1) 🎯 MVP

**Goal**: all single-model performers (self-hosted and native frontier) expressed via catalogs with identical behavior and no proxy.
**Independent test**: convert every in-repo example config; each previously-single-model performer runs identically; native (`kind: anthropic`/`openai`, `strategy: single`) uses the harness's own client with no override; no proxy launches.

- [X] T010 [US1] Write failing parity tests in `tests/unit/config/test_single_mode_parity.py`: `strategy: single` self-hosted yields the same override env as today's inline path; native `kind` yields NO override; resolution returns `proxy=False`
- [X] T011 [US1] Ensure the dispatch/consumption path launches NO proxy for `strategy: single` and applies native-vs-self-hosted override correctly (FR-007/FR-009) in `src/coordinare/services/http_performer_service.py`
- [X] T012 [P] [US1] Migrate the 9 `config.example.*.yaml` files in repo root to the catalog form (`endpoints`/`model_endpoints`/`modes` + `performers.<role>.mode`), removing inline `model:`
- [X] T013 [US1] Migrate active `config.yaml` / `config.claude.yaml` to catalog form and document the operator (gitignored) migration steps in `quickstart.md`
- [X] T014 [US1] Run the full coordinare config suite (`.venv/bin/pytest tests/unit/test_config*.py tests/unit/config tests/contract`) green; fix any regressions

**Checkpoint**: today's behavior fully reproduced through the unified config. MVP deliverable complete.

---

## Phase 4: User Story 2 — Dual-model "always think, then act" (Priority: P2)

**Goal**: per-turn planner/executor proxy for `strategy: always`.
**Independent test**: a fake CLI pointed at the proxy issues a completion; the thinking upstream is called (tools hidden), then the tool upstream with the plan injected as a system message; one recombined response returns with the tool model's `tool_calls` and the plan per `expose_plan_as`; works in JSON and SSE.

- [X] T015 [P] [US2] Failing tests for the `LLMTurn` canonical rep in `agent/performer/tests/unit/proxy/test_llm_turn.py`
- [X] T016 [P] [US2] Failing tests for `Upstream` + anthropic/openai format adapters (request render + response parse round-trip, `tools=False` hides tools) in `.../proxy/test_upstreams.py`
- [X] T017 [P] [US2] Failing tests for `ResponseAssembler` across {anthropic, openai} × {JSON, SSE} × `expose_plan_as` in `.../proxy/test_assembler.py`
- [X] T018 [P] [US2] Failing tests for `DualModelProxy` front door + `AlwaysThinkThenAct` (think tools-hidden → act plan-injected-as-system-msg → assemble) + `on_think_error` fallback in `.../proxy/test_strategies_always.py`
- [X] T019 [US2] Implement `LLMTurn` in `agent/performer/src/performer/proxy/llm_turn.py`
- [X] T020 [US2] Implement `Upstream` + anthropic/openai adapters (httpx, per-call timeout-bounded so a hung upstream surfaces/falls back rather than black-holing — composes with the 077 stall watchdog, FR-018) in `.../proxy/upstreams.py`
- [X] T021 [US2] Implement `ResponseAssembler` (JSON + SSE, plan merge per `expose_plan_as`, reuse 073 SSE handling) in `.../proxy/assembler.py`
- [X] T022 [US2] Implement `DualModelProxy` transport shell (aiohttp `127.0.0.1:0`, `start()`→loopback URL, `capture_dir`, log hygiene FR-019) in `.../proxy/dual_model_proxy.py`
- [X] T023 [US2] Implement `OrchestrationStrategy` protocol + shared `think()`/`act()`/`assemble()` + `AlwaysThinkThenAct` in `.../proxy/strategies.py`
- [X] T024 [US2] Implement `OrchestrationRecord` capture (strategy/decision/plan/per-call latency, no secrets) to the job capture dir (FR-021)
- [X] T025 [US2] Wire backends to set their provider base URL to the proxy when `strategy != single` (claude_code already shims; add codex/opencode/junie/pi/openclaw) in `agent/performer/src/performer/backends/*.py`; document hermes `single`-only constraint
- [X] T026 [US2] Integration test: fake agent CLI driving the proxy through the `always` path in JSON and SSE in `agent/performer/tests/unit/proxy/test_proxy_integration.py`; assert the agent's terminal contract (DONE/PARTIAL_PROGRESS/BLOCKED) passes through unchanged (FR-020) and that an upstream timeout surfaces/falls back rather than hanging (FR-018)

**Checkpoint**: a performer can run `strategy: always` end-to-end; SC-002 verifiable from capture artifacts.

---

## Phase 5: User Story 3 — Conditional escalation (Priority: P3)

**Goal**: classifier-gated think→act.
**Independent test**: stubbed classifier scores route ≥threshold→think→act and <threshold→act-only; classifier failure defaults to think.

- [X] T027 [P] [US3] Failing tests for `DifficultyClassifier` (score + failure→think) and `ConditionalEscalation` routing in `agent/performer/tests/unit/proxy/test_strategies_conditional.py`
- [X] T028 [US3] Implement `DifficultyClassifier` (model or rules, timeout-bounded) in `.../proxy/classifier.py`
- [X] T029 [US3] Implement `ConditionalEscalation` in `.../proxy/strategies.py` (reuses `always` path on escalation)
- [X] T030 [US3] Record classifier score + decision in `OrchestrationRecord`

**Checkpoint**: `strategy: conditional` works; escalation observable.

---

## Phase 6: User Story 4 — Think once, act many (Priority: P3)

**Goal**: cached-plan reuse across a stage.
**Independent test**: think called once up front; cached plan reused on later turns; re-think on `invalidate_after_turns` or an error marker in incoming tool results.

- [X] T031 [P] [US4] Failing tests for `StageState` invalidation + `ThinkOnceActMany` in `agent/performer/tests/unit/proxy/test_strategies_think_once.py`: think-once, reuse, re-think on `invalidate_after_turns`, and re-think on the FR-016 error marker (explicit failure status OR `error_pattern` match in the latest incoming tool result; case-insensitive; plan text never scanned)
- [X] T032 [US4] Implement `StageState` (cached plan, turn counter, `error_pattern` detection per FR-016) + `ThinkOnceActMany` in `.../proxy/strategies.py`

**Checkpoint**: `strategy: think_once` works; plan amortized across the stage.

---

## Phase 7: Polish & Cross-Cutting

- [X] T033 [P] Proxy-overhead benchmark: assert `DualModelProxy` non-model overhead < 50 ms/turn and `strategy: single` adds zero overhead (SC-003); add to the perf benchmark suite
- [X] T034 [P] Restore ≥90% coverage; run `bin/build --all` green (lint, unit, coverage, performer, e2e, docker)
- [X] T035 [P] Update `AGENTS.md` + docs with the catalog model and the hermes `single`-only constraint
- [X] T036 Validate `quickstart.md` end-to-end: each documented validation error actually fires; smoke a `single` and an `always` mode
- [ ] T037 Live-round validation: run one card stage on an `always` mode; capture per-backend findings (mirrors 077 Phase-9 format)

---

## Dependencies & Story Order

- **Setup (P1 tasks T001–T002)** → **Foundational (T003–T009)** block everything.
- **US1 (T010–T014)** depends on Foundational. **MVP = Setup + Foundational + US1.**
- **US2 (T015–T026)** depends on Foundational (resolved dispatch config) and builds the shared proxy core. US1 not strictly required for US2 code, but US1 lands first (P1).
- **US3 (T027–T030)** and **US4 (T031–T032)** depend on US2's proxy core (`LLMTurn`/`Upstream`/`Assembler`/`DualModelProxy`/strategy protocol). US3 and US4 are independent of each other.
- **Polish (T033–T037)** after the targeted stories.

## Parallel Execution Examples

- Foundational tests: T003 ∥ T004 (different files), then T005→T006→T007 sequential (same `config.py`/validation), T009 ∥ after models exist.
- US2 test-first batch: T015 ∥ T016 ∥ T017 ∥ T018 (distinct test files). Implementations T019→T020→T021 then T022/T023 (T023 shares `strategies.py` with later stories — keep sequential within proxy), T024, T025 (distinct backend files can go ∥), T026 last.
- US3 ∥ US4 once US2 core is merged (different test/impl seams, though both touch `strategies.py` — serialize edits to that file).

## Implementation Strategy

- **MVP first**: ship Setup + Foundational + US1 — unified config with single/native modes and full migration, behavior-identical to today. Independently deployable and reviewable.
- **Increment 2**: US2 (`always`) — the core dual-model capability + proxy framework.
- **Increment 3**: US3 + US4 — adaptive strategies layered on the proxy core.
- **Polish**: perf budget benchmark, coverage, docs, live validation.
- Per project discipline: this branch collects the 080 feature only; unrelated fixes get their own spec.
