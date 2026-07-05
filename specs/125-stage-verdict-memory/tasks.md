# Tasks: Stage Verdict Memory

**Input**: Design documents from `/specs/125-stage-verdict-memory/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/{skip-decision,state-schema-v14}.md, quickstart.md

**Tests**: TDD required (plan Constitution Check II). Contract tables (V/R/D/F/W rows, N invariants) are the test oracle.

**Organization**: Schema v13 is foundational (all four stories persist through it). US1 = verdict cache (MVP), US2 = doc-gate re-keying, US3 = shared fetch, US4 = comment watermark.

## Phase 1: Setup

- [x] T001 Confirm baseline: run `.venv/bin/pytest tests/unit -q` and `tests/contract -q` green; record counts (SC-006 regression reference). Use `PYTHONPATH=$PWD/src:$PWD/agent/performer/src` with the main checkout's venv when running from the worktree.

## Phase 2: Foundational — snapshot schema v14

- [x] T002 Write failing contract test `tests/contract/test_state_persistence_v13_to_v14.py` (mirror the v11→v12 test): v13 snapshot loads with `stage_verdicts == {}` / `processed_issue_comment_ids == []` / `last_issue_comment_id is None`; v14 round-trips all three losslessly; corrupted `stage_verdicts` entry (extra key / empty head_sha) drops that entry with a warning, never crashes the load
- [x] T003 Implement in `src/coordinare/state_store.py`: `StageVerdict` model (`head_sha`/`verdict` non-empty, `recorded_at` str, `extra="forbid"`), `PersistedSession.stage_verdicts: dict[str, StageVerdict] = {}`, `WorkflowSnapshot.processed_issue_comment_ids: list[int] = []` + `last_issue_comment_id: int | None = None`, per-entry tolerant validation, SCHEMA_VERSION 12→13 + header comment documenting v13
- [x] T004 Plumb per-card field in `src/coordinare/session.py` (`CardSession.stage_verdicts` + `_SESSION_FIELDS`) and `src/coordinare/daemon.py` (persist in `_persist_active_sessions`, restore in `_restore_from_snapshot`), plus `src/coordinare/graph/state.py` (`stage_verdicts` key + initial_state default, `override_forced_dispatch: str | None` transient key)
- [x] T005 Write failing unit tests in `tests/unit/test_state_store.py` for v13 field defaults + serialisation shape (sorted bounded comment list), then run T002+T005 tests green

**Checkpoint**: schema lands; behaviour byte-identical (nothing reads the new fields yet).

## Phase 3: User Story 1 — Skip verdict stages whose input has not changed (P1) 🎯 MVP

**Goal**: contract R1–R4 recording + V1–V7 skip decisions; fail-open everywhere; overrides always dispatch.

**Independent Test**: with a seeded `stage_verdicts` record and a stub github returning a matching `head_ref_oid`, dispatch of that stage advances without invoking the performer; every V-row negation dispatches.

- [x] T006 [US1] Write failing tests `tests/unit/graph/nodes/test_monitor_performer_verdict_record.py` for recording rules R1–R4: passing marker per stage records `{head_sha, verdict}` from `status.head_after` (fallback `head_sha`); no-head → no record; implementing/assessing markers → no record; marker/stage mismatch (e.g. `approved` while stage==qa) → no record; persona-scope/override-skip advancement → no record
- [x] T007 [US1] Implement recording in `src/coordinare/graph/nodes/monitor_performer.py` terminal-success block (before `_advance_stage` at ~:3395): module-level `VERDICT_STAGES` + `EXPECTED_MARKER` maps, write `state["stage_verdicts"]` (dict of plain dicts at state level, validated at persist), structured debug log on R2
- [x] T008 [US1] Write failing tests `tests/unit/graph/nodes/test_dispatch_verdict_cache.py` for decision table V1–V7 + invariants N1/N2/N3: stub github `check_mergeability` (matching OID → skip + `stage_skipped`/`verdict_cached` log + `_advance_stage` effect; mismatching OID / raise / empty OID → dispatch; relay_feedback non-empty → dispatch; `override_forced_dispatch` set → dispatch + flag cleared; implementing stage never consults the cache)
- [x] T009 [US1] Implement the skip check in `src/coordinare/graph/nodes/dispatch_performer.py` `_dispatch_performer_body` immediately after the persona-scope skip block (~:762): helper `_verdict_cache_skip(state, card, stage) -> bool` per contract order V1–V7, one `check_mergeability` call whose `head_ref_oid` is stashed local for the doc gate (US2 reuse)
- [x] T010 [US1] Implement the override veto flag in `src/coordinare/graph/nodes/monitor_performer.py` `_apply_pending_override` (`restart` action sets `state["override_forced_dispatch"] = target`), with a test in `tests/unit/graph/nodes/test_monitor_performer_verdict_record.py` asserting restart sets it and skip/veto actions do not
- [x] T011 [US1] Run US1 tests + existing dispatch/monitor suites: `.venv/bin/pytest tests/unit/graph/nodes -q` — green, zero regressions

**Checkpoint**: US1 shippable — no-op bounces stop re-dispatching passed stages.

## Phase 4: User Story 2 — Documenting gate keyed to the last documentation pass (P2)

**Goal**: contract D1–D5; completes deferred 123 FR-003; fail-open chain compare → 123 gate → dispatch.

**Independent Test**: seeded `stage_verdicts["documenting"]` + stub compare returning code-only paths → skip `no_doc_changes_since_last_pass`; docs path → dispatch; compare raising → falls back to 123 gate.

- [x] T012 [P] [US2] Write failing tests (landed in `tests/unit/services/test_github_service.py`, colocated with the existing diff tests to reuse `_make_branch_service`) for `compare_changed_files`: parses `files[].filename` across pages (per_page=100, ≤3 pages), raises on page-cap overflow / non-2xx / malformed pr_url / missing token; never logs token (assert via log capture)
- [x] T013 [P] [US2] Implement `compare_changed_files(pr_url, base_sha, head_sha) -> list[str]` in `src/coordinare/services/github.py` on the existing `_rest_api_base()`/`_current_token()`/httpx plumbing (REST `GET /repos/{o}/{r}/compare/{base}...{head}`, JSON accept)
- [x] T014 [US2] Write failing tests `tests/unit/graph/nodes/test_dispatch_doc_gate_sha.py` for D1–D5: prior doc pass + code-only compare → skip with `no_doc_changes_since_last_pass`; docs path in compare → dispatch; compare raises → falls back to whole-PR 123 gate; no prior pass → 123 gate unchanged; both unavailable → dispatch (N1)
- [x] T015 [US2] Rewire the documenting gate in `src/coordinare/graph/nodes/dispatch_performer.py` per D1–D5, reusing the `head_ref_oid` stashed by T009 (no second mergeability call) and `stage_verdicts["documenting"].head_sha` as `S_doc`
- [x] T016 [US2] Run US2 tests + 123's existing doc-gate tests — green (123 semantics intact for the no-prior-pass path)

## Phase 5: User Story 3 — One PR-diff fetch per dispatch evaluation (P3)

**Goal**: contract F1/F2 — collapse `_fetch_changed_files` + `_fetch_pr_diff_text` into one `_fetch_pr_data`.

**Independent Test**: counting stub github: documenting dispatch with doc changes performs exactly one `get_pr_diff` call end-to-end.

- [x] T017 [US3] Write failing test in `tests/unit/graph/nodes/test_dispatch_doc_gate_sha.py`: counting `get_pr_diff` stub asserts exactly 1 call on a documenting dispatch that passes the gate and injects the diff (F1); fetch failure yields gate-fallback-to-dispatch AND omitted inline diff (F2)
- [x] T018 [US3] Implement `_fetch_pr_data(state, card) -> tuple[str | None, list[str] | None]` in `src/coordinare/graph/nodes/dispatch_performer.py`; collapse both helpers' call sites (gate consumes `changed_files`, `_DIFF_REVIEW_ROLES` injection consumes sanitized raw); delete the now-unused helpers (no other callers — verify with grep before deleting)
- [x] T019 [US3] Run US2+US3 test files + full `tests/unit/graph/nodes` — green

## Phase 6: User Story 4 — Issue-comment dedup survives restart (P4)

**Goal**: contract W1–W3 — persisted, bounded watermark; route_issue_comments untouched.

**Independent Test**: save snapshot with populated comment state → fresh daemon restore → `processed_issue_comment_ids` set + `last_issue_comment_id` intact; >2000 IDs persist only the largest 2000.

- [x] T020 [US4] Write failing tests `tests/unit/test_daemon_comment_watermark.py`: save/restore round-trip (W1/W2), bound at 2000 keeping numerically largest, pre-v13 snapshot restores to empty set/None (W2), `route_issue_comments` file untouched (assert no import/logic change needed — behavioural test seeds state and verifies dedup skip after simulated restart)
- [x] T021 [US4] Implement persist/restore in `src/coordinare/daemon.py` (`_build_snapshot` + `_restore_from_snapshot`), mirroring the per-card session round-trip of the other PersistedSession fields
- [x] T022 [US4] Run US4 tests + `tests/unit/test_daemon*.py` — green

## Phase 7: Polish & Cross-Cutting

- [x] T023 [P] Run `.venv/bin/ruff check` on all touched files — zero warnings
- [x] T024 Run full suites: `tests/unit -q`, `tests/contract -q`, `tests/integration -q` — no regressions vs T001 counts
- [x] T025 Traceability pass: tick every contract row (V1–V7, R1–R4, D1–D5, F1–F2, W1–W3, N1–N4) against a named test in the four new test files
- [x] T026 Update `specs/125-stage-verdict-memory/quickstart.md` if any implementation detail drifted (log event names, bounds)

## Dependencies

- T001 → Phase 2 (T002–T005) → US1 (T006–T011) → US2 (T012–T016) → US3 (T017–T019); US4 (T020–T022) depends only on Phase 2 and may run any time after it; Polish last.
- US2 reuses US1's mergeability fetch (T009 stashes `head_ref_oid`); US3 refactors the fetch surface US2's fallback consumes — keep the order US1 → US2 → US3.

## Parallel Example

- T012+T013 (github service, own files) can proceed in parallel with T014's test authoring.
- US4 (T020–T022) is fully parallel to US1–US3 after Phase 2.

## Implementation Strategy

MVP = Phase 2 + Phase 3 (US1): the verdict cache alone removes the bulk of redundant dispatches. US2 completes the 123 FR-003 debt, US3 is a small API-efficiency cleanup piggybacking on US2's rewiring, US4 is an independent restart-cost fix sharing the schema bump.
