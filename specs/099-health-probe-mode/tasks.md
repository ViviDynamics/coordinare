# Tasks: Completion-Style Health-Probe Mode for Non-Tool-Calling Backends

**Input**: Design documents from `/specs/099-health-probe-mode/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/health-probe-mode.md, quickstart.md

**Tests**: INCLUDED — Constitution II (TDD, non-negotiable).

**Organization**: by user story (US1 P1 completion-probe mechanism → US2 P1 default-unchanged regression → US3 P2 junie activation recipe). Performer-side only; reuses `gate()` + the normalizer chain. **No new deps, no coordinare state, no schema change.**

## Path Conventions

Performer: `agent/performer/src/performer/proxy/`. Tests: `agent/performer/tests/unit/proxy/`.

---

## Phase 1: Setup

- [X] T001 Confirm injection points by reading: `agent/performer/src/performer/proxy/routing.py` (`TargetDescriptor`: frozen pydantic, `Literal` `wire_format`/`strategy`, `extra="forbid"`, ~L49-59) and `agent/performer/src/performer/proxy/health.py` (`_probe_body` ~L131, `_probe_url` ~L115, `_has_structured_tool_call` ~L194, `_translate_round_trip_has_tool_use` ~L173, `check_health` ~L215-276, `gate` + the `proxy.health` log ~L266). Note exactly where the mode branch + new helper hook.

---

## Phase 2: Foundational (config field — blocks all stories)

- [X] T002 Write FAILING test `agent/performer/tests/unit/proxy/test_routing_health_probe.py`: (a) a `TargetDescriptor` with no `health_probe` defaults to `"tool_call"`; (b) `health_probe: "completion"` is accepted; (c) `health_probe: "bogus"` raises a pydantic validation error at construction / `RoutingTable.from_yaml_file` (FR-006 fail-fast). MUST fail before T003.
- [X] T003 Add `health_probe: Literal["tool_call", "completion"] = "tool_call"` to `TargetDescriptor` in `agent/performer/src/performer/proxy/routing.py` (the frozen + `extra="forbid"` model gives fail-fast on an invalid value for free). Make T002 pass.

**Checkpoint**: the probe mode is a validated, default-safe config field.

---

## Phase 3: User Story 1 — Completion-mode health gating (Priority: P1) 🎯 MVP

**Goal**: a `completion`-mode target is judged healthy on a non-empty normalized completion (no tools), fail-closed on empty/broken. **Independent test**: completion-mode + normal completion → healthy/proceed; + empty/non-200/timeout → unhealthy/fail_closed; + reasoning-only (promoted by normalizers) → healthy.

- [X] T004 [US1] Write FAILING tests `agent/performer/tests/unit/proxy/test_health_completion.py` (use respx/mock upstream like `test_health.py`): (a) completion-mode target, upstream returns non-empty `choices[0].message.content` → `check_health` → healthy/proceed; (b) upstream returns empty content → unhealthy → fail_closed (no `reroute_upstream`); (c) non-200 / timeout → unhealthy; (d) upstream returns empty content + populated `reasoning_content` with `strip_reasoning` in normalizers → healthy (normalize-then-judge, FR-004); (e) the probe body sent in completion mode carries NO `tools`/`tool_choice`. MUST fail before T005/T006/T007.
- [X] T005 [US1] In `agent/performer/src/performer/proxy/health.py`, extend `_probe_body(target, model)`: when `target.health_probe == "completion"`, build a trivial no-tools chat-completion body (single short user message, small `max_tokens`, no `tools`/`tool_choice`); for `strategy == "translate"` still route a representative Anthropic body through `translate_request` but omit the tool injection. `_probe_url` + `model`/`upstream_model` 404-avoidance unchanged. Make T004(e) pass.
- [X] T006 [US1] Add `_has_nonempty_completion(target, body) -> bool` in `health.py`: run `body` through the target's declared normalizers (mirror `_translate_round_trip_has_tool_use`), then check for non-empty assistant content (OpenAI: `choices[0].message.content` non-empty str; anthropic/translate: a non-empty text block). Make T004(d) reachable.
- [X] T007 [US1] Branch `check_health` on `target.health_probe`: completion mode uses `_has_nonempty_completion` for the 200-body success criterion (else unhealthy with reason "probe response carried no usable completion"); tool-call mode unchanged. Feed the existing `gate()`. Make T004(a)-(d) pass.

**Checkpoint**: US1 independently testable — a non-tool-calling target is honestly health-gated; the MVP that unblocks routing junie.

---

## Phase 4: User Story 2 — Existing tool-call targets unchanged (Priority: P1)

**Goal**: zero regression for any target that declares no `health_probe`. **Independent test**: existing reviewer/qa-style targets produce identical health decisions.

- [X] T008 [US2] Write FAILING/REGRESSION tests in `test_health_completion.py` (or extend `test_health.py`): (a) a target with no `health_probe` still sends the tools probe and gates healthy ONLY on a structured tool call (unchanged); (b) the same tool-call target with a harmony leak + no reassembler → unhealthy (unchanged); (c) confirm the default value path is exercised (no `health_probe` key → tool_call). These must pass with the existing tool-call code paths untouched.
- [X] T009 [US2] Verify (and adjust only if needed) that T005/T007 changes are strictly additive — the `tool_call` branch is byte-for-byte the prior behavior. Run the full existing `test_health.py` + `test_health_translate.py` suites; all must stay green (FR-003/SC-002).

**Checkpoint**: US2 independently verified — default tool-call path is regression-free.

---

## Phase 5: User Story 3 — Junie activation recipe (Priority: P2)

**Goal**: prove that a completion-mode normalize target makes the junie assessor routable through the 098 normalizers (the FR-007 enablement); document the activation recipe.

- [X] T010 [US3] Write a test `agent/performer/tests/unit/proxy/test_health_completion.py::test_junie_style_completion_target_admitted`: a target shaped like the junie entry (backend junie, strategy normalize, `health_probe: completion`, normalizers `[strip_control_chars, strip_reasoning]`, base_url Ollama origin) → healthy on a normal gpt-oss-style completion; unhealthy/fail_closed on a persistently empty body. Proves FR-007/SC-004 at the layer.
- [X] T011 [US3] Add the `mode` field to the `proxy.health` log record in `check_health` (`mode=target.health_probe`) and assert it in a test (structlog capture) carrying only mode + method/path/status/resolved_action — no tokens/bodies (FR-008/SC-005).
- [X] T012 [US3] Confirm `quickstart.md` Scenario F (the routing.yaml junie entry + junie-ephemeral mount + image rebuild + restart) is accurate against the merged 098 normalizer keys and current config.yaml mount pattern; this is the operator activation recipe (doc only, no code).

**Checkpoint**: US3 — the enablement is proven at the layer; the activation recipe is documented (ops step, not auto-run).

---

## Phase 6: Polish & Cross-Cutting

- [X] T013 [P] Full performer proxy suite (`agent/performer/tests/unit/proxy/`) green — health, health_translate, launch, normalizers, classifier, assembler; confirm no other strategy/backend path changed.
- [X] T014 Walk `quickstart.md` A–F; confirm each SC (SC-001…SC-006) has a covering test; verify the decision record is secret-free and no new external dependency.
- [X] T015 `.venv/bin/ruff check` (and the performer lint path) on all edited files; fix nits. Confirm `routing.example.yaml` documents the new `health_probe` field for operators.
- [X] T016 A couple of adversarial review rounds (diverse-lens + refute-verify) before merge — focus: default-unchanged (no regression to tool-call gating), completion success criterion (empty vs whitespace vs reasoning-only), normalize-then-judge correctness, fail-closed preserved on empty/timeout, config fail-fast, secret-free record, other strategies/targets untouched.

---

## Dependencies & Execution Order

- **Phase 2 (config field)** blocks all stories.
- **US1 (P1)** = MVP (completion-probe mechanism). Depends on Phase 2.
- **US2 (P1)** = regression guard; verifiable alongside US1 (same files).
- **US3 (P2)** depends on US1 (the mechanism) + the merged 098 normalizers.
- **Polish** last.

## Parallel Opportunities

- T013 [P] independent suite run.
- US1 implementation (T005-T007) and US2 regression (T008-T009) touch the same files → sequential, not parallel.

## Implementation Strategy

MVP-first: ship **US1** (the completion-probe mode + non-empty-normalized-content criterion) — it alone unblocks routing a non-tool-calling backend through the layer, which is the whole point. **US2** is the regression guard proving the tool-call default is untouched (same change, verified). **US3** proves the junie enablement at the layer and documents the operator activation recipe (the actual routing entry + mount + image rebuild are ops steps, not code). Each phase is an independently testable increment; existing routed targets are unchanged.
