---

description: "Task list for Persona Scope Tiering (spec 074)"
---

# Tasks: Persona Scope Tiering

**Input**: Design documents from `/specs/074-persona-scope-tiering/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Test tasks are included — the plan's Constitution Check explicitly commits to unit, contract, integration, and regression coverage (Testing Discipline is NON-NEGOTIABLE), and FR-011 calls out the `_SESSION_FIELDS` round-trip as regression-prone.

**Organization**: Tasks grouped by user story per spec.md priorities (US1/US2/US3 are P1; US4/US5 are P2). Each story is independently testable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Different file, no dependency on uncompleted tasks
- **[Story]**: Maps to spec.md user story; setup/foundational/polish phases have no story label
- All paths absolute-relative to repo root `~/Workspaces/ViviDynamics/coordinare`

---

## Phase 1: Setup

**Purpose**: No new project init — existing coordinare repo. Only branch + tooling sanity.

- [X] T001 Confirm working on branch `074-persona-scope-tiering` and `.venv` is active; verify `.venv/bin/pytest --version` and `.venv/bin/ruff --version` succeed (per memory: pyenv shim issues with `python -m pytest`).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Config schema, persistence, and classifier-service skeleton must land before any user story can wire behavior. These changes are touched by every story.

**⚠️ CRITICAL**: No user-story work begins until Phase 2 is complete.

- [X] T002 [P] Add `ScopeTierBehavior` and `ScopeBehavior` pydantic models in `src/coordinare/config.py` per `contracts/config-schema.md` (with `extra="forbid"` on `ScopeBehavior`).
- [X] T003 [P] Extend `PersonaConfig` in `src/coordinare/config.py` with optional `scope_behavior: ScopeBehavior | None = None` (per `data-model.md`, extends existing class at line 159).
- [X] T004 Add `PersonaScopeConfig` pydantic model in `src/coordinare/config.py` with `enabled`, `path_classes`, `forced_full_on_path_classes`, `classifier_latency_budget_seconds`, `classifier_failure_warning_cooldown_seconds`, plus the `_validate_globs` field validator and `_classes_in_forced_full_must_exist` model validator from `contracts/config-schema.md`. (Depends on T002.)
- [X] T005 Wire `persona_scope: PersonaScopeConfig | None = None` into the top-level `SymphonyConfig` (or equivalent root) in `src/coordinare/config.py` so YAML `symphony.persona_scope:` parses.
- [X] T006 [P] Add `PersonaScope` and `PersonaScopeSlice` TypedDicts in `src/coordinare/session.py` per `data-model.md`.
- [X] T007 Add `persona_scope: PersonaScope | None` field to `CardSession` TypedDict at `src/coordinare/session.py:28`; initialize to `None` in `create_session_from_card()`; add to `_SESSION_FIELDS` at `src/coordinare/session.py:109`; ensure `session_to_state` / `state_to_session` round-trip the field. (Depends on T006.)
- [X] T008 [P] [US5] Unit test `tests/unit/test_session.py::test_persona_scope_round_trips` — verify `persona_scope` survives `session_to_state` → `state_to_session` (mirrors the four existing 069/072/073 regression tests). FR-011 regression guard.
- [X] T009 Bump `CURRENT_SCHEMA_VERSION` from 3 to 4 in `src/coordinare/state_store.py`; leave `MIN_SUPPORTED_SCHEMA_VERSION` at 1; add optional `persona_scope: PersonaScope | None = None` to `PersistedSession`; ensure v1–v3 snapshots load with `persona_scope = None`.
- [X] T010 [P] Unit test `tests/unit/test_state_store.py::test_v3_snapshot_loads_with_null_persona_scope` — load a fixture v3 snapshot and assert `persona_scope is None` on every rehydrated session.
- [X] T011 [P] Unit test `tests/unit/test_state_store.py::test_v4_snapshot_roundtrips_persona_scope` — serialize a session with a populated `PersonaScope`, reload, assert deep equality.
- [X] T012 Create empty service module `src/coordinare/services/persona_classifier.py` with the public surface: `async def classify(session, conducting_backend, config) -> PersonaScope | None` returning `None` for shadow-mode/unimplemented (placeholder; real body lands in US1/US3).
- [X] T013 [P] Add `_match_path_classes(file_path: str, path_classes: dict[str, list[str]]) -> list[str]` helper in `src/coordinare/services/persona_classifier.py` (pure function, no I/O) using `fnmatch`/`pathlib` glob semantics.
- [X] T014 [P] Unit test `tests/unit/services/test_persona_classifier.py::test_match_path_classes` — covers single-class match, multi-class match, no match, malformed glob; locks the matching semantics referenced by R8.
- [X] T015 Add new graph node skeleton `src/coordinare/graph/nodes/classify_scope.py` exporting `async def classify_scope_node(state) -> dict` returning the unchanged state when `persona_scope.enabled is False` (no-op default for opt-in feature per FR-010).
- [X] T016 Wire `classify_scope_node` into `src/coordinare/graph/builder.py` between `dispatch_card` and `dispatch_performer`.
- [X] T017 [P] [US5] Unit test `tests/unit/test_config.py::test_persona_scope_disabled_by_default` — load a minimal YAML without a `persona_scope` block, assert config parses and `persona_scope is None` (FR-010 additive default).
- [X] T018 [P] [US5] Unit test `tests/unit/test_config.py::test_forced_full_unknown_class_rejected` — load a YAML where `forced_full_on_path_classes` references a class not in `path_classes`; assert pydantic ValidationError (per `contracts/config-schema.md` validation rules).

**Checkpoint**: Config + persistence + graph wiring in place. Pipeline still behaves exactly like today because `enabled` defaults to `False` and the new node is a no-op.

---

## Phase 3: User Story 1 — Docs-only PR skips irrelevant personas (P1) 🎯 MVP

**Goal**: A docs-only card runs reviewer in `skim`, security/qa in `skip`, tech_writer in `full`; lifecycle advances past skipped personas; PR comment shows the rollup.

**Independent Test**: Submit a card whose diff touches only `*.md` files; verify per `spec.md` US1 acceptance scenarios — `PersonaScope` has the expected depths, skipped personas don't invoke their performer, closer ignores depth.

### Tests for User Story 1

- [X] T019 [P] [US1] Contract test `tests/contract/test_persona_classifier_io.py::test_input_schema_shape` — render the classifier input from a synthetic session/PR and assert it matches the JSON schema in `contracts/classifier-prompt.md` (card, pr.files with path/added/removed/status/classes, project_context, personas, path_classes, depth_definitions).
- [X] T020 [P] [US1] Contract test `tests/contract/test_persona_classifier_io.py::test_output_schema_validates` — feed a synthetic classifier JSON response into the parser and assert every persona slice has `{depth, focus, overrides}` with depth ∈ {skim, normal, full, skip}.
- [X] T021 [P] [US1] Contract test `tests/contract/test_persona_classifier_io.py::test_classifier_never_sees_raw_diff` — inspect the rendered prompt and assert the diff body is absent (FR-002 / R1). Asserts only path stats and project context appear.
- [X] T022 [P] [US1] Integration test `tests/integration/test_persona_scope_e2e.py::test_docs_only_card_skips_security_qa` — walk a docs-only card through the graph with a mocked classifier emitting `{reviewer: skim, security: skip, qa: skip, tech_writer: full, closer: <ignored>}`; assert security/qa never get a performer dispatch and the card advances to monitoring.

### Implementation for User Story 1

- [X] T023 [US1] Implement classifier prompt rendering in `src/coordinare/services/persona_classifier.py`: build the input JSON from `card`, the PR file list (path, ±LOC, status, derived `classes` via `_match_path_classes`), `CLAUDE.md`/`AGENTS.md` heads (first 4 KB each), `personas` list, `path_classes`, and the static `depth_definitions` from `contracts/classifier-prompt.md`. Render the system + user prompt templates verbatim from the contract.
- [X] T024 [US1] Implement classifier invocation in `src/coordinare/services/persona_classifier.py`: call `conducting_backend.prompt(system=..., user=..., response_format="json")` wrapped in `asyncio.wait_for(..., timeout=config.classifier_latency_budget_seconds)`; parse JSON; validate against output schema; build `PersonaScope` with `computed_at`, `cycle_index`, `classifier_model`, `head_sha`, `files_summary`, `personas`.
- [X] T025 [US1] Implement post-processing steps 4–6 from `contracts/classifier-prompt.md` in `src/coordinare/services/persona_classifier.py`: append `closer_is_scope_invariant` to closer's overrides; drop unknown persona keys with debug log; fill missing personas with `depth: full, focus: "(no classifier output — defaulting to full)", overrides: ["missing_from_classifier_output"]`.
- [X] T026 [US1] Flesh out `src/coordinare/graph/nodes/classify_scope.py` to call `classify(...)` when `persona_scope.enabled is True` and a PR exists; write result to `session.persona_scope`; emit `persona_scope.classifier.start` (debug) and `persona_scope.classifier.complete` (info) logs per the contract's logging table.
- [X] T027 [US1] Add skip-routing branch in `src/coordinare/graph/routing.py`: when `session.persona_scope.personas[<persona>].depth == "skip"`, advance the lifecycle past that persona without invoking the performer (mirrors the existing assess-skip pattern at `pickup_skips_assess_when_pr_open`). Emit `persona_scope.persona_skipped` debug log.
- [X] T028 [US1] In `src/coordinare/graph/nodes/dispatch_performer.py`: read the persona's scope slice from `session.persona_scope`; if the persona's config has a `scope_behavior` block and the slice's `depth` has a matching tier, apply `max_tool_calls` (cap the performer's tool-call budget) and pass `prompt_addon` as a **structured input field** (NOT mutating the persona base prompt — FR-007). For `closer`, ignore `depth` but pass `focus` as advisory context.
- [X] T029 [US1] Add PR-rollup emission in `src/coordinare/graph/nodes/notify.py`: post (or edit) a deduplicated PR comment summarizing per-persona `depth` + `focus` + any `overrides`. Reuse the existing rollup channel/marker pattern (see 064's pr-checks rollup). Trigger only when `PersonaScope` is first computed OR depth/persona set changes between cycles.
- [X] T030 [P] [US1] Unit test `tests/unit/graph/nodes/test_classify_scope.py::test_disabled_is_noop` — when `persona_scope.enabled is False`, the node returns state unchanged and never calls the backend.
- [X] T031 [P] [US1] Unit test `tests/unit/graph/nodes/test_classify_scope.py::test_writes_persona_scope_to_session` — with `enabled=True` and a mocked backend, assert `session.persona_scope` is populated with the expected shape.
- [X] T032 [P] [US1] Unit test `tests/unit/graph/nodes/test_dispatch_performer_scope.py::test_max_tool_calls_applied` — when a persona's `scope_behavior.skim.max_tool_calls=5` and the slice has `depth: skim`, assert the dispatched performer config receives `max_tool_calls=5`.
- [X] T033 [P] [US1] Unit test `tests/unit/graph/nodes/test_dispatch_performer_scope.py::test_prompt_addon_is_structured_input_not_base_mutation` — assert the persona's base instructions are untouched and the addon appears in a separate structured field (FR-007).
- [X] T034 [P] [US1] Unit test `tests/unit/graph/nodes/test_dispatch_performer_scope.py::test_closer_ignores_depth_consumes_focus` — closer with `depth: skim` still runs at full behavior; the slice's `focus` is passed in (FR-009).
- [X] T035 [P] [US1] Unit test `tests/unit/graph/routing/test_skip_routing.py::test_skip_advances_lifecycle` — depth `skip` advances past the persona without invoking the performer (FR-008).

**Checkpoint**: US1 deliverable. A docs-only card now classifies, skips security/qa, runs tech_writer full, and surfaces the rollup on the PR.

---

## Phase 4: User Story 2 — Security-sensitive paths force full depth (P1)

**Goal**: Any file matching `forced_full_on_path_classes[<persona>]` deterministically overrides the classifier's output to `full` for that persona, with a structured override reason recorded.

**Independent Test**: 6-line diff to `src/auth/middleware.py` with classifier emitting `security: skim` — assert post-processing yields `security: full` with `overrides: ["forced_full_on_path_class:security_sensitive"]` and a `persona_scope.classifier.forced_full` info log.

### Tests for User Story 2

- [X] T036 [P] [US2] Unit test `tests/unit/services/test_persona_classifier.py::test_forced_full_override_applied` — config has `forced_full_on_path_classes: {security: [security_sensitive]}`; diff touches a `security_sensitive`-matched file; classifier output says `security: skim`; assert post-processing flips to `full` and appends `forced_full_on_path_class:security_sensitive` to overrides.
- [X] T037 [P] [US2] Unit test `tests/unit/services/test_persona_classifier.py::test_forced_full_does_not_affect_other_personas` — same scenario, assert `tech_writer`/`qa` slices are unmodified (FR-005 surgical override).
- [X] T038 [P] [US2] Integration test `tests/integration/test_persona_scope_e2e.py::test_security_sensitive_card_forces_full` — walk a small-diff security-sensitive card through the graph; assert `session.persona_scope.personas['security'].depth == 'full'` and the structured log is emitted.

### Implementation for User Story 2

- [X] T039 [US2] Implement post-processing step 3 from `contracts/classifier-prompt.md` in `src/coordinare/services/persona_classifier.py`: for each persona in `forced_full_on_path_classes`, check if any file's classes intersect the configured list; if yes, override depth to `full` and append `forced_full_on_path_class:<class>` to that persona's `overrides`. Emit `persona_scope.classifier.forced_full` info log with `card_id, persona, path_class, matching_files` per the logging table.

**Checkpoint**: US2 deliverable. Risk-class paths are non-negotiable `full` regardless of LLM output.

---

## Phase 5: User Story 3 — Classifier failure falls back to today's behavior (P1)

**Goal**: Network errors, timeouts, malformed JSON, schema violations, or unknown depth values all collapse to `depth: full` for every persona; warning is logged and (rate-limited) Slack-posted; previous-cycle scope is reused when available.

**Independent Test**: Mock a backend timeout — assert every persona slice ends up `depth: full`, a `persona_scope.classifier.failed` warning is emitted, and the card still reaches `monitoring_pr` within SC-003's 30s budget.

### Tests for User Story 3

- [X] T040 [P] [US3] Unit test `tests/unit/services/test_persona_classifier.py::test_timeout_falls_back_to_full` — mock backend to raise `asyncio.TimeoutError`; assert returned `PersonaScope` has every persona at `depth: full` with `overrides: ["classifier_failed:timeout"]` (or similar structured reason).
- [X] T041 [P] [US3] Unit test `tests/unit/services/test_persona_classifier.py::test_malformed_json_falls_back_to_full` — mock backend to return non-JSON; assert full-everywhere fallback + warning emitted.
- [X] T042 [P] [US3] Unit test `tests/unit/services/test_persona_classifier.py::test_unknown_depth_value_falls_back_to_full` — mock backend to return `depth: "deep"`; assert fallback engages (per R6 / `contracts/classifier-prompt.md` post-processing step 2).
- [X] T043 [P] [US3] Unit test `tests/unit/services/test_persona_classifier.py::test_previous_cycle_scope_reused_on_transient_failure` — session has a prior-cycle `PersonaScope`; this cycle's classify fails; assert the prior scope is reused with a `persona_scope.classifier.reusing_previous_cycle` debug log.
- [X] T044 [P] [US3] Unit test `tests/unit/services/test_persona_classifier.py::test_warning_rate_limited_via_cooldown` — two consecutive failures within `classifier_failure_warning_cooldown_seconds` produce only one warning; the second is suppressed (FR-006).
- [X] T045 [P] [US3] Integration test `tests/integration/test_persona_scope_e2e.py::test_classifier_failure_does_not_block_card` — mock the backend to fail; assert the card still reaches `monitoring_pr` within 30s and `depth: full` was applied to every persona.

### Implementation for User Story 3

- [X] T046 [US3] Implement the failure-fallback path in `src/coordinare/services/persona_classifier.py`: catch `asyncio.TimeoutError`, JSON parse errors, schema validation errors, unknown depth values; if a prior `session.persona_scope` exists, return it (with `reusing_previous_cycle` debug log); otherwise return a `PersonaScope` with every persona at `depth: full, focus: "(classifier unavailable — full depth applied)", overrides: ["classifier_failed:<reason>"]`.
- [X] T047 [US3] Wire warning rate-limiting in `src/coordinare/services/persona_classifier.py` via the existing `NotificationService` cooldown mechanism (same channel used by 069's blocked-notification rehydration). Key the cooldown by project + reason so different failure modes don't suppress each other indefinitely.
- [X] T048 [US3] Add the `persona_scope.classifier.failed` warning log with `card_id, cycle_index, reason, fallback_to_full` per the contract's logging table.

**Checkpoint**: US3 deliverable. Pipeline never stalls on classifier failures.

---

## Phase 6: User Story 4 — Re-assessment each cycle keeps scope current (P2)

**Goal**: Every cycle recomputes `PersonaScope`; a card whose diff grows mid-flight gets re-scoped on the next cycle.

**Independent Test**: Two-cycle test where cycle 1 has a 12-line diff (mock classifier → all `skim`) and cycle 2 has a 240-line diff (mock classifier → all `full`); assert cycle 2 dispatches at `full`.

### Tests for User Story 4

- [X] T049 [P] [US4] Integration test `tests/integration/test_persona_scope_e2e.py::test_recompute_on_diff_growth` — run two cycles with different mocked PR file lists; assert `session.persona_scope.cycle_index` advances and depths reflect the new diff.
- [X] T050 [P] [US4] Unit test `tests/unit/graph/nodes/test_classify_scope.py::test_node_runs_every_cycle_when_enabled` — invoke the node twice with the same session and assert classify is called both times (FR-003).

### Implementation for User Story 4

- [X] T051 [US4] Ensure `classify_scope_node` is invoked unconditionally on each cycle (no caching by head_sha or file list); confirm by inspection that no early-return short-circuits exist when `enabled=True` and a PR is present.
- [X] T052 [US4] In `src/coordinare/services/persona_classifier.py`, stamp `PersonaScope.computed_at` (ISO-8601 UTC), `cycle_index` (from `session.cycle_index`), and `head_sha` (from current PR HEAD) on every produced scope so downstream consumers can detect drift.

**Checkpoint**: US4 deliverable. Per-cycle freshness verified; transient failures still reuse prior scope per US3.

---

## Phase 7: User Story 5 — Project author configures path classes and per-persona scope behavior (P2)

**Goal**: A fresh project on a different stack adopts the feature with config only, no coordinare source changes. Per-persona opt-out works by omitting `scope_behavior`.

**Independent Test**: Run the quickstart Stage 1 / Stage 2 / Stage 3 configs end-to-end against a synthetic project; assert behavior matches the documented expectations.

### Tests for User Story 5

- [X] T053 [P] [US5] Unit test `tests/unit/test_config.py::test_closer_scope_behavior_warns_and_is_ignored` — config sets `closer.scope_behavior`; assert a startup warning is emitted and the dispatched closer ignores the configured behavior (FR-009).
- [X] T054 [P] [US5] Unit test `tests/unit/graph/nodes/test_dispatch_performer_scope.py::test_persona_without_scope_behavior_runs_as_today` — persona has no `scope_behavior` block; assert PersonaScope is ignored for that persona and the performer runs with today's defaults (FR-010 additive default).
- [X] T055 [P] [US5] Unit test `tests/unit/graph/nodes/test_dispatch_performer_scope.py::test_missing_tier_falls_back_to_today` — persona has `scope_behavior` but only `full` is defined; classifier emits `skim`; assert today's behavior is used (no `max_tool_calls` cap, no `prompt_addon`) and a debug log is emitted (per `contracts/config-schema.md` validation rules row 6).
- [X] T056 [P] [US5] Static check `tests/unit/test_no_hardcoded_path_globs.py::test_coordinare_ships_no_default_globs` — grep `src/coordinare/` for any literal glob patterns like `*.md`, `src/auth/**` outside of test fixtures; fail if found (SC-005 / FR-004).
- [X] T057 [P] [US5] Integration test `tests/integration/test_persona_scope_e2e.py::test_shadow_mode_classifier_runs_but_no_persona_changes` — enabled + path_classes set, no `scope_behavior` on any persona; assert classifier runs (and rollup posts) but every persona dispatches with today's defaults.

### Implementation for User Story 5

- [X] T058 [US5] Implement the startup-warning emitter for the four scenarios in `contracts/config-schema.md`: enabled+empty-path_classes warning, scope_behavior-with-feature-off info, closer.scope_behavior warning. Put these in a `validate_persona_scope_config()` helper called at coordinare startup (likely from the existing config-load pipeline).
- [X] T059 [US5] Implement the per-tier-missing fallback in `src/coordinare/graph/nodes/dispatch_performer.py`: when the slice's depth has no matching tier in the persona's `scope_behavior`, skip the override and emit a `persona_scope.dispatch.tier_missing` debug log.

**Checkpoint**: US5 deliverable. A new project can adopt via YAML alone.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T060 [P] Ruff + type-check sweep on touched files: `.venv/bin/ruff check src/coordinare/config.py src/coordinare/session.py src/coordinare/state_store.py src/coordinare/services/persona_classifier.py src/coordinare/graph/nodes/classify_scope.py src/coordinare/graph/nodes/dispatch_performer.py src/coordinare/graph/nodes/notify.py src/coordinare/graph/routing.py src/coordinare/graph/builder.py tests/`.
- [X] T061 [P] Performance check for SC-006: time the classifier path locally across ≥20 synthetic cards spanning the path-class spectrum; record p50/p95; assert ≤2 s p50 / ≤10 s p95. Add a small benchmark script under `scripts/bench_persona_classifier.py` (not a test — runtime tool).
- [X] T062 [P] Run `specs/074-persona-scope-tiering/quickstart.md` Stage 1 / Stage 2 / Stage 3 manually against a synthetic project config; confirm each stage's documented behavior matches.
- [X] T063 Update `CLAUDE.md` "Active Technologies" / "Recent Changes" footer with the 074 entry once the feature lands (per the existing pattern set by 064/066/067/068/069/073).
- [X] T064 Run full unit + integration suite: `.venv/bin/pytest tests/unit tests/integration tests/contract -x`. Fix any incidental breakage on already-shipped specs (031, 064, 069, 073 round-trip tests are most exposed to changes in `_SESSION_FIELDS`).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No deps.
- **Phase 2 (Foundational)**: Blocks all user stories. T002 → T003 → T004 → T005; T006 → T007 → T008; T009 → T010/T011; T012 → T013 → T014; T015 → T016. T017/T018 depend on T004/T005.
- **Phase 3 (US1)**: Depends on Phase 2 complete. T023→T024→T025→T026→T027→T028→T029 sequential within US1; tests T019–T022 and T030–T035 parallel.
- **Phase 4 (US2)**: Depends on Phase 3's classifier pipeline (T024). T036–T038 tests + T039 impl.
- **Phase 5 (US3)**: Depends on Phase 3 (classifier pipeline). T040–T045 tests + T046–T048 impl.
- **Phase 6 (US4)**: Depends on Phase 3 (classify_scope node). T049–T050 tests + T051–T052 impl.
- **Phase 7 (US5)**: Depends on Phase 2 (config) + Phase 3 (dispatch_performer wiring). Tests T053–T057 + impl T058–T059.
- **Phase 8 (Polish)**: After all user stories.

### User Story Dependencies

- US1, US2, US3 are all P1 and independently testable once Phase 2 + Phase 3's core classifier service exist.
- US4 is a thin layer over US1 (per-cycle invocation); can start as soon as US1's `classify_scope_node` lands.
- US5 cross-cuts Phase 2 (config validation) and US1 (dispatch wiring); test-heavy.

### Parallel Opportunities

- T002, T003, T006, T013, T014 can run in parallel within Phase 2 (different files / pure functions).
- T019–T022 (US1 tests), T030–T035 (US1 unit tests) — all `[P]`, all in different test files.
- T036–T038 (US2 tests), T040–T045 (US3 tests), T049–T050 (US4 tests), T053–T057 (US5 tests) — all parallelizable within their phase.
- T060–T062 (polish) — all parallelizable.

---

## Parallel Example: Phase 2 Foundational

```bash
# Launch in parallel (different files, no inter-dep):
Task: "T002 — Add ScopeTierBehavior + ScopeBehavior pydantic models in src/coordinare/config.py"
Task: "T006 — Add PersonaScope + PersonaScopeSlice TypedDicts in src/coordinare/session.py"
Task: "T013 — Add _match_path_classes helper in src/coordinare/services/persona_classifier.py"
```

## Parallel Example: User Story 1 Tests

```bash
Task: "T019 — Contract test test_input_schema_shape in tests/contract/test_persona_classifier_io.py"
Task: "T020 — Contract test test_output_schema_validates in tests/contract/test_persona_classifier_io.py"
Task: "T021 — Contract test test_classifier_never_sees_raw_diff in tests/contract/test_persona_classifier_io.py"
Task: "T022 — Integration test test_docs_only_card_skips_security_qa in tests/integration/test_persona_scope_e2e.py"
```

---

## Implementation Strategy

### MVP (US1 only)

1. Phase 1 (T001).
2. Phase 2 (T002–T018) — foundational config + persistence + classifier skeleton.
3. Phase 3 (T019–T035) — full US1 docs-only path including PR rollup.
4. **STOP and VALIDATE**: run a docs-only card through a live coordinare instance; confirm SC-001 (≥40% wall-time reduction) on the live test queue.
5. Deploy/demo.

### Incremental Delivery

- MVP = US1 (skim/skip path delivers the visible throughput win).
- Add US2 next — small surgical addition (single post-processing step). Closes the safety gap before any production-like deployment.
- Add US3 — the operational safety net. Required before broad rollout.
- US4 + US5 round out the feature; both are P2 and can ship together.

### Suggested Cut Lines

If time-pressed:
- US1 + US3 = safe MVP (delivers the win, has the failure safety net).
- Add US2 before any rollout that includes security-sensitive repos.
- US4 + US5 can ship in a follow-up commit on the same branch.

---

## Notes

- Tests are listed BEFORE their corresponding implementation tasks (TDD-friendly) but the project doesn't strictly require red-green; the order does help catch regressions on the `_SESSION_FIELDS` round-trip (FR-011 — the recurring 073-era pattern).
- The `_SESSION_FIELDS` regression test (T008) is the single most load-bearing test in Phase 2 — do not skip.
- Avoid mutating persona base prompts under any circumstance (FR-007); the `prompt_addon` is a structured field, not a string-concat.
- Closer scope-invariance (FR-009) is absolute — no config override.
- All PR/state inspection must go through existing services (`services/github.py`, `services/state_store.py`); do not duplicate HTTP/JSON logic.
