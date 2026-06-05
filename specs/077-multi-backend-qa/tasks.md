# Tasks — 077 Diverse Multi-Backend QA Round

**Spec:** [spec.md](./spec.md) | **Plan:** [plan.md](./plan.md) | **Research:** [research.md](./research.md) | **Data model:** [data-model.md](./data-model.md) | **Contracts:** [contracts/](./contracts/) | **Quickstart:** [quickstart.md](./quickstart.md)

5 user stories (US1–US4 = P1 per the full-mapping clarification; US5 = P3). Tests
are included (Constitution II is NON-NEGOTIABLE; coverage gate `--cov-fail-under=90`).
Run pytest via `.venv/bin/pytest`; lint via `.venv/bin/ruff check`.

**Stage-pass rule (clarification):** a stage passes on backend-correctness — its
mapped backend runs, drives `spark/qwen3.6:35b` via LiteLLM, and returns a valid
terminal contract. The card need not merge; model-quality issues are findings.

---

## Phase 1: Setup

- [X] T001 Verify the diverse-backend baseline `config.yaml` (already drafted: codex/claude/hermes/junie endpoints) loads and passes `coordinare.config_validation.validate_config('config.yaml')` with `.env` sourced; confirm the backup `config.yaml.bak.*` is retained
- [X] T002 [P] Create the findings skeleton `specs/077-multi-backend-qa/findings.md` (one section per backend × role, columns: verdict / evidence / follow-up) to capture US5 results during the live run

## Phase 2: Foundational (Blocking Prerequisites)

- [X] T003 FR-007: ensure an unknown `performers.<role>.backend` (or endpoint `BACKEND`) value is rejected BEFORE dispatch — add/confirm a config-validation check in `src/coordinare/config_validation.py`. Key the supported-name list off the performer factory's canonical keys (`opencode`, `opencode_compat`, `junie`, `claude_code`, `codex`, `hermes`, + `pi` once added) AND first confirm/handle the `BACKEND: claude` → `claude_code` alias normalization (working configs use `claude`); the validation must accept the alias and reject only genuinely-unknown names with an actionable error
- [X] T004 [P] Write `agent/performer/tests/unit/test_backends_factory.py` (or extend if present): assert `get_backend("codex")`/`get_backend("opencode")` resolve, an unknown name raises `UnsupportedBackendError` naming the supported set; add a placeholder asserting `pi` will resolve (xfail/skip until T014–T015 land the adapter + registration)

**Checkpoint:** baseline config validates; unknown-backend rejection is enforced; factory test scaffolding exists. No behavior change yet.

---

## Phase 3: User Story 1 — Diverse per-role routing on one shared model (Priority: P1) 🎯 MVP

**Goal:** every lifecycle role routes to its mapped backend, all on `spark/qwen3.6:35b` via LiteLLM; per-stage backend is observable (FR-001, FR-002, FR-008, FR-009).

**Independent Test:** with the existing backends (junie/codex/claude_code/hermes) mapped across roles, dispatch one card and confirm each stage's container ran the mapped `BACKEND` and the card advances across backend handoffs.

### Tests for User Story 1

- [X] T005 [P] [US1] Contract test in `tests/contract/test_diverse_routing_077.py`: load `config.yaml`, assert each lifecycle role resolves to the expected endpoint + `BACKEND` (assessor→junie, architect/implementer/security→codex, reviewer/qa→claude, tech_writer→hermes) and every role's `model` is `spark/qwen3.6:35b`
- [X] T006 [P] [US1] Contract test in `tests/contract/test_backend_routing_env_077.py`: assert each endpoint's env carries the LiteLLM provider-routing vars for its backend (per `contracts/backend-provider-routing.md` C-5) so no backend can default to a vendor-hosted model

### Implementation for User Story 1

- [ ] T007 [US1] Finalize the 4 working endpoints + per-role `performers` mapping in `config.yaml` (codex/claude/hermes/junie), all `model: spark/qwen3.6:35b`; confirm `env_bootstrap_performer_id` points at a real endpoint
- [ ] T008 [US1] Confirm per-stage backend attribution (FR-009): document the `docker ps --filter ... coordinare.performer_stage` + endpoint-id read in `quickstart.md`; verify the `coordinare.*` labels identify the backend per dispatch
- [ ] T009 [US1] Live MVP run: launch coordinare on `config.yaml`, dispatch card #151 through assess→architect→implement→review→qa→docs with the 4 backends, and verify (a) each stage ran its mapped backend, (b) captured LiteLLM traffic shows `spark/qwen3.6:35b` for every stage (SC-002), (c) card state (branch/PR/relay) survives each cross-backend handoff (FR-008)

**Checkpoint:** diverse routing proven across ≥4 backends on the shared model — shippable MVP independent of Pi/opencode/openclaw.

---

## Phase 4: User Story 2 — New "Pi" backend for the closer (Priority: P1)

**Goal:** closer runs on a new Pi backend reaching the shared model via OpenAI-compatible LiteLLM routing (FR-003, FR-004; SC-003).

### Tests for User Story 2

- [X] T010 [US2] **Feasibility spike (DO FIRST, blocks US2 impl)** — inspect the Pi CLI (https://pi.dev): how it authenticates, how it selects/points at a custom OpenAI-compatible endpoint (config file like codex? env? flag?), and whether it installs into `coordinare-performer:full`. Record the actual mechanism so `PI_PROVIDER_*` (T014) maps to something real; if Pi cannot target an external OpenAI-compatible endpoint, escalate before writing the adapter.
- [X] T011 [P] [US2] Write `agent/performer/tests/unit/test_pi_backend.py`: `PiBackend` satisfies `BackendAdapter` (start/get_status/drain_events/relay_feedback/stop); terminal states map correctly (done/blocked/error → DONE/PARTIAL_PROGRESS/BLOCKED); a "remaining work" report without the partial_progress sentinel is NOT read as DONE (contracts C-4)
- [X] T012 [P] [US2] Add a `PI_PROVIDER_*` routing test (hermetic, mirror `test_claude_code_proxy_env.py`): when `PI_PROVIDER_BASE_URL` is set, the launch targets that endpoint with the key from `PI_PROVIDER_ENV_KEY`, NOT a Pi-hosted model (contracts C-2)
- [X] T013 [P] [US2] Update `test_backends_factory.py`: `get_backend("pi")` returns a `PiBackend` instance (remove the T004 skip/xfail)

### Implementation for User Story 2

- [X] T014 [US2] Implement `agent/performer/src/performer/backends/pi.py` — `PiBackend` to the `BackendAdapter` protocol, modeled on `CodexBackend` and the T010 spike findings: invoke the Pi CLI per turn, write/launch with a `PI_PROVIDER_BASE_URL` OpenAI-compatible override → LiteLLM, parse output into `BackendStatus` with the terminal contract
- [X] T015 [US2] Register `"pi": ("performer.backends.pi", "PiBackend")` in `agent/performer/src/performer/backends/__init__.py` `supported_backends`; add `pi` to the coordinare-side supported-backend list (T003)
- [X] T016 [US2] Make the Pi CLI available **via the entrypoint** (matching the existing per-backend install pattern in `agent/performer/entrypoint.sh`: codex/claude/opencode/junie/hermes each install/upgrade their CLI on start), NOT baked into the image. Added a `pi)` case running `npm install -g @mariozechner/pi-coding-agent` (warn-and-continue, like codex). Validated: clean `coordinare-performer:full` + that line → `pi 0.73.1` runnable; entrypoint `sh -n` clean.
- [X] T017 [US2] Wire a `pi-ephemeral` endpoint (`roles: [closer]`, `BACKEND: pi`, `PI_PROVIDER_*` env) in `config.yaml` and set `performers.closer.backend: pi` / `model: spark/qwen3.6:35b`
- [ ] T018 [US2] Live validation: dispatch a closing-review stage on Pi; confirm it authenticates, drives `spark/qwen3.6:35b` via LiteLLM (captured traffic), and returns a parseable terminal outcome (no unrecognized-output stall) — SC-003; record the verdict in `findings.md`

---

## Phase 5: User Story 3 — (withdrawn)

> A backend candidate evaluated for this round could not target a custom
> OpenAI-compatible endpoint (it routed only through a vendor-hosted gateway), so
> it could not drive the shared self-hosted `spark/qwen3.6:35b` model (FR-002) and
> was removed from the codebase. Tasks **T019–T024 are dropped**; the numbers are
> retained as a gap so T025+ keep their labels.

---

## Phase 6: User Story 4 — opencode handles env_bootstrap (Priority: P1)

**Goal:** env_bootstrap runs on opencode; dev-env install completes and a service-inference timeout stays non-fatal (FR-006; SC-005).

### Tests for User Story 4

- [X] T025 [P] [US4] Add/confirm a test that the env_bootstrap terminal path is non-fatal on inference timeout for any backend (the 076 T175 behavior is backend-agnostic) — assert opencode bootstrap completes-with-inference-skipped rather than error, in `agent/performer/tests/unit/`. DONE: extended `test_service_inference_helper.py` with `test_inference_timeout_is_non_fatal` — `_run_service_inference` catches a `TimeoutError` from `infer_services` and returns `inference_skipped_reason="unexpected_error: TimeoutError"` (never raises). The helper runs in `main.py` independent of `BACKEND`, so it covers the opencode bootstrap path.

### Implementation for User Story 4

- [X] T026 [US4] Wire an `opencode-ephemeral` endpoint (`roles: [env_bootstrap]`, `BACKEND: opencode`) in `config.yaml`; set `performers.env_bootstrap.backend: opencode` and `symphony.env_bootstrap_performer_id: opencode-ephemeral`. DONE: added the `opencode-ephemeral` endpoint with `OPENCODE_PROVIDER_*` (→ LiteLLM) + the `COORDINARE_INFERENCE_*` service-inference env (moved off claude-ephemeral); `OpenCodeAdapter.start()` now writes `opencode.json` from those vars (opt-in, mirrors `PiBackend._write_provider_config`) and routes `modelID = litellm/spark/qwen3.6:35b` — **no `~/.opencode` login mount needed** (POC R-03). Covered by routing tests in `test_opencode.py` (the env prefix is keyed off `adapter_name`). The entrypoint `opencode)` arm already installs the CLI. `validate_config(config.yaml)` passes (FR-007 accepts opencode).
- [X] T027 [US4] Document the opencode provider routing → LiteLLM in `config.example.opencode.yaml` and `quickstart.md`. DONE: documented BOTH auth modes in `config.example.opencode.yaml` — (A) mounted `~/.opencode` creds for vendor models, (B) the 077 self-hosted path via `OPENCODE_PROVIDER_*` (adapter writes `opencode.json`, **no login mount**, key from `{env:LITELLM_MASTER_KEY}`), with a full mode-(B) endpoint example. Updated `quickstart.md` prereqs (opencode needs no mount) and the mapping section (opencode-ephemeral carries the `COORDINARE_INFERENCE_*` env).
- [ ] T028 [US4] Live validation: trigger a cold env_bootstrap on opencode; confirm the dev-env install completes, the env cache is marked ready, and a service-inference timeout does not fail the bootstrap (SC-005); record verdict in `findings.md`

---

## Phase 6b: User Story 6 — OpenClaw handles the reviewer (Priority: P1)

**Goal:** the `reviewer` role runs on OpenClaw, driving `spark/qwen3.6:35b` via
LiteLLM with a valid terminal contract (FR-012). Added mid-round on the active
branch.

- [X] T035 [US6] **POC (DID FIRST, gated the adapter)** — install `openclaw` (npm) into `coordinare-performer:full`, configure a custom `openai-completions` provider in `~/.openclaw/openclaw.json` + model allowlist, run `openclaw agent --local --json` against LiteLLM. DONE: OpenClaw 2026.5.27 returned `pong` with `executionTrace.winnerModel=spark/qwen3.6:35b`; recipe + JSON contract recorded in research.md R-07.
- [X] T036 [P] [US6] Write `agent/performer/tests/unit/backends/test_openclaw_backend.py`: protocol conformance, `_extract_final_text`, the `openclaw agent --json` terminal contract (`payloads[0].text` / `meta.stopReason`), and `OPENCLAW_PROVIDER_*` routing (writes `~/.openclaw/openclaw.json` with `${LITELLM_MASTER_KEY}` interpolation + `<name>/<model>` allowlist, prefixes the model, never a vendor-hosted model). DONE: 12 tests pass.
- [X] T037 [US6] Implement `OpenClawBackend` (`backends/openclaw.py`) to the protocol, modeled on Hermes/Pi (one-shot embedded agent). Writes the provider config from `OPENCLAW_PROVIDER_*`, runs `openclaw agent --local --json --session-key … --model <provider>/<model>`, parses the `{payloads,meta}` JSON (done on `stopReason=="stop"`, error otherwise). Reuses `opencode._build_task_prompt` (persona-in-body + reviewer JSON-only contract). Register `"openclaw"` in the factory; add the entrypoint `openclaw)` npm install arm; add `"openclaw"` to `SUPPORTED_PERFORMER_BACKENDS` (FR-007). DONE.
- [X] T038 [US6] Wire an `openclaw-ephemeral` endpoint (`roles: [reviewer]`, `BACKEND: openclaw`, `OPENCLAW_PROVIDER_*` → LiteLLM) in `config.yaml`; set `performers.reviewer.backend: openclaw` (moved reviewer off claude-ephemeral; qa stays on claude_code). DONE: `validate_config(config.yaml)` passes.
- [ ] T039 [US6] Live validation: dispatch a review stage on the openclaw endpoint; confirm captured traffic targets `spark/qwen3.6:35b` (no vendor fallback) and the adapter reports a terminal outcome — record verdict in `findings.md`. (Needs `coordinare-performer:full` rebuilt with the new `openclaw)` entrypoint arm.)

---

## Phase 7: User Story 5 — Per-backend findings report (Priority: P3)

**Goal:** a written verdict per backend exercised (FR-010; SC-007).

- [ ] T029 [US5] Run the FULL diverse mapping end-to-end on card #151 (all 7 role-backends — junie/codex/claude_code/openclaw/hermes/opencode/pi): each stage on its mapped backend, all on `spark/qwen3.6:35b`
- [ ] T030 [US5] Complete `findings.md`: for each backend × role, record verdict (`contract-respecting` / `needs-fix`), evidence (log events / branch artifacts / captured LiteLLM traffic), and any follow-up task — mirroring spec 076's "Phase 9 live-test fixes" format

---

## Phase 8: Polish & Cross-Cutting

- [X] T031 Run `.venv/bin/ruff check src tests agent/performer/src agent/performer/tests` and fix all findings. DONE: all checks passed (clean).
- [X] T032 Run `bin/build --docker` green: lint, unit, coverage ≥90%, performer tests, base+full image rebuild (SC-007 size gap + rtk checks pass). DONE: all 12 steps ✓. Rebuilt `coordinare-performer:base`/`:full` bake in `openclaw.py`, the cursor removal, and the new `entrypoint.sh`. Smoke-tested the fresh `:full`: `BACKEND=openclaw` installs OpenClaw 2026.5.27 and `BACKEND=pi` installs pi 0.73.1 via the entrypoint; `cursor` is rejected by the factory. (`--e2e` Playwright tests skipped — not needed for the round; run separately if desired. Note: pi npm pkg deprecation `@mariozechner/pi-coding-agent` → `@earendil-works/pi-coding-agent` flagged for a future rename.)
- [X] T033 [P] Document any NEW structured log events introduced by the Pi/OpenClaw backends in the project's event vocabulary (only if added). DONE (no-op): Pi/OpenClaw emit only existing `BackendEventType` values (`progress`/`tool_use`/`error`) — no new vocabulary added.
- [~] T034 [P] Update memory roadmap to mark 077 as the latest landed spec; ensure `config.example.*` for opencode/pi/openclaw reflect the provider-routing contract. PARTIAL: added `config.example.openclaw.yaml` + `config.example.pi.yaml` (provider-routing recipe; `config.example.opencode.yaml` already updated). Memory "latest landed spec" marker DEFERRED until the live run lands the round.

### Phase 8b: CI-gate / persona scoping fix (FR-013 — live finding from #93)

- [X] T040 Decouple `persona_check_map` (075 implementer CI gate) from 074 persona-scope tiering. DONE: added a depth-agnostic `any` glob list to `PersonaCheckMapPerDepth` (`src/coordinare/config.py`); `required_checks_resolver.resolve()` now consults `persona_check_map` even when `scope` is None — using the depth-specific list when a 074 scope/depth exists, else falling back to `any`. So the implementer gate can be scoped per-persona WITHOUT enabling the classifier. Added a config-validation hint when depth-only maps are set with tiering off.
- [X] T041 [P] Resolver tests for the decoupled behavior in `tests/unit/services/test_required_checks_resolver.py`: `any` used without scope; `any` fallback when depth list empty; depth-specific precedence over `any`; `any` empty-intersection → branch_protection. DONE: 4 tests added; 3409 coordinare unit+contract green.
- [ ] T042 Live validation: with the implementer gate scoped (exclude `Feature tests*`), confirm a card advances past the implementer on its scoped checks and exercises the downstream reviewer/qa/docs/closer backends — record in `findings.md`.

---

## Dependencies

```
Phase 1 (Setup) ─→ Phase 2 (Foundational) ─┬─→ Phase 3 (US1, MVP) ─┐
                                            │                       │
                                            ├─→ Phase 4 (US2 Pi: T010 spike → impl) ───┤
                                            ├─→ Phase 5 (US3 — withdrawn) ─────────────├─→ Phase 7 (US5 findings) ─→ Phase 8 (Polish)
                                            ├─→ Phase 6 (US4 opencode) ────────────────┤
                                            └─→ Phase 6b (US6 OpenClaw) ───────────────┘
```

- US1 is the MVP and is independently demonstrable with the existing backends before US2–US4 land.
- **US2 begins with the T010 Pi spike** which gates its implementation. (US3 was withdrawn after its feasibility spike — the candidate backend could not target a self-hosted endpoint.)
- US2 (Pi), US4 (opencode), US6 (OpenClaw) are independent at the **code** layer (`pi.py`, opencode adapter, `openclaw.py` touch disjoint files) and can proceed in parallel after Phase 2.
- **`config.yaml` is a serialization point**: the per-backend endpoint edits (T007 baseline, T017 pi, T026 opencode, T038 openclaw) all touch the same file and must NOT run concurrently, even though the backend code is parallel.
- US5 (findings) depends on US1–US4 being live (it runs the full mapping).
- Phase 8 polish depends on all implementation phases.

## Parallel Execution Opportunities

- **Phase 2:** T004 [P] alongside T003.
- **Phase 3:** T005, T006 [P] (independent test files).
- **Phase 4:** after the T010 spike, T011, T012, T013 [P] (independent test files) before T014–T018.
- **US2 / US4 / US6 code can run in parallel** — `pi.py`, the opencode adapter, and `openclaw.py` touch disjoint files. **Exception:** their `config.yaml` endpoint edits (T017 / T026 / T038) serialize (shared file).
- **Phase 8:** T033, T034 [P].

## Implementation Strategy

1. **MVP (Phase 1–3):** prove diverse routing across the 4 already-working backends on the shared model — shippable, validates FR-001/FR-002/FR-008/FR-009 and SC-001's routing core.
2. **Backend integrations (Phase 4–6b, parallel):** land Pi (US2), opencode env_bootstrap (US4), OpenClaw reviewer (US6) — each begins with a feasibility spike/POC (T010 / T035), then unit/contract tests, then a live per-stage validation. (US3 was withdrawn after its spike.)
3. **Full round + findings (Phase 7):** run the complete mapping on #151 and write the per-backend findings report.
4. **Polish (Phase 8):** lint, `bin/build --all` green (coverage ≥90%), docs/examples updated.

## Task Index Summary

| Phase | Task IDs | Count | Story |
|---|---|---|---|
| 1 — Setup | T001–T002 | 2 | — |
| 2 — Foundational | T003–T004 | 2 | — |
| 3 — US1 (routing, MVP) | T005–T009 | 5 | US1 |
| 4 — US2 (Pi backend; T010 spike) | T010–T018 | 9 | US2 |
| 5 — US3 (withdrawn) | — | 0 | US3 |
| 6 — US4 (opencode bootstrap) | T025–T028 | 4 | US4 |
| 7 — US5 (findings) | T029–T030 | 2 | US5 |
| 8 — Polish | T031–T034 | 4 | — |
| **Total** | | **34** | |
