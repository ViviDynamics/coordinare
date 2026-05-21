# Tasks: Compatibility-First Performer Backend

**Input**: Design documents from `/specs/067-compatibility-first-backend/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included. Spec FR-006/FR-008 mandate integration + contract tests; constitution Principle II requires unit coverage; NFR-001 requires a perf benchmark.

**Organization**: Tasks are grouped by user story (US1, US2, US3 from spec.md) so each story can be implemented and shipped independently. US1 is the MVP — completing US1 alone delivers the motivating value (a self-hosted card moving to `IN_REVIEW`).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no dependency on incomplete tasks)
- **[Story]**: US1 / US2 / US3 — Setup, Foundational, and Polish phases have no story label

## Path Conventions

Single project. Performer code under `agent/performer/src/performer/`, coordinare under `src/coordinare/`, tests under `tests/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Repo scaffolding for the new adapter + test fixtures shared across user stories.

- [X] T001 Add `"opencode_compat"` entry to `supported_backends` in `agent/performer/src/performer/backends/__init__.py` mapping to `("performer.backends.opencode_compat", "OpenCodeCompatAdapter")` — leave the module empty for now (T010 fills it). Verify `get_backend("opencode_compat")` raises `UnsupportedBackendError` until then.
- [X] T002 [P] Create empty module `agent/performer/src/performer/backends/opencode_compat.py` with module docstring describing the LCD profile, citing `specs/067-compatibility-first-backend/research.md` R1 and R2.
- [X] T003 [P] Add an `lmstudio` pytest marker to `pyproject.toml` (or `pytest.ini`) gated on `LMSTUDIO_AVAILABLE` env var; document the marker in `tests/README.md` if one exists, or add a one-line note in `AGENTS.md` under the testing section.
- [X] T004 [P] Add a `tests/fixtures/lcd_payloads/` directory with two reference JSON fixtures: `valid_lcd_request.json` (all whitelist fields populated) and `forbidden_fields_request.json` (one entry per denylist row in `data-model.md` §1).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The wire-format contract and the coordinare-side error surfacing layer — both user stories US1 and US3 depend on these.

**⚠️ CRITICAL**: All user-story work blocks on this phase.

- [X] T005 Implement `UpstreamHTTPError` pydantic model in `src/coordinare/protocol.py` (or a new `src/coordinare/upstream_errors.py` if `protocol.py` is coordinare↔performer protocol only — choose the location that already exposes pydantic models to both sides). Schema per `contracts/upstream_http_error.md`. Include the `BODY_CAP_BYTES = 2048` and `TRUNCATION_SUFFIX` constants.
- [X] T006 [P] Extend `PerformerResponse.metrics` in `agent/performer/src/performer/protocol.py` to accept an optional `upstream_http_error` field typed as the new model. Confirm pydantic `extra="ignore"` is in effect so older coordinares drop it cleanly (back-compat per `contracts/upstream_http_error.md`).
- [X] T007 [P] In `src/coordinare/graph/nodes/handle_system_error.py`, add module-level constants `TRANSIENT_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})` and `TRANSIENT_BODY_MARKERS: tuple[str, ...] = ("temporarily unavailable", "rate limit")`. Add a `classify_upstream(error: UpstreamHTTPError) -> Literal["transient", "permanent"]` helper that uses these — single source of truth per FR-005.
- [X] T008 In `src/coordinare/services/http_performer_service.py`, when the performer response carries `metrics.upstream_http_error`, log a single structlog line containing all envelope fields (kv order per `data-model.md` §2) at WARN if transient, ERROR if permanent. No friendly-rephrasing — body logged verbatim. Strip userinfo and `api_key` query params from `base_url` before logging.
- [X] T009 [P] Contract test `tests/contract/test_067_upstream_http_error_schema.py` covering all four obligations in `contracts/upstream_http_error.md` (round-trip, truncation boundary, base_url credential stripping, `kind` discriminator).
- [X] T009.5 [P] Verify dispatch payload is unchanged: grep `src/coordinare/services/agent_service.py` and `agent/performer/src/performer/protocol.py` to confirm no new fields cross coordinare → performer; assert `specs/contracts/dispatch-payload.md` Field Registry is unmodified by 067. (Per `plan.md` "Dispatch payload" note — `compat_remap_developer_role` is performer-local.)
- [ ] T009b Capture pre-067 codex baseline: run `quickstart.md` Steps 4–5 with `backend: codex` against OpenAI; save the outbound request payload to `tests/fixtures/067_codex_baseline_request.json` and a log excerpt to `specs/067-compatibility-first-backend/baseline-codex.log`. These are the references for the SC-003 swap test and the US2 byte-identity regression test. **MUST land before any T010+ code merges** so the fixture captures pre-067 state.

**Checkpoint**: Wire contract pinned. Codex baseline captured. User-story implementation can begin.

---

## Phase 3: User Story 1 — Operator points daemon at a self-hosted model and it works (Priority: P1) 🎯 MVP

**Goal**: Land a card from `TODO` to `IN_REVIEW` on LM Studio via the new `opencode_compat` backend without source patches.

**Independent Test**: Run `pytest -m lmstudio tests/integration/test_067_env_bootstrap_lmstudio.py` with LM Studio serving `qwen3-coder-30b` at `n_ctx >= 32768`. Card reaches `IN_REVIEW`. LM Studio console shows zero `unsupported tool type`, zero `developer role rewritten`, zero `prompt_cache_key ignored` warnings.

### Implementation

- [X] T010 [US1] Implement `OpenCodeCompatAdapter` in `agent/performer/src/performer/backends/opencode_compat.py`. Mirror the lifecycle methods of `OpenCodeAdapter` (`start`, `get_status`, `drain_events`, `relay_feedback`, `stop`) but **do not subclass** — copy the surface and adjust ([[feedback_junie_own_harness]], [[feedback_interface_first_design]]). Launch opencode CLI with flags that disable hosted-tool descriptors. Wire `base_url` through unchanged.
- [X] T011 [US1] Add private helpers in `opencode_compat.py`:
  - `_assert_lcd_payload(body: dict) -> None` enforcing the whitelist/denylist from `data-model.md` §1; raises `LcdPayloadError` (new exception class, defined locally) on violation.
  - `_remap_developer_role(messages: list[dict]) -> list[dict]` per R4 — converts `role: developer` to `role: system` and emits a one-time WARN log via structlog (deduped per session via a `_warned_developer` instance flag). Gated on `compat_remap_developer_role` config flag (default true).
- [X] T012 [US1] Implement `_redact_request_body(body: dict) -> dict` in `opencode_compat.py` per R5: key-denylist pass (`api_key`, `authorization`, `OPENAI_API_KEY`, `token`, `secret`, `password`, `bearer`) replacing values with `"***REDACTED***"`; pattern pass over string values for `sk-...`, `gho_...`, `ghp_...`, `OPENAI_API_KEY=...`, `Bearer ...`. Call this from the debug-log emit path; do **not** mutate the outbound body.
- [X] T013 [US1] Wire the adapter into the outbound request path so `_assert_lcd_payload` runs immediately before `httpx.AsyncClient.post(...)`; ensure `_remap_developer_role` runs before validation (so a remapped payload passes); ensure structlog debug emission uses the redacted copy.
- [X] T014 [US1] On non-2xx upstream response in `opencode_compat.py`, construct an `UpstreamHTTPError` (truncating body to `BODY_CAP_BYTES - len(TRUNCATION_SUFFIX)` when oversize, setting `body_truncated=True`), attach it to the outgoing `PerformerResponse.metrics.upstream_http_error`, and return — do **not** re-raise as a generic exception. Mirror `x-request-id` / `openai-request-id` into `upstream_request_id` when present.
- [X] T015 [P] [US1] Update `config.example.yaml` with a worked `opencode_compat` example block per `data-model.md` §3 (LM Studio defaults; comment referencing `specs/067-compatibility-first-backend/quickstart.md`). Include the `compat_remap_developer_role: true` flag with an inline pointer to research.md R4.
- [X] T016 [P] [US1] Audit `packages/service_inference/src/coordinare_service_inference/prompt.py` per R3. Remove or qualify any mention of `web_search` that implies hosted execution. If a function-call entry exists, leave it; if a `type: web_search` descriptor is referenced, delete the reference and add a one-line code comment naming spec 067.
- [X] T017 [P] [US1] Author `docs/quickstart-selfhosted.md` mirroring `specs/067-compatibility-first-backend/quickstart.md` for operator distribution (the in-spec quickstart stays authoritative; the docs copy is the public-facing one). Cross-link.

### Tests

- [X] T018 [P] [US1] Unit test `tests/unit/test_067_compat_request_shape.py` — table-driven assertions that `_assert_lcd_payload` accepts every fixture in `tests/fixtures/lcd_payloads/valid_lcd_request.json` and rejects every entry in `forbidden_fields_request.json` with a clear error message naming the offending field.
- [X] T019 [P] [US1] Unit test `tests/unit/test_067_developer_role_remap.py` — covers: (a) `developer` → `system` when flag is true (default); (b) untouched when flag is false; (c) WARN is emitted exactly once per adapter instance regardless of how many remapped messages are seen.
- [X] T020 [P] [US1] Unit test `tests/unit/test_067_request_body_redaction.py` — covers: (a) each denylisted key is redacted; (b) each pattern (`sk-...`, `gho_...`, `Bearer ...`) is redacted inside `messages[].content`; (c) the outbound body is **not** mutated (only the log copy is).
- [X] T021 [US1] Integration test `tests/integration/test_067_env_bootstrap_lmstudio.py` gated by `@pytest.mark.lmstudio`. Stands up an LM Studio session (or skips if `LMSTUDIO_AVAILABLE != "1"`), dispatches a representative website card, asserts: env_bootstrap completes, services manifest emits, card reaches `IN_REVIEW`. Inspects LM Studio console output (when surface-able) for the three forbidden warning patterns.

**Checkpoint**: MVP shipped — US1 alone validates the 065 self-hosted deliverable.

---

## Phase 4: User Story 2 — Existing codex+OpenAI deployment keeps working (Priority: P1)

**Goal**: Confirm zero behavioural change for `backend: codex`.

**Independent Test**: Existing `tests/integration/test_065_*.py` pass unchanged. A pre-067 captured request payload is byte-identical to one produced after 067.

- [X] T022 [US2] Add regression test `tests/integration/test_067_codex_unchanged.py` that replays a representative codex outbound request payload (live with `OPENAI_API_KEY`-gated marker, or replay) and asserts byte-for-byte equality against the fixture captured in T009b at `tests/fixtures/067_codex_baseline_request.json`. (Fixture capture itself is **T009b in Phase 2** — must precede any T010+ merge.)
- [X] T023 [P] [US2] Swap test `tests/integration/test_067_swap_test.py` (SC-003): start from `backend: codex` config, run a minimal dispatch dry-run; flip to `backend: opencode_compat` against the same OpenAI endpoint via a temp config override; confirm both succeed with no code or other-config changes.

---

## Phase 5: User Story 3 — Operator diagnoses a local-server failure from logs alone (Priority: P2)

**Goal**: When upstream returns non-2xx, the daemon log line contains the verbatim upstream status + body within one WARN/ERROR line.

**Independent Test**: Configure a deliberately-broken backend (wrong base URL or expired key). Inspect daemon log. Within one log line at WARN/ERROR, find `status: <code>` and the verbatim `upstream_body`.

- [X] T024 [US3] Unit test `tests/unit/test_067_upstream_error_classification.py` — table-driven coverage of `classify_upstream` from T007: every status in `TRANSIENT_STATUSES` → "transient"; representative permanent statuses (400, 401, 403, 404) → "permanent"; 200-with-marker → "transient" via the marker fallback; 200-without-marker → not classified as transient.
- [X] T025 [P] [US3] Integration test `tests/integration/test_067_transparent_errors.py` — points `opencode_compat` at a `pytest-httpserver`-style local stub returning 404/`model not found`, 400/`context length exceeded`, 503/`unavailable` in turn. Asserts each response yields the corresponding log line with the verbatim upstream body and that 503 is classified transient while 400/404 are permanent.
- [X] T026 [US3] Verify `src/coordinare/graph/nodes/handle_system_error.py` no longer falls back to substring matching against friendly-wrapped strings anywhere outside `TRANSIENT_BODY_MARKERS`. Grep for `"high demand"`, `"please try again"`, and similar — remove or migrate to the marker tuple. Add a code comment at the marker tuple stating it is the single source of truth (per FR-005).

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T027 [P] Performance benchmark `tests/integration/test_067_perf_compat_vs_codex.py` gated by `OPENAI_API_KEY` presence. Measures first-token latency for an identical prompt on `codex` vs `opencode_compat` against `https://api.openai.com/v1`. Asserts compat is within 10% of codex per NFR-001. Reports both numbers in the test output for the PR description.
- [X] T028 [P] Update `AGENTS.md` if it has a "Backends" section: add a one-paragraph entry for `opencode_compat` pointing at `specs/067-compatibility-first-backend/` and `docs/quickstart-selfhosted.md`. If no such section exists, skip — do not add new top-level sections.
- [X] T029 [P] Run `.venv/bin/ruff check agent/performer/src/performer/backends/opencode_compat.py src/coordinare/services/http_performer_service.py src/coordinare/graph/nodes/handle_system_error.py` and fix all findings. Run `.venv/bin/pytest tests/unit/test_067_*.py tests/contract/test_067_*.py` and ensure all pass; gate-marked integration tests skipped acceptably.
- [X] T030 Final audit: grep the repo for any remaining `type: web_search` / `type: file_search` / `role: developer` / `prompt_cache_key` references in prompts or fixtures used by `opencode_compat`; remove or qualify each. Update `specs/067-compatibility-first-backend/spec.md` Open Questions section to mark each question resolved with a pointer to `research.md`.
- [X] T031 [P] [US3] Hand-run `quickstart.md` (including the Swap test, SC-003) against vLLM, Ollama, and LiteLLM proxy; record results in `specs/067-compatibility-first-backend/swap-test-results.md` (one section per endpoint with model id, `n_ctx`, and pass/fail per quickstart step). Satisfies FR-006's non-CI portability claim and provides the US3 end-to-end verification artifact.

---

## Dependencies

```
Setup (T001–T004)
    ↓
Foundational (T005–T009, T009.5, T009b)   ← BLOCKS all user-story phases
    ↓                                       (T009b captures codex baseline before T010+)
┌───────────────────────────────────────────────┐
│ US1 (T010–T021)   ← MVP                       │
│ US2 (T022–T023)   ← independent of US1 once   │
│                     T005–T008, T009b done     │
│ US3 (T024–T026)   ← uses T007/T008 only       │
└───────────────────────────────────────────────┘
    ↓
Polish (T027–T031)
```

- T010–T014 are sequential (same file: `opencode_compat.py`).
- T015, T016, T017 touch different files → parallel within US1.
- T018, T019, T020 are independent test files → parallel.
- T009b (codex baseline capture) must run **before** T010 merges. Promoted into Phase 2 (Foundational) so the dependency is explicit; T022 then only consumes the fixture.
- US2 and US3 can be developed in parallel with US1 once Phase 2 is complete.

## Parallel Execution Examples

**Setup phase (after T001)**:
```
T002, T003, T004 — three independent file/config edits.
```

**Foundational phase**:
```
T006, T007, T009 in parallel after T005 lands (T005 introduces the type the others import).
T008 sequential after T005.
```

**US1 (after T010)**:
```
T011, T012 sequential (same file).
T013, T014 sequential (same file, builds on T011/T012).
T015, T016, T017 in parallel (different files).
T018, T019, T020 in parallel (different test files).
T021 last (integration; needs T010–T014 wired).
```

**Polish**:
```
T027, T028, T029, T031 in parallel; T030 last.
```

## Implementation Strategy

1. **MVP = US1 only**. Stop after T021 if time-constrained; the 065 self-hosted deliverable is unblocked at that point.
2. **US2 (T022–T023)** is cheap insurance and should ship in the same PR as US1 — captures the pre-067 codex baseline before any code drift can invalidate it.
3. **US3 (T024–T026)** is independently shippable. It can land in a follow-up PR if US1 is urgent, **but** T026 (the substring-matching purge) should ride with US1 since the new transparent-error path makes the legacy fallbacks obsolete.
4. **Polish phase** is a single closing commit; do not let it gate user-story merges.

---

## Format validation

All 33 tasks above (T001–T031, plus inserted T009.5 and T009b in Phase 2) follow `- [ ] TNNN [P?] [USx?] Description with file path`. Setup, Foundational, and Polish phases carry no `[USx]` label per the rules — except T031, which sits in Polish but ships as the US3 end-to-end verification artifact and so carries `[US3]` deliberately.
