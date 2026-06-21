# Tasks: Route the hermes (tech_writer) Backend Through the Self-Hosted Shim

**Input**: Design documents from `/specs/100-hermes-shim-routing/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/hermes-shim-routing.md, quickstart.md

**Tests**: INCLUDED — Constitution II (TDD, non-negotiable).

**Organization**: by user story (US1 P1 normalize-before-parse → US2 P1 completion-gating → US3 P2 wire-path). Performer-side only; reuses the 098 normalizers + 099 completion probe + the existing shim/launch seam. **No new normalizer, no new deps, no coordinare state.**

## Path Conventions

Performer: `agent/performer/src/performer/proxy/`. Tests: `agent/performer/tests/unit/proxy/`.

---

## Phase 1: Setup

- [X] T001 Confirm injection points by reading `agent/performer/src/performer/proxy/launch.py` (`UNSUPPORTED_BACKENDS` ~L61, `PROVIDER_BASE_URL_ENV` ~L48, `_with_verbatim_wire_path` ~L80, `_launch_for_target` normalize branch ~L191 + the `mapping[env_var] = …` loopback-set, `maybe_launch_proxy` ~L237), `proxy/shim.py` served front-door paths (`/v1/chat/completions`), `proxy/routing.py` `TargetDescriptor.health_probe`, `proxy/health.py` completion probe (099), and `backends/hermes.py` `HERMES_BASE_URL` custom-provider config (~L433). Note exactly where eligibility + the `/v1` loopback prefix hook.

---

## Phase 2: Foundational (launch eligibility + /v1 loopback base — blocks all stories)

- [X] T002 Write FAILING test `agent/performer/tests/unit/proxy/test_launch_hermes.py`: a routing table with a `(hermes, spark/gpt-oss:120b)` normalize entry → `maybe_launch_proxy(None, "hermes", env, routing_table=table, model="spark/gpt-oss:120b")` returns a shim (does NOT raise ProxyLaunchError "unsupported"), sets `env["HERMES_BASE_URL"]`, and the value ends with `/v1` (the shim loopback + the prefix hermes's CLI appends `/chat/completions` to). MUST fail before T003/T004.
- [X] T003 In `proxy/launch.py`: remove `"hermes"` from `UNSUPPORTED_BACKENDS`; add `"hermes": "HERMES_BASE_URL"` to `PROVIDER_BASE_URL_ENV`. Make the "eligible / not-unsupported" part of T002 pass.
- [X] T004 In `proxy/launch.py`: hand hermes a loopback base with a `/v1` prefix where the provider env is set (normalize + translate branches), via a per-backend base-prefix (e.g. `PROVIDER_BASE_PATH_PREFIX = {"hermes": "/v1"}`) applied alongside the existing `_with_verbatim_wire_path` (junie's verbatim full path stays unchanged; hermes gets only the `/v1` prefix because its CLI appends `/chat/completions`). Make T002's `/v1` assertion pass.

**Checkpoint**: hermes is routable and addressed correctly; eligibility unit-tested.

---

## Phase 3: User Story 1 — Normalize before hermes parses (Priority: P1) 🎯 MVP

**Goal**: a routed hermes target applies the 098 normalizer chain so control-char / reasoning-wrapped output is cleaned before hermes's strict parser. **Independent test**: the launched hermes shim carries `[strip_control_chars, strip_reasoning]`; a dirty body normalizes clean; a clean body is unchanged.

- [X] T005 [US1] Write FAILING test in `test_launch_hermes.py`: the shim launched for the hermes normalize entry has `[n.key for n in shim.normalizers] == ["strip_control_chars", "strip_reasoning"]`. MUST fail before T006 (it fails today because hermes can't launch a shim at all).
- [X] T006 [US1] Verify (no code beyond Phase 2) the normalize branch resolves the declared normalizers for hermes exactly as for other backends; add an assertion test that a control-char + reasoning-only body fed through the resolved chain (`SelfHostedShim.normalize_json` / `normalize_raw`) yields clean parseable content (reuse the 098 normalizer behavior — no new normalizer). Make T005 pass.

**Checkpoint**: US1 — a routed hermes gets its output normalized before parse; the MVP that unblocks documenting.

---

## Phase 4: User Story 2 — Completion health probe for hermes (Priority: P1)

**Goal**: the hermes target is gated by the completion probe (non-tool-calling), healthy on a normal completion, fail-closed on empty/broken. **Independent test**: hermes target + `health_probe: completion` → healthy on a non-empty completion, unhealthy/fail-closed on empty/non-200.

- [X] T007 [US2] Write FAILING/REGRESSION test `agent/performer/tests/unit/proxy/test_health_hermes.py` (mirror `test_health_completion.py`): a hermes-shaped target (`strategy: normalize`, `health_probe: completion`, `normalizers: [strip_control_chars, strip_reasoning]`, base_url the LiteLLM endpoint) → `check_health` healthy on a non-empty completion; unhealthy → fail_closed on empty body / non-200 / timeout. MUST pass via the existing 099 probe (no new code) — confirms hermes composes with 099.
- [X] T008 [US2] Confirm no code change needed beyond Phase 2 (099 completion probe is backend-agnostic); if a gap surfaces, fix minimally. Run `test_health.py` + `test_health_completion.py` — all green (no regression to tool-call or junie completion gating).

**Checkpoint**: US2 — hermes is honestly health-gated, not fail-closed for "no tool call".

---

## Phase 5: User Story 3 — Correct wire path (Priority: P2)

**Goal**: hermes's appended `/chat/completions` hits a served route; misaddress caught at probe. **Independent test**: loopback base ends `/v1`; probe path is the served `/v1/chat/completions`.

- [X] T009 [US3] Write a test in `test_launch_hermes.py`: the `HERMES_BASE_URL` handed to hermes is `<127.0.0.1:port>/v1` (so the CLI's appended `/chat/completions` → `/v1/chat/completions`, a served front-door path); and `_probe_url` for the hermes target resolves to the same served path. Assert junie's verbatim full-path suffix is unchanged (no cross-contamination).

**Checkpoint**: US3 — addressing is correct and probe-verified.

---

## Phase 6: Polish & Cross-Cutting

- [X] T010 [P] Default-unchanged regression: with NO hermes routing entry, `maybe_launch_proxy(None, "hermes", env)` is a no-op (env untouched), and an existing hermes-without-routing path is unchanged (FR-005/SC-004). Confirm `UNSUPPORTED_BACKENDS` emptied doesn't change any other backend's resolution.
- [X] T011 [P] Full performer proxy suite (`agent/performer/tests/unit/proxy/`) green — launch, launch_translate, health, health_completion, normalizers, routing; confirm no other backend/pair changed.
- [X] T012 Add the hermes entry to `routing.example.yaml` (documented, with the completion-probe + normalizers + LiteLLM base_url) for operators; confirm `:full`/`:extra` schema-drift guard (#137) still parses it.
- [X] T013 `.venv/bin/ruff check` (+ performer lint) on all edited files; fix nits. Walk `quickstart.md` A–E; confirm each SC has a covering test.
- [X] T014 A couple of adversarial review rounds (diverse-lens + refute-verify) before merge — focus: default-unchanged (UNSUPPORTED_BACKENDS emptied doesn't break other backends), the `/v1` prefix vs junie's verbatim path (no cross-contamination), completion gating fail-closed preserved, secret-free records, hermes auth/profile (HERMES_API_KEY) still works through the repointed base, and the env-restore on stop().

---

## Dependencies & Execution Order

- **Phase 2 (eligibility + /v1 base)** blocks all stories.
- **US1 (P1)** = MVP (normalize-before-parse); depends on Phase 2.
- **US2 (P1)** = completion gating; reuses 099, verifiable alongside US1.
- **US3 (P2)** = wire-path correctness; part of Phase 2's base construction, tested here.
- **Polish** last.

## Parallel Opportunities

- T010 / T011 [P] (independent regression checks).
- US2 health tests (T007) parallel with US1 launch tests (T005) — different files.

## Implementation Strategy

MVP-first: Phase 2 (eligibility + `/v1` loopback base) + **US1** (the 098 normalizer chain applied to hermes) is the core that unblocks documenting — it reuses the proven junie chain, so the only new code is the launch-seam eligibility + the `/v1` base prefix. **US2** confirms 099's completion probe composes with hermes (no new code expected). **US3** verifies addressing. Polish guards the default-unchanged contract and other backends. Activation (routing entry + mount + image rebuild) is config/ops, documented in quickstart.
