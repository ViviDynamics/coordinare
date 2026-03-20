# Tasks: Performer Personas

**Input**: Design documents from `/specs/018-performer-personas/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, contracts/personas-api.yaml ✓, quickstart.md ✓

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to
- All paths are relative to repository root

---

## Phase 1: Setup

**Purpose**: No new packages or project init required — this feature adds to an existing project. Setup confirms the working baseline.

- [X] T001 Verify test suite passes on clean checkout: run `.venv/bin/pytest tests/unit/ -q` and confirm zero failures

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Config model extension and PersonaService — required by all three user stories.

- [X] T002 Add `PersonaConfig` and `PersonasConfig` Pydantic models to `src/coordinare/config.py` beneath existing nested models (after `AdvocateConfig`); add `personas: PersonasConfig = PersonasConfig()` field to `ProjectConfiguration`
- [X] T003 Create `src/coordinare/services/persona_service.py` with: `PERSONA_MAX_LENGTH = 8_000`, `DEFAULT_INSTRUCTIONS: dict[str, str]` (one entry per role: advocate, assessor, architect, implementer, reviewer, security, qa, tech_writer), `VALID_ROLES: frozenset`, `get_effective_instructions(role, personas) -> str` (falls back to default when empty/whitespace), `save_persona(role, instructions, config_path) -> None` (read-YAML → update → write-YAML; raises `ValueError` if role unknown or instructions > 8000 chars), `reset_persona(role, config_path) -> None` (delegates to `save_persona(role, "", ...)`)
- [X] T004 Write unit tests for `PersonaService` in `tests/unit/test_persona_service.py`: test `get_effective_instructions` returns custom when set; returns default when empty string; returns default when whitespace-only; raises `ValueError` for unknown role; **call `get_effective_instructions` for all 8 roles (including unimplemented: architect, reviewer, security, qa, tech_writer) and assert each returns a non-empty string** (FR-001 coverage); test `save_persona` raises `ValueError` when instructions > 8000 chars; test round-trip save/read via temp YAML file
- [X] T005 Write unit tests for the new config models in `tests/unit/test_config.py` (add to existing file): test `PersonasConfig` parses all 8 roles from YAML; test missing `personas:` key produces all-default `PersonasConfig`; test empty `instructions` field parsed correctly; **test role isolation: set `personas.implementer.instructions = "custom"`, assert `personas.assessor.instructions` remains `""` (SC-004)**

**Checkpoint**: `PersonaService` and config models complete. Run `pytest tests/unit/test_persona_service.py tests/unit/test_config.py -q` — all green.

---

## Phase 3: User Story 1 — Implementer Persona via Config File (Priority: P1) 🎯 MVP

**Goal**: Implementer persona instructions are injected into every dispatch payload. Config change takes effect on next dispatch without restart.

**Independent Test**: Set `personas.implementer.instructions` in `config.yaml`; dispatch a card; assert the dispatch payload received by the performer contains the configured instructions under `persona_instructions`.

### Implementation for User Story 1

- [X] T006 [US1] Modify `src/coordinare/graph/nodes/dispatch_card.py`: import `get_effective_instructions` from `persona_service`; before calling `agent.dispatch_card()`, call `get_effective_instructions("implementer", state["config"].personas)` and add the result to `card_context` as `card_context["persona_instructions"] = instructions`
- [X] T007 [US1] Modify `src/coordinare/services/agent_service.py` in `dispatch_card()`: after building `payload`, add `if card_context.get("persona_instructions"): payload["persona_instructions"] = card_context["persona_instructions"]`
- [X] T008 [US1] Write unit tests for implementer persona injection in `tests/unit/graph/nodes/test_dispatch_card.py` (add to existing file): test `persona_instructions` present in payload when `personas.implementer.instructions` is set; test `persona_instructions` equals the default text (not empty) when config field is empty string — FR-007 guarantees defaults always apply; test `persona_instructions` equals the default text when config field is whitespace-only

**Checkpoint**: US1 complete. Quickstart Scenario 1 passes end-to-end.

---

## Phase 4: User Story 2 — Assessor Persona via Config File (Priority: P2)

**Goal**: Assessor persona instructions are prepended to the assessment prompt on every `assess_card` invocation.

**Independent Test**: Set `personas.assessor.instructions` in config; trigger `assess_card`; assert the prompt passed to the assessment backend starts with `## Assessor Instructions\n{instructions}`.

### Implementation for User Story 2

- [X] T009 [US2] Modify `src/coordinare/graph/nodes/assess_card.py`: import `get_effective_instructions`; before calling `backend.assess(details)`, add `details["persona_instructions"] = get_effective_instructions("assessor", state["config"].personas)`
- [X] T010 [US2] Modify `src/coordinare/services/assessment.py` in `_build_assess_prompt()`: if `card.get("persona_instructions")` is non-empty, prepend `f"## Assessor Instructions\n{card['persona_instructions']}\n\n"` to the prompt string
- [X] T011 [US2] Write unit tests for assessor persona injection in `tests/unit/graph/nodes/test_assess_card.py` (add to existing file): test `persona_instructions` present in `details` dict when assessor persona is configured; test default instructions used when no custom assessor persona set; test prompt starts with `## Assessor Instructions` when instructions are non-empty; test prompt unchanged when `persona_instructions` is absent from card dict

**Checkpoint**: US2 complete. Quickstart Scenario 3 passes.

---

## Phase 5: User Story 3 — View and Edit Personas via Web Dashboard (Priority: P3)

**Goal**: Dashboard exposes three REST endpoints (GET/PUT/DELETE `/api/personas/{role}`) and a Personas section in the UI. Changes take effect without restart.

**Independent Test**: Load dashboard personas page; read current implementer instructions; submit an edit; verify next dispatch payload reflects updated instructions.

### Implementation for User Story 3

- [X] T012 [US3] Modify `src/coordinare/dashboard.py`: add `GET /api/personas` endpoint — reads `config.personas` via `get_effective_instructions()` for all 8 roles, returns `list[PersonaResponse]` (role, instructions, is_default)
- [X] T013 [US3] Modify `src/coordinare/dashboard.py`: add `PUT /api/personas/{role}` endpoint — validates role name (404 if invalid), validates instructions length (400 if > 8000 chars — API-layer rejection; distinct from config-load ValidationError in T020), calls `save_persona(role, instructions, config_path)`, returns updated `PersonaResponse`; handle `OSError` from file write (return HTTP 500 with `{"error": "..."}` body)
- [X] T014 [US3] Modify `src/coordinare/dashboard.py`: add `DELETE /api/personas/{role}` endpoint — validates role name (404 if invalid), calls `reset_persona(role, config_path)`, returns HTTP 204 No Content
- [X] T015 [US3] Add Personas section to the dashboard HTML template inside `dashboard.py`: collapsible section listing all 8 roles; each row shows role name, current instructions (or "Using default instructions" if `is_default=True`), inline textarea for editing, Save button (calls PUT), Reset button (calls DELETE); buttons show loading state during request; show success/error feedback after save
- [X] T016 [US3] Write unit tests for the three personas API endpoints in `tests/unit/test_dashboard.py` (add to existing file): test GET `/api/personas` returns list of 8 roles with correct `is_default` flags; test PUT `/api/personas/implementer` with valid instructions returns 200 with updated data; test PUT with instructions > 8000 chars returns 400; test PUT with unknown role returns 404; test DELETE `/api/personas/assessor` returns 204; test DELETE with unknown role returns 404; **test PUT when `save_persona` raises `OSError` returns HTTP 500 with `{"error": "..."}` body** (covers dashboard-save-failure edge case — D2)
- [X] T016b [US3] Assert dashboard API performance in `tests/unit/test_dashboard.py`: time the PUT `/api/personas/implementer` call (mocked file I/O) and assert response time < 2 seconds — satisfies SC-003 (D1)

**Checkpoint**: US3 complete. Quickstart Scenarios 4, 5, and 6 pass. SC-003 verified.

---

## Phase 6: User Story 4 — Advocate Persona via Config File (Priority: P4)

**Goal**: Advocate persona instructions are passed into the advocate scan context on every `advocate_scan` invocation.

**Independent Test**: Set `personas.advocate.instructions`; trigger advocate scan; assert `AdvocateService.scan_and_respond()` is called with `persona_instructions` matching the configured value.

### Implementation for User Story 4

- [X] T017 [US4] Modify `src/coordinare/graph/nodes/advocate.py`: import `get_effective_instructions`; before calling `advocate_service.scan_and_respond(processed_ids)`, read `get_effective_instructions("advocate", state["config"].personas)` and pass it as `persona_instructions=instructions` keyword argument
- [X] T018 [US4] Modify `src/coordinare/services/advocate.py`: add `persona_instructions: str = ""` keyword parameter to `scan_and_respond()`; forward it to `ClaudeScorer` as additional system context when non-empty (e.g., prepend to the scoring prompt in `_build_score_prompt()` or equivalent method)
- [X] T019 [US4] Write unit tests for advocate persona injection in `tests/unit/graph/nodes/test_advocate.py` (add to existing file): test `scan_and_respond` called with `persona_instructions` matching configured advocate persona; test default instructions used when no advocate persona set

**Checkpoint**: US4 complete. Quickstart Scenario 1 (advocate variant) passes.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T020 [P] Verify length enforcement at config-load time (distinct from API-layer rejection in T013): add a `@field_validator("instructions")` to `PersonaConfig` in `src/coordinare/config.py` that raises `ValueError` when `len(instructions) > PERSONA_MAX_LENGTH`; add test in `tests/unit/test_config.py` asserting `ProjectConfiguration` raises `ValidationError` when `personas.implementer.instructions` exceeds 8000 chars in YAML — default instructions are NOT used as fallback here, the config is simply invalid
- [X] T021 [P] Verify hot-reload: set persona in config, dispatch, change config, dispatch again without restart — confirm second dispatch uses updated instructions (this is automatic given on-demand reads, but add an integration-style unit test confirming no caching)
- [X] T022 Run full test suite: `.venv/bin/pytest tests/unit/ -q` — confirm no regressions and coverage does not decrease
- [X] T023 Run linter: `.venv/bin/ruff check src/coordinare/services/persona_service.py src/coordinare/graph/nodes/dispatch_card.py src/coordinare/graph/nodes/assess_card.py src/coordinare/dashboard.py` — zero warnings
- [X] T024 Run Quickstart Scenario 6 (length limit): confirm API returns 400/422 for instructions > 8000 chars

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1** (Setup): Start immediately
- **Phase 2** (Foundational): Depends on Phase 1 — **BLOCKS all user stories**
- **Phase 3** (US1 — Implementer): Depends on Phase 2
- **Phase 4** (US2 — Assessor): Depends on Phase 2; independent of Phase 3
- **Phase 5** (US3 — Dashboard): Depends on Phase 2; independent of Phases 3 and 4
- **Phase 6** (US4 — Advocate): Depends on Phase 2; independent of Phases 3, 4, and 5
- **Phase 7** (Polish): Depends on all desired user stories complete

### Within Phase 2 (Foundational)

- T002 (config model) must complete before T003 (`PersonaService` imports `PersonasConfig`)
- T004 and T005 can run in parallel after T002 and T003

### Within Each User Story

- Implementation tasks (T006–T007, T009–T010, T012–T015, T017–T018) before their tests is acceptable; tests are written alongside implementation here
- T012, T013, T014 (three dashboard endpoints) modify the same file and MUST run sequentially to avoid merge conflicts — no `[P]` markers apply here

### Parallel Opportunities

- T004 and T005 (foundational tests) can run in parallel after T002+T003
- Phases 3, 4, 5, and 6 can run in parallel once Phase 2 is complete (different files throughout)
- T020, T021 in Phase 7 can run in parallel

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1 (T001)
2. Complete Phase 2 (T002–T005) — foundational
3. Complete Phase 3 (T006–T008) — implementer persona live end-to-end
4. **STOP and VALIDATE**: Run Quickstart Scenarios 1 and 2
5. Merge or continue to US2

### Incremental Delivery

1. Setup + Foundational → `PersonaService` available
2. US1 (implementer) → Implementer dispatches carry persona — **highest value**
3. US2 (assessor) → Assessor prompts carry persona
4. US3 (dashboard) → Non-technical users can manage personas via UI
5. US4 (advocate) → Advocate scans carry persona

### Total Task Count

- **Total tasks**: 25 (T001–T024 + T016b)
- **Phase 2 (Foundational)**: 4 tasks
- **US1 (P1)**: 3 tasks
- **US2 (P2)**: 3 tasks
- **US3 (P3)**: 6 tasks (T012–T016, T016b)
- **US4 (P4)**: 3 tasks
- **Polish**: 5 tasks (1 setup + 4 polish)
- **Parallel opportunities**: T004+T005, all of Phases 3–6 after Phase 2, T020+T021
- **Note**: T012/T013/T014 edit the same file (`dashboard.py`) — execute sequentially within Phase 5
