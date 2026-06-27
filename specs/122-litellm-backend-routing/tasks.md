# Tasks: Consolidate All Agent Backends on the LiteLLM Model Gateway

**Input**: Design documents from `/specs/122-litellm-backend-routing/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/compatibility-matrix.md, quickstart.md

**Tests**: REQUIRED — Constitution II is NON-NEGOTIABLE. For code changes, TDD (Red→Green). The US2
compatibility matrix is itself the empirical test that gates the US3 migration.

**Organization**: By user story. Execution order is dependency-driven: **US2 (matrix) → US1 (reach
fixes) → US3 (migrate)** — the matrix produces the per-backend data US1/US3 act on. (US1 & US2 are both
P1; US3 is P2.)

## Path Conventions

Single repo: coordinare daemon `src/coordinare/`, performer package `agent/performer/src/performer/`,
harness `scripts/smoke_backends.py`, example configs at repo root, tests under `tests/` and
`agent/performer/tests/`. Live `config.yaml`/`routing.yaml` are gitignored deployment state.

---

## Phase 1: Setup (Shared)

- [x] T001 Confirm clean baseline on branch `122-litellm-backend-routing`: `.venv/bin/pytest -q` and `.venv/bin/ruff check src agent/performer/src scripts` green; record the starting point (no code changes).
- [x] T002 Re-confirm the LiteLLM feasibility probes from research.md still hold (quick guard before building on them): `spark/gpt-oss:120b` answers on `/v1/chat/completions` (structured `tool_calls` + `reasoning_content`) and on `/v1/messages`; record the probe output under `tmp/`. If a probe fails, STOP and surface (the spec assumes the gateway serves these).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: A LiteLLM-pointed test config the matrix harness consumes, without touching live config.

- [x] T003 Create a committed test-config variant `config.example.litellm-test.yaml` (repo root; the `config.example.*` name is git-allowlisted so it commits — no secrets, `${VAR}` placeholders only) with: an `endpoints[]` entry for the LiteLLM proxy; `model_endpoints[]` `gptoss120-litellm`→`spark/gpt-oss:120b` and `gptoss20-litellm`→`spark/gpt-oss:20b`; a `single-*` `mode` per; and one `performer_endpoints[]` entry per backend (claude_code, codex, opencode, junie, pi, openclaw, hermes) whose `env` points that backend's provider base-URL (`PROVIDER_BASE_URL_ENV` per research.md) at the LiteLLM proxy + the LiteLLM master key, with NO `SELFHOSTED_ROUTING_CONFIG` (hit LiteLLM directly, not the Ollama shim). claude_code → `ANTHROPIC_BASE_URL` = LiteLLM `/` (anthropic front door); OpenAI-wire backends → their `*_PROVIDER_BASE_URL` = LiteLLM `/v1`.

---

## Phase 3: User Story 2 — Per-backend compatibility matrix 🎯 GATE (Priority: P1)

**Goal**: A reproducible matrix proving, per backend, launched/completed/output/contract-satisfied + which normalizers are still needed against `spark/gpt-oss:120b` via LiteLLM — without touching the live fleet.

**Independent Test**: Run the harness; it emits one matrix row per backend matching `contracts/compatibility-matrix.md`; re-running on an unchanged system yields the same matrix.

### Tests (write first — must fail)

- [x] T004 [P] [US2] Failing unit test `tests/unit/scripts/test_smoke_backends_matrix.py`: assert the harness's row-builder maps a backend's job result to the contract fields (`launched/completed/output_present/contract_satisfied/normalizers_needed/gateway_available/verdict`) and computes `verdict` per the contract rules (compatible ⟺ launched∧completed∧contract_satisfied; `gateway_unavailable` distinct from `incompatible`). Use a synthetic job-status dict (no containers).
- [x] T005 [P] [US2] Failing unit test asserting the matrix row carries NO secret value (the LiteLLM master key never appears in any field, incl. `note`).

### Implementation

- [x] T006 [US2] Extend `scripts/smoke_backends.py`: add a `--via-litellm` (or `--config config.example.litellm-test.yaml`) path that runs each backend against the LiteLLM-pointed endpoints from T003 and emits the per-backend matrix (contract fields). Reuse the existing launch→POST-qa-job→read-result flow; add `gateway_available` detection (distinguish a gateway 5xx / "no healthy deployments" from a backend failure) and the `verdict` computation.
- [x] T007 [US2] Add the `normalizers_needed` determination: for each backend, re-run with the candidate normalizer(s) absent (the LiteLLM path has none by default, so the baseline run already tests "no normalizer" — record PASS ⇒ normalizers redundant; if a backend fails raw, note which normalizer it would need). Emit `normalizers_needed` per row.
- [x] T008 [US2] Run the matrix across ALL backends; write the artifact to `tmp/litellm_matrix.json` AND record a human-readable copy in `specs/122-litellm-backend-routing/compatibility-matrix-results.md` (traceability, FR-016). Sequentially (the gateway/Ollama upstream is single-request-ish) with adequate `max_tokens` (avoid the empty-content-at-low-tokens trap from research.md).

**Checkpoint**: matrix exists; each backend labelled compatible / incompatible / gateway_unavailable, with normalizers_needed. This GATES US3.

---

## Phase 4: User Story 1 — Every backend reaches spark/gpt-oss via LiteLLM (Priority: P1)

**Goal**: Each backend the fleet uses can reach `spark/gpt-oss:120b` through LiteLLM via its provider base-URL; close any gap the matrix surfaced.

**Independent Test**: Each `compatible` matrix row demonstrates the backend reached LiteLLM (not Ollama-direct); claude_code via `/v1/messages` with no translate shim.

### Implementation

- [ ] T009 [US1] For any backend the matrix marked `incompatible` for a *reachability* reason (wrong front door, auth, model name, config format), fix the test-config wiring in `config.example.litellm-test.yaml` (and note the required real-config shape) and re-run that backend's matrix row until it is `compatible` or confirmed a genuine backend incompatibility (then it is NOT migrated — FR-013).
- [ ] T010 [US1] Document, in `research.md` (or the results md), the exact per-backend LiteLLM wiring that works (provider base-URL value, front-door path, model name, auth env) — the source of truth US3 copies into the live config + examples.

**Checkpoint**: every fleet backend is either proven gateway-reachable (compatible) or recorded as a genuine incompatibility to skip.

---

## Phase 5: User Story 3 — Migrate routing + retire redundant shims (Priority: P2)

**Goal**: Re-point live routing to LiteLLM for compatible backends, drop proven-redundant shims/normalizers, delete dead model_endpoints, keep examples in sync, and validate a full card on gateway-only routing.

### Tests (write first — must fail, for code changes)

- [ ] T011 [P] [US3] If a normalizer/strategy is to be removed from code: add/adjust a unit test in `tests/unit/` (or `agent/performer/tests/`) asserting the registry/launch behavior AFTER removal (the removed normalizer is no longer wired; remaining ones still work). Existing self-hosted-layer/normalizer tests must stay green (regression guard).

### Implementation

- [x] T012 [US3] Migrate live `config.yaml` (operational, gitignored): point the self-hosted `model_endpoints`/`modes` used by `compatible` backends at LiteLLM `spark/gpt-oss:120b`/`:20b` with each backend's provider base-URL env → LiteLLM (per T010). Leave intentionally-frontier roles unchanged. **Constraint (from the spread, see compatibility-matrix-results.md):** `spark/qwen2.5-coder:14b` leaks tool-calls into content — do NOT point any tool-using role at it; keep it only for `env_bootstrap` (its non-tool `translate` use), and route any tool-using coder role to `spark/qwen3-coder:30b` (structured tool_calls ✅).
- [x] T013 [US3] Migrate live `routing.yaml` (operational): for each `compatible` backend, re-point its target `base_url` to LiteLLM and convert it to an **observe/passthrough** (no normalizers — just forward + log latency/status + write `capture_dir`), the configurable default per Decision 6 — NOT a deletion. Keep a matrix-proven normalizer only where `normalizers_needed!=[]`. Leave entries for any `incompatible` backend untouched (stay on current routing per FR-013).
- [ ] T013b [US3] Add the observe-passthrough TOGGLE: a config flag (e.g. per-target `strategy: observe` / `direct`, or `selfhosted.observe_passthrough`) so an operator can switch a target between observe-passthrough (default for LiteLLM routing; keeps the coordinare-side tap) and direct (no proxy hop). Unit-test the toggle resolves both ways; default = observe-passthrough.
- [ ] T014 [US3] Delete dead/stale `model_endpoints` from `config.yaml`: the `spark/*` self-hosted refs the namespace no longer serves (`gptoss120-spark`, `gptoss20-spark`, `qwen36`, `qwen25coder`) and Ollama-direct entries no `mode` references post-migration (`gptoss120-ollama`, `qwen3coder30-ollama`, `glm47flash-ollama`). Verify no `mode` has a dangling reference afterward. (Deletes config ENTRIES, not normalizer CODE.)
- [ ] T015 [US3] Keep ALL normalizer/strategy CODE registered-but-dormant (`proxy/normalizers/`, `proxy/launch.py`) — do NOT delete the registry (Decision 6: instant re-enable if LiteLLM regresses); existing normalizer/self-hosted-layer tests stay green. Update `config.example.*.yaml` + `routing.example.yaml` to document the single-gateway + observe-passthrough topology (committed surface).
- [x] T016 [US3] Restart the daemon on the migrated config (`set -a && source .env && set +a`; relaunch via `nohup uv run --env-file .env python -m coordinare --config config.yaml`; verify health on 9090) and drive ONE card through the full lifecycle; confirm every stage's backend reaches LiteLLM (not `192.168.3.30:11434`) and the card completes (SC-005).

**Checkpoint**: fleet consolidated on LiteLLM; no migrated backend targets Ollama-direct; a full card completed.

---

## Phase 6: Polish & Cross-Cutting

- [ ] T017 [P] `.venv/bin/ruff check` on all changed files (`scripts/smoke_backends.py`, any `proxy/` code, tests) — clean.
- [ ] T018 [P] Secret-invariant audit: grep the matrix artifact + new logs/records for the LiteLLM master key / any value-bearing field; assert names/counts/reasons only (SC-006/FR-014).
- [ ] T019 Full regression: `.venv/bin/pytest -q` green (incl. the self-hosted-layer/normalizer suites after any removal); coverage not decreased.
- [ ] T020 Verify config invariants (SC-003/SC-004): grep `config.yaml`/`routing.yaml` → no migrated backend targets `192.168.3.30:11434`; every retained normalizer maps to a matrix row listing it in `normalizers_needed`; no dangling `model_endpoints` refs.
- [ ] T021 Adversarial review before merge (diverse-lens finders + refute-verify): focus on backends NOT regressed, normalizer removals not breaking a non-migrated path, examples in sync with live topology, secret invariant. Resolve findings inline.
- [ ] T022 Mark tasks `[x]`; write a PR description referencing spec 122 + the matrix results (which backends migrated, which shims dropped, which backends skipped + why); rebase on `main` before merge.

---

## Dependencies & Order

- **Setup (T001–T002)** → **Foundational (T003)** → **US2 matrix (T004–T008)** → **US1 reach fixes (T009–T010)** → **US3 migrate (T011–T016)** → **Polish (T017–T022)**.
- US2 is the GATE: US3 acts only on `compatible` rows. A backend that fails the matrix is recorded and NOT migrated (FR-013).
- T012/T013/T014 all edit the live config and are sequential (same files). T015 (code) is parallel to the config edits but its test (T011) comes first.

## Parallel Execution Examples

- T004, T005 (US2 unit tests) in parallel.
- T017, T018 (Polish lint + secret audit) in parallel.
- The matrix run (T008) is sequential by backend (single-request upstream).

## Implementation Strategy

- **MVP = US2 matrix** — it is the empirical "ensure compatibility" deliverable and the gate. It can land and be reviewed before any live migration.
- Then US1 fixes (close reach gaps), then US3 (migrate + retire), then validate a live card. A backend that won't pass stays on its current routing — partial consolidation is still a win.

## Task Count

22 tasks — Setup 2, Foundational 1, US2 5 (incl. 2 tests), US1 2, US3 6 (incl. 1 test), Polish 6.
