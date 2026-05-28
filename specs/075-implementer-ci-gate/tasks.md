# Tasks: Implementer CI Gate

**Input**: Design documents from `/specs/075-implementer-ci-gate/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: TDD required by Constitution Principle II. Test tasks are MANDATORY for every implementation slice.

**Organization**: Tasks are grouped by user story so each story is independently implementable and testable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no incomplete dependencies)
- **[Story]**: Maps to user stories from spec.md (US1, US2, US3, US4)
- File paths are absolute-style relative to repo root

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Repo-level scaffolding shared by every user story below. No production-code edits in this phase.

- [X] T001 [P] Create test scaffolding skeleton at `tests/unit/services/test_required_checks_resolver.py` with module imports + a single skipped placeholder test (so pytest collection passes during incremental work)
- [X] T002 [P] Create test scaffolding skeleton at `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py` with imports + skipped placeholder
- [X] T003 [P] Create test scaffolding skeleton at `tests/unit/graph/nodes/test_notify_ci_gate_rollup.py` with imports + skipped placeholder
- [X] T004 [P] Create contract test files `tests/contract/test_gate_decision_schema.py` and `tests/contract/test_persona_check_map_schema.py` with imports + skipped placeholders
- [X] T005 [P] Create integration test file `tests/integration/test_implementer_ci_gate_e2e.py` with imports + skipped placeholder

**Checkpoint**: Pytest collects all new files without errors; CI green on a no-op commit.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Config models, session schema, persistence migration, and the `decide()` extension — every user story phase below depends on these.

**⚠️ CRITICAL**: No user story work begins until Phase 2 completes.

- [X] T006 Add `CIGateConfig` and `PersonaCheckMapPerDepth` + `PersonaCheckMapConfig` pydantic models in `src/coordinare/config.py`, nested under the existing `PersonaScopeConfig` (per `contracts/config-schema.md`)
- [ ] T007 [P] Write `tests/contract/test_persona_check_map_schema.py` exercising the 7 error-case rows in `contracts/config-schema.md` (replace placeholder from T004)
- [X] T008 Add `bounce_counter: dict[str, int]` field to `CardSession` TypedDict in `src/coordinare/session.py` and extend `_SESSION_FIELDS` tuple to include it (canonical 073/074 drift trap — must round-trip)
- [X] T009 Initialize `bounce_counter={}` in `create_session_from_card()` in `src/coordinare/session.py`
- [X] T010 Add `bounce_counter: dict[str, int] = Field(default_factory=dict)` to `PersistedSession` in `src/coordinare/state_store.py`; bump `CURRENT_SCHEMA = 5`, leave `MIN_SCHEMA` unchanged; add `_migrate_v4_to_v5` setting the default
- [X] T011 Write session round-trip regression test in `tests/unit/test_session.py`: build a `CardSession` with non-empty `bounce_counter`, push through state ↔ PersistedSession ↔ JSON ↔ disk, assert preservation. **Mirrors the four 073/074 regressions — non-negotiable.**
- [X] T012 Write v4→v5 migration test in `tests/unit/test_state_store.py`: load a v4 snapshot, assert `bounce_counter={}` on the resulting `PersistedSession`
- [X] T013 Extend `services/pr_checks_policy.decide(rollup, ..., required_check_names: set[str] | None = None)` in `src/coordinare/services/pr_checks_policy.py` — when set, filter `rollup` entries to those names before deciding; when None, preserve existing 064 behavior
- [X] T014 [P] Add regression test in `tests/unit/services/test_pr_checks_policy.py` asserting existing 064 callers (no `required_check_names` argument) get byte-identical decisions
- [X] T015 [P] Add new test cases in `tests/unit/services/test_pr_checks_policy.py` covering `decide(..., required_check_names={...})` filtering behavior (PASS / HOLD / BOUNCE / unknown-required)
- [X] T016 Add `get_required_status_checks(default_branch: str) -> set[str]` helper to `src/coordinare/services/github_service.py` if not present (fetches branch-protection required-status-checks set); cache per-cycle
- [X] T017 [P] Write unit test in `tests/unit/services/test_github_service.py` for `get_required_status_checks` — happy path, empty branch protection, 404, transient error

**Checkpoint**: Foundation ready; config parses; sessions round-trip; `decide()` extended without breaking 064; helper available.

---

## Phase 3: User Story 1 — Red CI bounces implementer back (Priority: P1) 🎯 MVP

**Goal**: At implementer→reviewer handoff, when any required check on HEAD is in `failure`/`timed_out`/`cancelled`, coordinare MUST NOT advance and MUST re-dispatch implementer with structured feedback naming each failing check. (FR-001, FR-003, FR-008, FR-010, FR-015)

**Independent Test**: Submit a card whose PR HEAD has a failing required check; verify the lifecycle stays in `monitoring_performer`, the bounce counter increments, a `relay_feedback` entry of `source: "ci_gate"` shape lands on the session, and on a new HEAD push the counter resets.

### Tests for User Story 1 (write FIRST, must FAIL before implementation)

- [X] T018 [P] [US1] In `tests/contract/test_gate_decision_schema.py`, assert the `CIGateDecision` JSON shape from `contracts/gate-decision.md` §1 — round-trip a bounce decision, assert all field rules including verdict enum, 40-char SHA, sorted `required_checks`, populated `failed_checks`, ISO-8601 `decided_at`
- [X] T019 [P] [US1] In `tests/contract/test_gate_decision_schema.py`, assert the `relay_feedback` entry shape from `contracts/gate-decision.md` §2 — `source == "ci_gate"`, populated `failed_checks`, `bounce_count_after`, `max_bounces`
- [X] T020 [P] [US1] In `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py`, add `test_gate_bounces_on_failing_required_check` — implementer terminal + 1 failing required check → `phase: monitoring_performer`, `bounce_counter[head]==1`, relay_feedback appended
- [X] T021 [P] [US1] In `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py`, add `test_gate_pass_on_all_green` — all required checks success → `_advance_stage` invoked, no relay_feedback, `bounce_counter[head]` unchanged
- [X] T022 [P] [US1] In `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py`, add `test_gate_escalates_at_bounce_limit` — counter at `max_bounces_per_head - 1`, another bounce → `phase: needs_human_review`, counter incremented to limit
- [X] T023 [P] [US1] In `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py`, add `test_counter_resets_on_new_head` — counter populated for old HEAD, new HEAD bounce → `bounce_counter[new_head]==1`, old entry preserved
- [X] T024 [P] [US1] In `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py`, add `test_gate_fails_open_on_exception` — resolver raises → log + advance, no block on pipeline (FR-011 fail-open)
- [X] T025 [P] [US1] In `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py`, add `test_gate_noop_when_disabled` — `ci_gate.enabled=False` → existing 064 behavior, no gate eval
- [X] T025a [P] [US1] In `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py`, add `test_gate_passes_with_neutral_and_skipped_required_checks` — required checks include one `neutral` and one `skipped` conclusion alongside `success` → verdict `pass`, no bounce (FR-007). Asserts 064's fail-conclusions set is preserved under the new `required_check_names` filter.

### Implementation for User Story 1

- [X] T026 [P] [US1] Create `src/coordinare/services/required_checks_resolver.py` with `resolve(scope, branch_protection_set, all_head_checks, persona_check_map) -> RequiredChecksList`. Implement layer 3 (all_head_checks) only for US1 — US3 will fill in layers 1 and 2. Return `{names: sorted(all_head_checks), source: "all_head_checks"}`.
- [X] T027 [US1] Add unit tests for the layer-3 path in `tests/unit/services/test_required_checks_resolver.py` (replacing the T001 placeholder)
- [X] T028 [US1] In `src/coordinare/graph/nodes/monitor_performer.py`, add `_get_ci_gate_config(config)` and `_evaluate_ci_gate(state, session, current_card)` helpers mirroring the 064 helper pair at lines ~599–824. `_evaluate_ci_gate` returns a `CIGateDecision` dict per `contracts/gate-decision.md` §1.
- [X] T029 [US1] In `src/coordinare/graph/nodes/monitor_performer.py` at the implementing→reviewing transition (~lines 1317–1359, where `marker in TERMINAL_SUCCESS_STATES` calls `_advance_stage`), insert: if `_get_ci_gate_config(config).enabled` and stage==`implementing` → call `_evaluate_ci_gate`; route on verdict: `pass` → existing `_advance_stage`; `hold` → return `{"phase": "monitoring_performer"}`; `bounce` → increment `session["bounce_counter"][head_sha]`, append `relay_feedback` entry per contracts §2, return `{"phase": "monitoring_performer"}`; `escalate` → set `{"phase": "needs_human_review"}`.
- [X] T029a [P] [US1] In `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py`, add `test_gate_noop_when_no_pr` — session has no PR / no head_sha → gate early-returns PASS verdict with `resolver_source: "all_head_checks"` and empty `required_checks`; lifecycle defers to 070's implementer-commit-floor logic (FR-013)
- [X] T029b [US1] In `_evaluate_ci_gate` (`monitor_performer.py`), before the resolver call, check `head_sha = session.get("head_sha")` — if falsy, return `CIGateDecision(verdict="pass", head_sha="", required_checks=[], failed_checks=[], pending_checks=[], resolver_source="all_head_checks", bounce_count_after=0, decided_at=now())` so the routing layer advances and 070 handles the no-commit case (FR-013)
- [X] T030 [US1] Wrap the entire gate-eval block in `try/except Exception as exc` with `logger.warning("ci_gate.node_error", ...)` and fall through to existing `_advance_stage` (FR-011 fail-open, mirrors 074's `classify_scope_node` pattern)
- [X] T031 [US1] Emit `event=ci_gate.decided` structlog entry with: `card_id`, `verdict`, `head_sha`, `required_checks`, `failed_checks`, `bounce_count_after`, `resolver_source` (FR-012)
- [X] T032 [US1] Verify all T018–T025 tests now pass; run `.venv/bin/pytest tests/unit/graph/nodes/test_monitor_performer_ci_gate.py tests/contract/test_gate_decision_schema.py -v`

**Checkpoint**: Red CI bounces. Green CI advances. Bounce limit escalates. Counter resets per HEAD. Gate fails open. **US1 is the MVP and is independently shippable.**

---

## Phase 4: User Story 2 — Pending CI waits, doesn't advance (Priority: P1)

**Goal**: When required checks are still pending (`queued`/`in_progress`), the gate HOLDs (lifecycle stays in `monitoring_performer`) until checks resolve or `pending_timeout_seconds` is exceeded; on timeout, treat as failure for bounce purposes. (FR-002, FR-004, FR-005)

**Independent Test**: Submit a card whose required checks are `in_progress`; verify the gate returns `phase: monitoring_performer` without advancing or bouncing; after exceeding `pending_timeout_seconds`, verify a BOUNCE decision treats the timeout as a failure.

### Tests for User Story 2 (write FIRST, must FAIL before implementation)

- [X] T033 [P] [US2] In `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py`, add `test_gate_holds_on_pending_required_check` — required check `in_progress` → `phase: monitoring_performer`, no counter increment, no relay_feedback, `pending_checks` populated in decision
- [X] T034 [P] [US2] In `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py`, add `test_gate_bounces_on_pending_timeout` — pending check older than `pending_timeout_seconds` → BOUNCE with timeout described in feedback
- [X] T035 [P] [US2] In `tests/unit/services/test_pr_checks_policy.py`, verify the `pending_timeout_seconds` argument propagates through `decide(..., required_check_names={...}, pending_timeout_seconds=N)`

### Implementation for User Story 2

- [X] T036 [US2] In `_evaluate_ci_gate` (`monitor_performer.py`), pass `pending_timeout_seconds` from `CIGateConfig` into `decide(rollup, required_check_names=..., pending_timeout_seconds=cfg.pending_timeout_seconds)` — semantics are 064's
- [X] T037 [US2] Map `decide()` `GateDecision.HOLD` → verdict `hold`; populate `CIGateDecision.pending_checks` from `rollup.pending_check_names()` intersected with `required_checks`
- [X] T038 [US2] On the HOLD branch in T029's routing, return `{"phase": "monitoring_performer"}` without touching `bounce_counter` or `relay_feedback` (HOLD ≠ BOUNCE)
- [X] T039 [US2] Verify T033–T035 pass

**Checkpoint**: Pending checks HOLD; timeouts convert to BOUNCE. The gate now distinguishes the three real states (PASS / HOLD / BOUNCE) correctly.

---

## Phase 5: User Story 3 — Per-card required-checks from 074 + branch-protection fallback (Priority: P2)

**Goal**: Resolver layers 1 (persona_check_map ∩ PersonaScope) and 2 (branch-protection) replace US1's vacuous layer-3-only behavior. (FR-006, FR-007, FR-009 soft dep on 074, FR-014)

**Independent Test**: Submit a docs-only card with `persona_scope.persona_check_map.implementer.skim = ["lint*"]` and verify only lint checks are gated, even if integration tests are red. Disable 074, verify branch-protection set takes over. Remove branch protection, verify falls through to all-checks (US1 behavior preserved).

### Tests for User Story 3 (write FIRST, must FAIL before implementation)

- [X] T040 [P] [US3] In `tests/unit/services/test_required_checks_resolver.py`, add `test_resolver_layer1_persona_check_map` — `scope.personas["implementer"].depth=="normal"`, map has `implementer.normal=["lint*", "unit*"]`, HEAD checks include `lint`, `unit-tests`, `integration` → resolver returns `{names: ["lint", "unit-tests"], source: "persona_check_map"}`
- [X] T041 [P] [US3] In `tests/unit/services/test_required_checks_resolver.py`, add `test_resolver_layer1_empty_intersection_falls_through` — patterns match no actual checks → falls through to layer 2
- [X] T042 [P] [US3] In `tests/unit/services/test_required_checks_resolver.py`, add `test_resolver_layer2_branch_protection` — no persona_check_map; branch_protection_set non-empty → use it; source: `branch_protection`
- [X] T043 [P] [US3] In `tests/unit/services/test_required_checks_resolver.py`, add `test_resolver_layer3_all_checks_fallback` — both prior layers empty → all_head_checks; source: `all_head_checks`
- [X] T044 [P] [US3] In `tests/unit/services/test_required_checks_resolver.py`, add `test_resolver_persona_absent_from_map` — persona name not in map → falls through to layer 2
- [X] T045 [P] [US3] In `tests/unit/services/test_required_checks_resolver.py`, add `test_resolver_glob_patterns` — `"lint*"` matches `lint`, `lint-py`, `lint-js`; `"build (3.11, *)"` matches matrix jobs
- [X] T046 [P] [US3] In `tests/unit/graph/nodes/test_monitor_performer_ci_gate.py`, add `test_gate_passes_when_only_advisory_check_failed` — required set is `{lint}` (green); `integration` is failing but not required → PASS, integration surfaced as advisory (FR-014)

### Implementation for User Story 3

- [X] T047 [US3] In `src/coordinare/services/required_checks_resolver.py`, implement layer 1: read `scope.personas[persona].depth`, look up `persona_check_map[persona][depth]` pattern list, glob-match against `all_head_checks` using `fnmatch.fnmatch`. Return sorted `names` + `source: "persona_check_map"` if non-empty; else fall through.
- [X] T048 [US3] In `required_checks_resolver.py`, implement layer 2: if `branch_protection_set` non-empty, return sorted intersection with `all_head_checks` + `source: "branch_protection"`; else fall through to layer 3.
- [X] T049 [US3] In `monitor_performer.py`'s `_evaluate_ci_gate`, fetch inputs for the resolver: `scope = session.get("persona_scope")` (may be None), `branch_protection_set = github_service.get_required_status_checks(default_branch)` (may be empty / error → empty), `all_head_checks = {entry.name for entry in rollup.entries}`, `persona_check_map = config.persona_scope.persona_check_map`. Pass through to `resolver.resolve(...)`.
- [X] T050 [US3] On the PASS verdict, compute advisory failures = `{entry for entry in rollup.failed() if entry.name not in required_checks}` and attach to the dispatch context for reviewer (per FR-014; surface via existing reviewer-prompt context channel)
- [X] T051 [US3] Verify T040–T046 pass

**Checkpoint**: 074-aware narrowing works for cards with PersonaScope; branch-protection works for cards without; all-checks works for repos with neither.

---

## Phase 6: User Story 4 — Gate state auditable on the PR (Priority: P2)

**Goal**: Each non-PASS decision posts a deduped PR comment so a human can see why the card stalled without reading coordinare logs. (FR-009, SC-004)

**Independent Test**: Force two bounce decisions on the same HEAD with the same failure set; verify exactly one PR comment is posted. Force a bounce on a different HEAD or different failure set; verify a new comment is posted.

### Tests for User Story 4 (write FIRST)

- [X] T052 [P] [US4] In `tests/unit/graph/nodes/test_notify_ci_gate_rollup.py`, add `test_ci_gate_rollup_dedup_signature` — two decisions with identical `(head_sha, verdict, sorted(required_checks), sorted(failed_check_names))` produce the same signature; any change produces a different signature
- [X] T053 [P] [US4] In `tests/unit/graph/nodes/test_notify_ci_gate_rollup.py`, add `test_ci_gate_comment_dedup` — when a comment with the dedup marker `<!-- coordinare:ci-gate:{sig} -->` already exists on the PR, do NOT post again; when none exists, post one
- [X] T054 [P] [US4] In `tests/unit/graph/nodes/test_notify_ci_gate_rollup.py`, add `test_ci_gate_comment_per_verdict` — PASS does NOT post; HOLD / BOUNCE / ESCALATE DO post (PASS is the silent default)

### Implementation for User Story 4

- [X] T055 [US4] In `src/coordinare/graph/nodes/notify.py`, add `_ci_gate_signature(decision: CIGateDecision) -> str` — sha256 of `f"{head_sha}|{verdict}|{','.join(required_checks)}|{','.join(c['name'] for c in failed_checks)}"`, truncated to 16 chars (mirrors `_persona_scope_signature` from 074)
- [X] T056 [US4] In `notify.py`, add `_render_ci_gate_comment(decision)` producing the markdown template from `contracts/gate-decision.md` §3 (with HTML dedup marker)
- [X] T057 [US4] In `notify.py`'s main notify flow, after a non-PASS gate decision is observed on the session, fetch PR comments, scan for `<!-- coordinare:ci-gate:{sig} -->`, skip-if-present else post
- [X] T058 [US4] Have `monitor_performer.py` set `session["latest_ci_gate_decision"] = decision` so `notify.py` can pick it up on the next cycle (one-cycle delay is acceptable — matches 074's notify pattern)
- [X] T059 [US4] Verify T052–T054 pass

**Checkpoint**: PR comment trail tells the human story of every stall. No spam on repeated identical decisions.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T060 [P] Write integration test `tests/integration/test_implementer_ci_gate_e2e.py` (replace T005 placeholder) walking the full path: card with failing PR → implementer terminal → gate BOUNCE → relay_feedback delivered → implementer pushes fix → CI green → gate PASS → advance to reviewing
- [X] T061 [P] Write integration test variant: pending CI → HOLD → eventually green → PASS (US2)
- [X] T062 [P] Write integration test variant: bounce loop reaches `max_bounces_per_head` → ESCALATE to `needs_human_review` (US1 escalate path)
- [X] T063 [P] Update `AGENTS.md` footer with one-line 075 entry pointing at the gate evaluation in `monitor_performer.py` and the resolver fallback ladder
- [X] T064 [P] Add `event=ci_gate.api_error` rate-limited warning emission in `_evaluate_ci_gate`'s exception path (FR-011 sustained-error warning, matches 074's pattern)
- [X] T065 Run `.venv/bin/ruff check src/coordinare/services/required_checks_resolver.py src/coordinare/graph/nodes/monitor_performer.py src/coordinare/graph/nodes/notify.py src/coordinare/config.py src/coordinare/session.py src/coordinare/state_store.py` and resolve any findings
- [X] T066 Run the full `.venv/bin/pytest` suite; verify zero regressions across 064/070/074 tests
- [ ] T067 Walk through `specs/075-implementer-ci-gate/quickstart.md` end-to-end on a scratch repo (Option A config); verify the documented behavior matches actual behavior; correct any drift in quickstart.md
- [X] T068 Self-review the diff against `specs/074-persona-scope-tiering`'s [074] commit messages for style consistency; ensure no `# removed for X` comments, no follow-up language, no dead code (per `feedback_no_followups.md`)
- [X] T069 [P] Add lightweight timing assertion in `tests/integration/test_implementer_ci_gate_e2e.py`: mock GraphQL latency, assert `_evaluate_ci_gate` returns within 3s wall-clock (SC-002 p95 budget)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No prereqs.
- **Phase 2 (Foundational)**: Depends on Phase 1. **Blocks all user stories.** T013/T014/T015 may run in parallel with T008–T012 (different files); T016/T017 may run in parallel with both groups.
- **Phase 3 (US1, MVP)**: Depends on Phase 2 complete. Independently shippable.
- **Phase 4 (US2)**: Depends on Phase 2 + US1's gate-eval scaffolding (T028–T031). Can begin once US1's structure exists, even before US1 finishes T032.
- **Phase 5 (US3)**: Depends on Phase 2 + US1's gate-eval scaffolding. Independent of US2.
- **Phase 6 (US4)**: Depends on Phase 2 + US1 decisions being emitted to the session. Independent of US2 / US3.
- **Phase 7 (Polish)**: Depends on US1–US4 complete (integration tests need the full path).

### Within Each User Story

- Tests FIRST, must fail before implementation begins (Constitution Principle II — NON-NEGOTIABLE).
- Within a story, [P]-marked tests can run in parallel (different files).
- Implementation tasks within a story are mostly sequential (same files — `monitor_performer.py`, `required_checks_resolver.py`).

### Parallel Opportunities

- **Phase 1**: T001–T005 all [P].
- **Phase 2**: T007 [P]; T014/T015 [P]; T017 [P]; others mostly touch shared files (config.py, session.py, monitor_performer.py).
- **Phase 3 tests**: T018–T025 all [P] (different test functions / files).
- **Phase 5 tests**: T040–T046 all [P].
- **Phase 6 tests**: T052–T054 all [P].
- **Phase 7**: T060–T064 [P].
- Across stories: US2, US3, US4 can be staffed in parallel once US1's structural scaffolding lands (T028 + T029).

---

## Parallel Example: User Story 1 tests

```bash
# Launch all US1 test stubs together:
Task: "test_gate_bounces_on_failing_required_check in tests/unit/graph/nodes/test_monitor_performer_ci_gate.py"
Task: "test_gate_pass_on_all_green in tests/unit/graph/nodes/test_monitor_performer_ci_gate.py"
Task: "test_gate_escalates_at_bounce_limit in tests/unit/graph/nodes/test_monitor_performer_ci_gate.py"
Task: "test_counter_resets_on_new_head in tests/unit/graph/nodes/test_monitor_performer_ci_gate.py"
Task: "test_gate_fails_open_on_exception in tests/unit/graph/nodes/test_monitor_performer_ci_gate.py"
Task: "test_gate_noop_when_disabled in tests/unit/graph/nodes/test_monitor_performer_ci_gate.py"
Task: "CIGateDecision JSON shape in tests/contract/test_gate_decision_schema.py"
Task: "relay_feedback entry shape in tests/contract/test_gate_decision_schema.py"
```

---

## Implementation Strategy

### MVP First (US1 only)

1. Complete Phase 1 (Setup).
2. Complete Phase 2 (Foundational) — non-negotiable.
3. Complete Phase 3 (US1).
4. **STOP and VALIDATE**: red CI bounces, green CI advances, bounce limit escalates. Resolver is layer-3-only (all checks on HEAD) which is the conservative default — operators get FR-001 / FR-003 / FR-008 / FR-015 immediately.
5. Ship US1 alone as the first PR if desired.

### Incremental Delivery

1. US1 → ship MVP (red bounces, green advances, all checks gated).
2. + US2 → ship pending-aware gate (no more racing CI).
3. + US3 → ship 074-aware narrowing (docs-only cards stop waiting for integration tests).
4. + US4 → ship PR comment audit trail.
5. Polish phase finalizes.

### Parallel Team Strategy

Once Phase 2 is done and US1's gate-eval block (T028–T031) exists:

- Dev A: finishes US1 (T026–T032).
- Dev B: starts US2 (T033–T039) on the existing gate-eval scaffolding.
- Dev C: starts US3 (T040–T051) on resolver layers 1 + 2.
- Dev D: starts US4 (T052–T059) on the notify rollup.

All four converge into Phase 7 polish.

---

## Notes

- Constitution Principle II is **NON-NEGOTIABLE**: tests written + failing before implementation, for every slice.
- `_SESSION_FIELDS` round-trip test (T011) is **non-negotiable** — this is the exact drift pattern that bit us four times on 073 and once on 074.
- Gate **fails open** on every exception path (FR-011) — never block the pipeline on the gate's own failure.
- `decide()` extension (T013) **must not** change behavior for existing 064 callers (T014 regression test enforces this).
- No comments in production code beyond what `feedback_no_followups.md` permits.
- No follow-up TODOs — fix issues in the same PR.
