# Tasks: Terminal-Success Progress Floors

**Input**: Design documents from `/specs/126-terminal-success-floors/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/{success-floors,state-schema-v15}.md, quickstart.md
**Branch stacking**: built on `125-stage-verdict-memory` (uses `stage_verdicts`, `_verdict_cache_check`, `_record_stage_verdict`).

**Tests**: TDD required. Contract tables (L/F/D/U rows, I invariants) are the oracle.

**Organization**: schema v15 + wire contract are foundational; US1 = implementer floor (MVP), US2 = ledger/dispositions/disputes, US3 = documenting no-op. US2's ledger stamping is needed by US1's floor (origin SHA), so the stamping half of US2 lands in Foundational.

## Phase 1: Setup

- [x] T001 Confirm baseline on the stacked branch: `tests/unit -q` and `tests/contract -q` green (use `PYTHONPATH=$PWD/src:$PWD/agent/performer/src` + main-checkout venv); record counts

## Phase 2: Foundational — schema v15 + ledger stamping + wire fields

- [x] T002 Write failing contract test `tests/contract/test_state_persistence_v13_to_v14.py`: v13 loads with `feedback_ledger==[]`/`feedback_origin_sha is None`/`noop_success_retries==0`; v14 round-trips all three; malformed ledger entries drop, never crash; relax the v13 pin test to `>= 13`
- [x] T003 Implement in `src/coordinare/state_store.py`: `FeedbackItemRecord` model (`extra="forbid"`, disposition/round_status literals per contracts/state-schema-v15.md), three `PersistedSession` fields with tolerant validation, SCHEMA_VERSION 13→14 + header comment; update `specs/003-state-persistence/contracts/workflow-snapshot.schema.json` (enum + per-session properties)
- [x] T004 Plumb `src/coordinare/session.py` (CardSession fields + `_SESSION_FIELDS` + `create_session_from_card` defaults), `src/coordinare/graph/state.py` (keys + initial_state), `src/coordinare/daemon.py` (persist coercion incl. ledger-entry validation + restore), mirroring 125's stage_verdicts plumbing
- [x] T005 Write failing tests `tests/unit/graph/nodes/test_feedback_ledger.py` for stamping rules L1–L3: each bounce site (reviewer changes_requested, CI-gate bounce, security_failed, qa_failed) assigns monotonic ids + raiser (`ci` for the gate), sets `feedback_origin_sha` from the verdict head, digests bodies ≤200 chars, marks the prior round `previous` and prunes older, resets `noop_success_retries`
- [x] T006 Implement `_stamp_feedback_bounce(state, items, raiser, origin_sha)` in `src/coordinare/graph/nodes/monitor_performer.py` and wire it into the four bounce sites; relay entries carry `id`/`raiser`/`re_raised` inline
- [x] T007 Add `feedback_dispositions: list[dict] = []` to BOTH `src/coordinare/protocol.py` and `agent/performer/src/performer/protocol.py` ProtocolResponse models; register the wire additions in `specs/contracts/dispatch-payload.md` (relay entry keys, `disputed_feedback` context field, response field)

**Checkpoint**: ledger populates and persists; behaviour otherwise unchanged (nothing consumes it yet).

## Phase 3: User Story 1 — Implementer head-delta floor (P1) 🎯 MVP

**Goal**: contract F1–F7 + I1–I3: no-op "done" after a feedback bounce gets one strengthened re-dispatch then a hold, without touching content/transient budgets.

**Independent Test**: seeded `feedback_origin_sha` + completion with `head_after == origin` and no dispositions → F4 re-dispatch; repeat → F5 hold; head moved → F2 accept.

- [x] T008 [US1] Write failing tests `tests/unit/graph/nodes/test_success_floor.py` for F1–F7: no origin → accept; head moved → accept (+ dispositions applied); head unmoved + disputed → accept + queue; head unmoved + none disputed → F4 retry (relay names unaddressed ids; budgets untouched; `success_floor_retry` logged) then F5 hold (`success_floor_hold`, phase blocked, open_questions name head + ids); missing head_after → accept; reviewing/qa stages never evaluated
- [x] T009 [US1] Implement `_evaluate_success_floor(state, status) -> tuple[dict, bool]` in `src/coordinare/graph/nodes/monitor_performer.py`, called in the implementing terminal-success branch after the env-health taint check and BEFORE the CI gate; reuse the 070/072 strengthened-re-dispatch shape; `noop_success_retries` reset on head move
- [x] T010 [US1] Run US1 + ledger tests + `tests/unit/graph/nodes -q` — green, zero regressions

**Checkpoint**: US1 shippable — no-op thrash is bounded.

## Phase 4: User Story 2 — Dispositions + dispute adjudication (P2)

**Goal**: contract D1–D6 + I4: disputes reach the raiser, rounds resolve, CI disputes hold, 125 cache vetoed.

**Independent Test**: disputed item → raiser's next dispatch context carries `disputed_feedback`; raiser pass/bounce closes the round accepted/rejected; re-raised + second unmoved dispute → hold; ci raiser → hold.

- [x] T011 [US2] Extend `tests/unit/graph/nodes/test_feedback_ledger.py` with disposition application (I4: unknown ids ignored+logged, one disposition per round) and D2/D3 round resolution on raiser pass/bounce
- [x] T012 [US2] Write failing tests for D1/D4/D5/D6 in `tests/unit/graph/nodes/test_success_floor.py` (dispute queue → dispatch context injection; re-raised round second dispute → hold; ci dispute → hold) and in `tests/unit/graph/nodes/test_dispatch_verdict_cache.py` (V2b: pending dispute for stage vetoes the 125 skip)
- [x] T013 [US2] Implement disposition application + round resolution in `monitor_performer.py` (apply on implementing success; resolve on raiser terminal verdicts), `disputed_feedback` injection in `dispatch_performer.py`, and the V2b veto in `_verdict_cache_check`
- [x] T014 [US2] Update the implementer persona in `src/coordinare/services/persona_service.py`: require a disposition per delivered feedback id (`addressed` with summary / `disputed` with reason); update performer-side extraction in `agent/performer/src/performer/main.py` (implementer report → `feedback_dispositions` passthrough) with a unit test in `agent/performer/tests/`
- [x] T015 [US2] Run US2 tests + performer suite (`agent/performer` tests) — green

## Phase 5: User Story 3 — Documenting no-op completion (P3)

**Goal**: contract U1/U2: zero-change `docs_committed` advances without minting a documentation pass.

**Independent Test**: `docs_committed` with empty files_modified + no head delta → advance + `documenting_noop_completion` + `stage_verdicts.documenting` unchanged; with files or head delta → 125 records as today.

- [x] T016 [US3] Write failing tests `tests/unit/graph/nodes/test_documenting_noop_completion.py` for U1/U2 (including: no-op completion never bounces, and a later real pass records normally)
- [x] T017 [US3] Implement the U2 branch in `monitor_performer.py`'s documenting terminal-success path (suppress `_record_stage_verdict`, emit the event, advance)
- [x] T018 [US3] Run US3 tests + 125's verdict-record tests — green

## Phase 6: Polish & Cross-Cutting

- [x] T019 [P] `ruff check` all touched files — zero warnings
- [x] T020 Full suites: `tests/unit -q`, `tests/contract -q`, `tests/integration -q`, performer tests — no regressions vs T001
- [x] T021 Traceability: tick every contract row (L1–L3, F1–F7, D1–D6, U1–U2, I1–I5) against a named test
- [x] T022 Update quickstart.md/data-model.md if implementation details drifted

## Dependencies

- T001 → Phase 2 (T002–T007) → US1 (T008–T010) → US2 (T011–T015) → US3 (T016–T018) → Polish. US3 only depends on Phase 2 + 125's recorder and may run before US2 if convenient.

## Implementation Strategy

MVP = Phase 2 + Phase 3: the ledger stamping + head floor close the thrash loop even before dispositions exist (missing dispositions on an unmoved head trip the floor — which is the correct conservative reading until US2 lands the contract).
