# Tasks: Anthropic→OpenAI Request-Translating Shim

**Input**: Design documents from `/specs/084-anthropic-openai-translate/`
**Prerequisites**: plan.md (required), spec.md (user stories), research.md, data-model.md, contracts/, quickstart.md

**Tests**: INCLUDED — Constitution II makes testing NON-NEGOTIABLE (TDD Red-Green-Refactor; coverage must not decrease). Test tasks are written first and MUST fail before implementation.

**Organization**: Tasks are grouped by user story (US1/US2 = P1, US3 = P2) to enable independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1, US2, US3 — maps to the user stories in spec.md
- Exact file paths are included in every task

## Path Conventions

- Source: `agent/performer/src/performer/proxy/` (the existing 078 self-hosted backend layer)
- New sub-package: `agent/performer/src/performer/proxy/translate/`
- Tests: `tests/unit/`
- Test runner: `.venv/bin/pytest` — lint: `.venv/bin/ruff check`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create the new package and deterministic test scaffolding.

- [X] T001 [P] Create the translate sub-package marker at `agent/performer/src/performer/proxy/translate/__init__.py` (empty package; will export `translate_request`, `translate_response`, the SSE filter class, and the finish-reason helper as they land).
- [X] T002 [P] Add a deterministic **stub OpenAI-wire upstream** fixture (placed as module constants in `agent/performer/tests/unit/proxy/fixtures/__init__.py` to match the existing 078 proxy convention, not `tests/unit/conftest.py` — layout alignment): canned `/v1/chat/completions` non-streaming JSON, a streaming SSE stream, a structured tool-call case, a non-2xx error case, and reusing the existing **harmony-leak** streaming case (raw harmony commentary instead of structured `tool_calls`, the LiteLLM #17246 failure shape). No live network (Constitution II).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Routing-model and shared-helper changes every user story depends on.

**⚠️ CRITICAL**: No user-story work can begin until this phase is complete.

- [X] T003 In `agent/performer/src/performer/proxy/routing.py`, add `"translate"` to the `Strategy` Literal and extend the load-time validator with the translate rules from `contracts/routing-table.md`: T1 (`translate` ⇒ `wire_format == "openai"`, else reject at load — FR-008), T2 (declared normalizers must be in `NORMALIZER_REGISTRY`), T3 (`base_url` required for translate). Leave `reroute`/`normalize` validation paths byte-for-byte unchanged (FR-006).
- [X] T004 [P] Create `agent/performer/src/performer/proxy/translate/finish_reason.py` with the single-source-of-truth `FINISH_REASON_MAP` and `map_finish_reason()` per `contracts/response-translation.md` (stop→end_turn, length→max_tokens, tool_calls→tool_use, content_filter→end_turn, unknown/absent/null→end_turn). Imported by both `response.py` and `sse.py` to prevent drift (Decision 6, FR-005).

**Checkpoint**: Routing recognizes/validates `translate`; the finish-reason helper exists. User stories can begin.

---

## Phase 3: User Story 1 - claude_code reaches an OpenAI-wire upstream, no LiteLLM (Priority: P1) 🎯 MVP

**Goal**: An Anthropic `/v1/messages` request is translated to OpenAI `/v1/chat/completions`, forwarded to an OpenAI-wire upstream, and the response (JSON and SSE) is translated back to Anthropic wire — with no LiteLLM in the path.

**Independent Test**: Configure a `(claude_code, <model>)` translate entry against the stub upstream; assert the upstream receives a well-formed OpenAI request and the CLI-facing reply is a valid Anthropic message object, for both non-streaming JSON and streaming SSE (quickstart S1).

### Tests for User Story 1 (write first, MUST fail) ⚠️

- [X] T005 [P] [US1] Write failing `tests/unit/test_translate_request.py` covering the FR-001 top-level field map from `contracts/request-translation.md`: model passthrough; `system` (string + block array) → leading system message; message turns; `max_tokens`/`temperature`/`top_p`/`stream` passthrough; `stop_sequences`→`stop`; `metadata` and `thinking` dropped (asserted, not silent).
- [X] T006 [P] [US1] Write failing `tests/unit/test_translate_response.py` covering FR-002/FR-005 non-streaming mapping from `contracts/response-translation.md`: `type:"message"`, `role:"assistant"`, content→text block, usage rename (`prompt_tokens`→`input_tokens`, `completion_tokens`→`output_tokens`), and `finish_reason`→`stop_reason` against the shared map.
- [X] T007 [P] [US1] Write failing `tests/unit/test_translate_sse.py` covering FR-003/FR-005 streaming: the Anthropic event sequence (`message_start`→`content_block_start`/`_delta`/`_stop`→`message_delta`→`message_stop`) and chunk-boundary safety (split a frame across `\n\n` buffer boundaries).
- [X] T008 [P] [US1] Write failing `tests/unit/test_health_translate.py` covering FR-010: healthy upstream → `gate()` returns `proceed`; untranslatable reply → `fail_closed` (or `rerouted` when `reroute_upstream` set); MUST NOT fail open (quickstart S6).

### Implementation for User Story 1

- [X] T009 [P] [US1] Implement `translate_request(anthropic_body: dict) -> dict` in `agent/performer/src/performer/proxy/translate/request.py` per `contracts/request-translation.md` top-level + message-turn mapping (text/system handling; tool blocks added in US2). Pure, no I/O, no body logging (FR-011). Makes T005 pass.
- [X] T010 [P] [US1] Implement `translate_response(openai_body: dict) -> dict` in `agent/performer/src/performer/proxy/translate/response.py`, importing `map_finish_reason` from `finish_reason.py` (FR-002/FR-005). Pure, no I/O. Makes T006 pass.
- [X] T011 [US1] Implement the OpenAI-SSE→Anthropic-SSE `StatefulSSEFilter` subclass in `agent/performer/src/performer/proxy/translate/sse.py` with per-stream state (`emitted_message_start`, open block index/type, accumulated tool-call args, captured `finish_reason`), importing the shared map (FR-003/FR-005). Depends on T004, T007. Makes T007 pass.
- [X] T012 [US1] In `agent/performer/src/performer/proxy/shim.py`, add the request-translate hook on the inbound `/v1/messages` body and the response-translate step on the OpenAI reply, composing the SSE translator as the **outermost** filter appended to `_FilterChain` after the normalizers (Decision 4, FR-001/2/3). Surface upstream non-2xx verbatim — translator NOT invoked (FR-012).
- [X] T013 [US1] In `agent/performer/src/performer/proxy/launch.py`, add the `strategy == "translate"` branch to `_launch_for_target`: launch the loopback `SelfHostedShim` on `127.0.0.1:0` and point `ANTHROPIC_BASE_URL` (claude_code provider env) at it (FR-006). Leave `reroute`/`normalize` dispatch unchanged.
- [X] T014 [US1] In `agent/performer/src/performer/proxy/health.py`, add the translate-target probe that exercises the full request-translate → forward → normalize → response-translate round trip and routes through `gate()`; fail-closed unless `reroute_upstream`, never fail-open (FR-010). Makes T008 pass.

**Checkpoint**: A claude_code role reaches an OpenAI-wire upstream end-to-end for text, JSON + SSE, with health gating. MVP deliverable.

---

## Phase 4: User Story 2 - Tool calls survive the round trip, no harmony leak (Priority: P1)

**Goal**: Tools and tool_choice reach the upstream; tool calls return as structured Anthropic `tool_use` blocks (composing with `harmony_tool_calls`); a follow-up `tool_result` turn back-translates to the OpenAI tool-result message — streaming and non-streaming, zero harmony leak.

**Independent Test**: Run a tool exchange against the harmony-leak stub; assert structured `tool_use` blocks with no harmony markers and that a `tool_result` turn produces an OpenAI `{role:"tool", tool_call_id, content}` message (quickstart S2).

### Tests for User Story 2 (write first, MUST fail) ⚠️

- [X] T015 [P] [US2] Write failing `tests/unit/test_translate_tool_roundtrip.py` covering FR-004: request `tools` (`input_schema`→`function.parameters`) and `tool_choice` shape map (auto→auto / any→required / tool→named); `tool_use`→`tool_calls[]` (`input`→JSON-stringified `function.arguments`); `tool_result`→`{role:"tool", tool_call_id, content}`; response/SSE `tool_use` block emission composing with `harmony_tool_calls`; degenerate non-JSON arguments → `input:{}` + metadata-only record; assert zero harmony markers in CLI-facing output.

### Implementation for User Story 2

- [X] T016 [US2] Extend `agent/performer/src/performer/proxy/translate/request.py` with the tools mapping, `tool_choice` shape map, `tool_use`→`tool_calls[]`, and `tool_result`→`tool` message back-translation per `contracts/request-translation.md` (FR-004).
- [X] T017 [US2] Extend `translate/response.py` and `translate/sse.py` to emit `tool_use` content blocks (`id`/`name`/parsed `input`; streaming `input_json_delta`) from already-normalized OpenAI `tool_calls`, with the degenerate-arguments handling (FR-004). Makes T015 pass.

**Checkpoint**: US1 + US2 complete — full tool round trip with no harmony leak (covers SC-001/SC-002 in the suite).

---

## Phase 5: User Story 3 - Opt-in per pair; nothing else changes (Priority: P2)

**Goal**: Activation is per `(backend, model)`; every other pair (openclaw/junie/codex/pi and unrouted claude_code) is byte-for-byte unchanged; contradictory entries rejected at load.

**Independent Test**: With one translate entry present, resolve other pairs and confirm identical pre-feature behavior; load a `translate`+`wire_format: anthropic` table and confirm load-time rejection (quickstart S3/S4/S5).

### Tests for User Story 3 (write first, MUST fail) ⚠️

- [X] T018 [P] [US3] Write failing `tests/unit/test_routing_translate.py` covering FR-006/FR-008: a valid `translate` entry loads; `translate`+`wire_format: anthropic` is rejected at load with an actionable message naming the pair and both fields (Rule T1); missing/malformed table fails closed naming the path + env var (FR-009).
- [X] T019 [P] [US3] Write failing `tests/unit/test_launch_translate.py` covering FR-007: snapshot `TargetDescriptor`s and launch dispatch for `reroute`/`normalize`/no-entry pairs with a translate entry present, asserting they are identical to pre-feature behavior; unrouted claude_code pair is a no-op.

### Implementation for User Story 3

- [X] T020 [US3] In `agent/performer/src/performer/proxy/routing.py`, finalize the actionable error messaging for contradictory combos (Rule T1) and unloadable/missing tables (path + pointing env var, FR-009), and confirm `reroute`/`normalize`/no-entry resolution paths are untouched (FR-007). Makes T018/T019 pass.

**Checkpoint**: All three user stories independently functional; no regression on other pairs.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Performance budget, lint, coverage, and quickstart validation.

- [X] T021 [P] Add the **SC-006 performance benchmark** in `tests/unit/test_translate_benchmark.py`: measure in-process translation overhead (request translate + response/SSE translate, excluding upstream/network) for a qa-representative exchange (system + user turn, tools present, streaming) against the stub upstream; assert **≤ 5 ms median and ≤ 15 ms p99** per request. Deterministic, no live network.
- [X] T022 [P] Run `.venv/bin/ruff check` on all new/modified files (`proxy/translate/*.py`, `routing.py`, `shim.py`, `launch.py`, `health.py`, new tests) and fix offenses.
- [X] T023 Run `.venv/bin/pytest` for the full suite; confirm all translate tests pass and overall coverage has not decreased (Constitution II).
- [X] T024 Validate the 8 quickstart.md scenarios (S1–S8) map to passing tests; confirm S7 (non-2xx surfaced verbatim) and S8 (no secrets/bodies logged, FR-011) are explicitly covered.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately.
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories.
- **US1 (Phase 3)**: Depends on Foundational. MVP.
- **US2 (Phase 4)**: Depends on US1 (extends `request.py`/`response.py`/`sse.py`).
- **US3 (Phase 5)**: Depends on Foundational (routing). Independent of US1/US2 implementation but shares `routing.py`.
- **Polish (Phase 6)**: Depends on all desired user stories complete.

### Within Each User Story

- Tests (T005–T008, T015, T018–T019) written and FAILING before their implementation.
- Pure translators (`request.py`/`response.py`) before the SSE filter and shim wiring.
- `shim.py`/`launch.py`/`health.py` integration after the pure translators exist.

### Parallel Opportunities

- T001, T002 (Setup) in parallel.
- T004 parallel with T003 (different files).
- US1 tests T005, T006, T007, T008 in parallel (different files).
- US1 pure-translator impls T009, T010 in parallel; T011 after T004.
- US3 tests T018, T019 in parallel.
- Polish T021, T022 in parallel.

---

## Implementation Strategy

### MVP First (US1)

1. Phase 1 Setup → Phase 2 Foundational → Phase 3 US1.
2. **STOP and VALIDATE**: text exchange JSON + SSE end-to-end against the stub, health gating works.

### Incremental Delivery

1. Setup + Foundational → ready.
2. US1 → text round trip (MVP).
3. US2 → tool round trip, no harmony leak.
4. US3 → opt-in safety + no-regression guarantees.
5. Polish → SC-006 benchmark, lint, coverage, quickstart.

---

## Notes

- [P] = different files, no incomplete dependencies.
- TDD: verify each test fails before implementing.
- Security invariants (FR-011): no auth tokens or request/response bodies in logs — only method/path/status/latency + which translator/normalizers ran. `auth_env` holds the env-var NAME, never the secret.
- Commit after each task or logical group; bundle drive-by fixes on this branch (no follow-ups).
