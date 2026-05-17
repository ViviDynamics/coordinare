# Tasks: Closer PR Checks Gate (064)

**Branch**: `064-closer-pr-checks-gate`
**Source docs**: [spec.md](spec.md), [plan.md](plan.md), [data-model.md](data-model.md), [research.md](research.md), [quickstart.md](quickstart.md), [contracts/](contracts/)

Tests are included — they're called out as `Success Criteria` in spec.md and the closer/gate split is policy-heavy, so unit-testing the pure decider gives high coverage at low cost.

---

## Phase 1 — Setup

- [X] T001 Create branch-scoped module skeleton: empty `src/coordinare/services/pr_checks_service.py` and `src/coordinare/services/pr_checks_policy.py` with module docstrings only
- [X] T002 Create empty test files `tests/unit/services/test_pr_checks_policy.py` and `tests/unit/services/test_pr_checks_service.py`
- [X] T003 [P] Add `httpx`-based GraphQL helper smoke test (no functional code) confirming the existing GitHub service exports a usable transport — fail loud if not, so later tasks know to add one

---

## Phase 2 — Foundational

These block all user stories. Schemas and config wiring underpin every later task.

- [X] T004 [P] Add `CheckEntry`, `CheckRollup` pydantic models per data-model.md to `src/coordinare/services/pr_checks_service.py`
- [X] T005 [P] Add `GateDecision` pydantic model and `Action = Literal["FORWARD","BOUNCE","HOLD"]` to `src/coordinare/services/pr_checks_policy.py`
- [X] T006 [P] Add `CardChecksState` pydantic model (head_sha, first_observed_at, last_decision, last_polled_at) to `src/coordinare/state.py`; thread `checks_state: CardChecksState | None = None` onto the per-card lifecycle entry
- [X] T007 Add `closer_pr_checks` config block (enabled, pending_timeout_seconds=900, poll_interval_seconds=30, fail_open_on_error=True, treat_unknown_required_as="pass") to `src/coordinare/config.py` matching `contracts/config.schema.json`
- [X] T008 Wire the new config block into the symphony loader so `cfg.symphonies[name].closer_pr_checks` resolves; add a default in `config.yaml` example

---

## Phase 3 — User Story 1 (P1): Closer refuses to approve when required checks are failing

**Goal**: Closer persona consults `statusCheckRollup` and returns `approved=false` with named failing jobs (or `status: "pending_checks"`) before any human-review handoff.

**Independent test**: Run the closer against a fixture PR with a failing required check. JSON verdict must have `approved=false`, `status: "checks_failed"`, `failed_jobs` populated, and the card must route back to `implementing`.

### Implementation

- [X] T009 [P] [US1] Implement `PrChecksService.get_pr_check_rollup(pr_number) -> CheckRollup` in `src/coordinare/services/pr_checks_service.py` using the GraphQL query in `contracts/pr_check_rollup.graphql`; map `statusCheckRollup.contexts.nodes` (both `CheckRun` and `StatusContext` variants) into `CheckEntry` rows; resolve `is_required` against `branchProtectionRules` matched to `baseRefName`
- [X] T010 [US1] In `PrChecksService`, handle the auth-degraded path: if `branchProtectionRules` is missing or errors, set `branch_protection_readable=False` and leave `is_required=False` on every entry (FR-006 fallback handled later in policy)
- [X] T011 [P] [US1] Implement `pr_checks_policy.decide(rollup, *, pending_timeout_seconds, treat_unknown_required_as, now) -> GateDecision` as a pure function applying the FR-002 conclusion→effect mapping from data-model.md; emit `FORWARD` / `BOUNCE` (with `failed_jobs`) / `HOLD` (with `elapsed_seconds`)
- [X] T012 [US1] In `decide()`, apply the `treat_unknown_required_as` fallback when `branch_protection_readable=False`: per FR-006, "pass" treats no checks as required (forward-friendly); "block" treats all non-skipped as required
- [X] T013 [US1] Add `_CLOSER_PR_CHECKS_DIRECTIVE` constant to `src/coordinare/services/persona_service.py` instructing the closer to (a) invoke the rollup fetch, (b) emit `status: "checks_failed"` with `failed_jobs` on refusal, (c) emit `status: "pending_checks"` while pending, (d) include `head_sha` and `checks_url` per `contracts/closer_output.schema.json`
- [X] T014 [US1] Insert the new directive into the closer persona builder (immediately after the existing `_CI_REVIEWER_DIRECTIVE` injection around line 234); leave non-closer personas untouched
- [X] T015 [US1] Extend the closer's JSON output validator (wherever closer verdicts are parsed) to accept the new `status`, `failed_jobs`, `pending_jobs`, `head_sha`, `checks_url` fields per `contracts/closer_output.schema.json`; treat unknown `status` values as `checks_failed` so an LLM typo errs safe

### Tests

- [X] T016 [P] [US1] In `tests/unit/services/test_pr_checks_policy.py`, cover the FR-002 conclusion mapping table exhaustively: success/neutral/skipped pass; failure/cancelled/timed_out/action_required/stale/startup_failure bounce; mixed required+non-required; empty checks list
- [X] T017 [P] [US1] In `tests/unit/services/test_pr_checks_policy.py`, cover branch-protection-unreadable fallback for both `treat_unknown_required_as` values
- [X] T018 [P] [US1] In `tests/unit/services/test_pr_checks_service.py`, mock the GraphQL response and assert `CheckRollup` shape, `is_required` resolution, and the degraded-auth path
- [X] T019 [US1] In `tests/unit/services/test_persona_service.py`, assert the closer persona text contains the new directive markers (rollup-query instruction, status enum values, failed_jobs field) and that other personas do NOT contain it

---

## Phase 4 — User Story 2 (P1): Coordinare-side gate catches a closer that approves a red PR

**Goal**: `_advance_stage` re-queries the rollup before transitioning to `monitoring_pr` and blocks if required checks are not green.

**Independent test**: Inject a closer verdict with `approved=true` for a PR whose required checks include a `failure`. Coordinare must NOT transition `phase` to `monitoring_pr`; must route back to `implementing` with the failing job names in `relay_feedback`.

### Implementation

- [X] T020 [US2] In `src/coordinare/graph/nodes/monitor_performer.py`, immediately after the 043 lint-gate block (~line 426–476) and **before** `phase = "monitoring_pr"`, call `PrChecksService.get_pr_check_rollup(pr_number)` and `pr_checks_policy.decide(...)` using the symphony's `closer_pr_checks` config
- [X] T021 [US2] Map `GateDecision` to lifecycle outcomes: `FORWARD` → continue to `monitoring_pr` as today; `BOUNCE` → route to `implementing`, populate `relay_feedback` with the failed jobs (and `pending_timeout` reason when elapsed exceeds budget); `HOLD` → leave `phase` unchanged, persist `CardChecksState`, log the hold
- [X] T022 [US2] Wrap the gate call in a fail-open try/except per FR-007 and `fail_open_on_error` config: on GraphQL/network exceptions, log WARN with `closer.pr_checks.gate_error` and treat as FORWARD; mirror the 043 lint-gate logging style
- [X] T023 [US2] On `BOUNCE`, format `relay_feedback` so the next implementer sees: failing job names, the `details_url` for each, and the closer's prior comments — the implementer must not need to re-query GitHub
- [X] T024 [US2] Add structured logs at the gate boundary: `closer.pr_checks.decision` with action/pr/head_sha/elapsed/failed/pending fields per quickstart.md

### Tests

- [X] T025 [P] [US2] In `tests/unit/graph/nodes/test_monitor_performer.py`, add a test where the closer returned `approved=true` but the gate's mocked rollup contains a required `failure` — assert `phase` stays out of `monitoring_pr` and the card routes to `implementing`
- [X] T026 [P] [US2] In the same file, add a test where the closer returned `approved=true` and the rollup is all `success` — assert `phase` transitions to `monitoring_pr`
- [X] T027 [P] [US2] Add a test that the GraphQL call raising an exception results in FORWARD when `fail_open_on_error=true`, and HOLD when set to false (if we expose that mode — otherwise just FORWARD)
- [X] T028 [P] [US2] Add a test that `relay_feedback` on BOUNCE contains each failed job name and its `details_url`

---

## Phase 5 — User Story 3 (P2): Bounded wait for pending checks

**Goal**: When required checks are still pending, the coordinare holds the card (HTTP-only re-poll, no closer re-dispatch) for up to `pending_timeout_seconds` anchored to PR HEAD push time, then bounces if not resolved.

**Independent test**: PR whose required checks stay `pending` past the configured timeout — card transitions to `implementing` with the timeout reason, NOT to `monitoring_pr`. Closer is NOT re-invoked during the hold window.

### Implementation

- [X] T029 [US3] In `monitor_performer.py`, add the tick fast-path: if the card's persisted `CardChecksState.head_sha` matches live HEAD AND `last_polled_at` is within `poll_interval_seconds`, reuse the prior decision without a GraphQL call
- [X] T030 [US3] On each gate evaluation, refresh `CardChecksState` (head_sha, last_decision, last_polled_at); reset `first_observed_at` whenever `head_sha` changes so the timeout clock follows the latest push (FR-009)
- [X] T031 [US3] In `pr_checks_policy.decide()`, compute `elapsed_seconds = (now - rollup.head_pushed_at).total_seconds()`; when any required check is pending AND elapsed > `pending_timeout_seconds`, return `BOUNCE` with `reason="pending_timeout"` and an empty `failed_jobs` list (the bounce reason is informational, not job-named)
- [X] T032 [US3] On a HOLD decision, ensure the coordinare does NOT re-dispatch the closer — only re-polls the rollup on the next tick (FR-005); guard the closer dispatcher with a `phase == closer && pending_checks` check that short-circuits

### Tests

- [X] T033 [P] [US3] In `tests/unit/services/test_pr_checks_policy.py`, add timeout tests: required check pending + elapsed < timeout → HOLD; pending + elapsed > timeout → BOUNCE with `reason="pending_timeout"`
- [X] T034 [P] [US3] In `tests/unit/graph/nodes/test_monitor_performer.py`, add a multi-tick test: first tick HOLDs, second tick (same head, within poll_interval) reuses prior decision without calling the GraphQL mock again; third tick (after interval) re-queries
- [X] T035 [P] [US3] Add a test that pushing a new HEAD (changing `head_sha`) resets `first_observed_at` so the timeout budget restarts
- [X] T036 [P] [US3] Add a test that confirms the closer dispatcher is NOT called while a card sits in `pending_checks` HOLD

---

## Phase 6 — Polish & Cross-Cutting

- [X] T037 [P] Run `.venv/bin/ruff check src/coordinare/services/pr_checks_service.py src/coordinare/services/pr_checks_policy.py src/coordinare/graph/nodes/monitor_performer.py src/coordinare/services/persona_service.py` and fix all findings
- [X] T038 [P] Run `.venv/bin/pytest tests/unit/services/test_pr_checks_policy.py tests/unit/services/test_pr_checks_service.py tests/unit/services/test_persona_service.py tests/unit/graph/nodes/test_monitor_performer.py` — all green
- [X] T039 [P] Add an entry to `MEMORY.md` index pointing at the new policy/service split if it constitutes a non-obvious architectural pattern worth recalling (skip if obvious from code)
- [X] T040 [P] Update `AGENTS.md` (or equivalent dev-guideline doc) with a one-line pointer to spec 064 for future contributors touching closer or `_advance_stage`
- [X] T041 Manual end-to-end smoke against a real fixture PR per `quickstart.md`: trigger the closer, observe `closer.pr_checks.decision` log line with each of FORWARD / HOLD / BOUNCE paths
- [X] T042 Verify rollback path: set `closer_pr_checks.enabled=false` in config, restart coordinare, confirm closer reverts to pre-064 behaviour (no rollup query, no gate)
- [X] T043 Measure gate latency against a real PR with ~20 check entries; record p95 in PR description. Fail the budget check if > 500ms (per Performance Budgets in spec.md). Use the structured `closer.pr_checks.decision` log's elapsed field as the measurement.
- [X] T044 Verify per-tick HOLD cost budget: log inspection over a 5-minute HOLD window shows ≤1 GraphQL call per `poll_interval_seconds`, 0 closer LLM dispatches. Failing budget blocks merge.

---

## Dependencies

```
Phase 1 (Setup)            ──┐
Phase 2 (Foundational)     ──┤── must complete before Phase 3+
                             │
Phase 3 (US1) ────────────┐  │
Phase 4 (US2) ────────────┤  │── US1 and US2 are both P1 and may run in
Phase 5 (US3) ───┐        │  │   parallel after Foundational lands.
                 │        │  │   US3 depends on US2's gate plumbing
                 └────────┘  │   being in place (`_advance_stage` hook).
Phase 6 (Polish) ─────────────── after all stories
```

Within a phase, tasks marked `[P]` may run in parallel; unmarked tasks within the same phase have ordering dependencies inferred from file/line overlap.

## Parallel Execution Examples

- **Phase 2**: T004, T005, T006 are independent (different files) — launch together. T007/T008 are sequential (T008 reads T007's schema).
- **US1**: T009, T011 are independent (service vs policy file). T016/T017/T018 tests are all `[P]`.
- **US2**: All four test tasks (T025–T028) parallel.
- **US3**: All four test tasks (T033–T036) parallel.
- **Polish**: T037–T040 parallel; T041/T042 sequential and manual.

## MVP Scope

**MVP = US1 only**. The closer persona alone — with no coordinare-side gate — already closes the visible failure mode for ~all real cases (the closer is the bot that immediately precedes human review). US2 is defence-in-depth; US3 is a UX refinement. Ship US1 to main, observe one symphony cycle, then layer US2 and US3.

## Format Validation

All 44 tasks above use the strict format: `- [ ] TXXX [P?] [USx?] description with file path`. Setup/Foundational/Polish tasks omit `[USx]` per the rules. User-story tasks all carry a `[US1]`/`[US2]`/`[US3]` label.
