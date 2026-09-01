# Tasks: QA Verdict Integrity & Performer Environment Reliability

**Input**: Design documents from `/specs/088-qa-verdict-integrity/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: TDD is MANDATORY for this feature (constitution Testing Discipline + plan: "failing test first for every behavior change"). Every implementation task is preceded by a test task that must be watched RED before the fix turns it GREEN.

**Organization**: Tasks are grouped by user story (US1–US6 from spec.md). US3 was implemented early on merged PR #111 — its tasks are pre-marked `[X]` and must not be regenerated.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1–US6)
- Include exact file paths in descriptions

## Path Conventions

- Coordinare package: `src/coordinare/`, tests in `tests/unit/`
- Performer package: `agent/performer/src/performer/`, tests in `agent/performer/tests/unit/`
- Run coordinare tests: `.venv/bin/pytest tests/unit/... -q`; performer tests: `.venv/bin/pytest agent/performer/tests/unit/... -q`; lint: `.venv/bin/ruff check src tests agent`

---

## Phase 1: Setup

**Purpose**: Confirm a green baseline so every subsequent RED is attributable to the new test, not pre-existing drift.

- [X] T001 Verify baseline gates pass on branch `088-qa-verdict-integrity` (rebased on main `08bcffb`): `.venv/bin/ruff check src tests agent`, `.venv/bin/pytest --cov=coordinare --cov-fail-under=90 -q`, `.venv/bin/pytest agent/performer/tests/ -q --ignore=agent/performer/tests/integration` — record the baseline coverage number for the Polish-phase comparison *(baseline: ruff clean; coordinare 4128 passed, coverage **90.60%**; performer 1124 passed)*

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: None required. All changes are surgical edits at audited file:line sites (plan.md Structure Decision); the only shared prerequisite (`_env_policy.py`) already merged via PR #111. The cross-boundary `qa_env_blocked` status registration is owned by US1 as its first consumer.

*(no tasks)*

---

## Phase 3: User Story 1 — A QA pass always rests on evidence (Priority: P1) 🎯 MVP

**Goal**: A QA run that executed nothing can never post an unflagged clean PASS (the PR #159 false-pass). Pass claims require ≥1 evidence item; env-limited zero-evidence pass claims become the new terminal status `qa_env_blocked`; visual criteria require app-boot proof; PR comments never render dead screenshot links. Coordinare holds `qa_env_blocked` cards for env repair instead of advancing or starting fix-feedback.

**Independent Test**: Replay the PR #159 payload (criteria_passed=4, executed_checks=[], new_tests=[], visual_evidence=[], environment_error set) through the performer verdict path → status is `qa_env_blocked`, never `qa_passed` (SC-001). An ordinary evidence-backed pass and an honest fail are unchanged (SC-006).

### Tests for User Story 1 (RED — write first, watch each fail)

- [X] T002 [P] [US1] RED: add PR #159 replay regression test (canonical SC-001 fixture, name it so `-k pr159_replay` selects it) in agent/performer/tests/unit/test_main.py — QA output claiming criteria_passed=4 with all evidence channels empty and environment_error set must classify as `qa_env_blocked`, not `qa_passed`
- [X] T003 [P] [US1] RED: add test in agent/performer/tests/unit/test_main.py that `_qa_unsubstantiated_pass()` no longer early-returns False when env_limited — a pass claim with zero evidence is unsubstantiated regardless of environment_error (FR-001)
- [X] T004 [P] [US1] RED: add cross-validation tests in agent/performer/tests/unit/test_main.py — pass claim + zero evidence + NO environment_error ⇒ existing malformed/unsubstantiated refusal; pass claim + ≥1 executed_check ⇒ `qa_passed`; divergent counts produce the "N claimed / M evidence-backed" annotation in the PR comment body (FR-007)
- [X] T005 [P] [US1] RED: add `app_boot_check` gate tests in agent/performer/tests/unit/test_main.py — card with UI/visual criteria and absent or non-zero-exit `app_boot_check` counts those criteria unverified (folds into the evidence gate); `app_boot_check` must reference an entry in executed_checks; cards without visual criteria are unaffected by a null field (FR-005)
- [X] T006 [P] [US1] RED: add visual-evidence render-filtering tests in agent/performer/tests/unit/test_main.py — only entries whose upload returned a CDN URL render as links in the PR comment; local-path-only/failed-upload entries move to the capture-blockers list with the failure reason (FR-006)
- [X] T007 [P] [US1] RED: add coordinare-side tests in tests/unit/graph/nodes/test_monitor_performer.py — terminal status `qa_env_blocked` is recognized as terminal NON-success (in terminal markers, NOT in TERMINAL_SUCCESS_STATES), holds the card with a structured reason, routes to env re-verify, and never advances the stage nor starts the fix-feedback cycle (FR-002/FR-003)

### Implementation for User Story 1 (GREEN — only after the matching RED is observed)

- [X] T008 [US1] Remove the env_limited early-return from `_qa_unsubstantiated_pass()` and add the derived `evidence_count` (executed_checks + new_tests + validated visual_evidence) so the evidence check always runs, in agent/performer/src/performer/main.py (~810–847) — turns T002/T003/T004 green
- [X] T009 [US1] Add `qa_env_blocked` classification + emission: pass claim + evidence_count==0 + environment_error set ⇒ `qa_env_blocked`; extend status mapping (main.py:1576–1585, 2449–2490) and terminal-state tuples (main.py:2889, 2993) in agent/performer/src/performer/main.py
- [X] T010 [US1] Parse and gate the new `app_boot_check` field ({command, exit_code} | null, must reference executed_checks) and require it via the QA prompt builder when the card has UI/visual criteria, in agent/performer/src/performer/main.py — turns T005 green
- [X] T011 [US1] Filter the PR-comment renderer (main.py:2345–2394): render links only for upload-validated visual_evidence, move failures to "Capture blockers / unpublished artifacts" with reason, and annotate divergent criteria counts ("4 claimed / 2 evidence-backed") in agent/performer/src/performer/main.py — turns T006 green
- [X] T012 [US1] Handle `qa_env_blocked` in coordinare: add to terminal-marker sets (src/coordinare/graph/nodes/monitor_performer.py:67, :2020 — NOT TERMINAL_SUCCESS_STATES at :35) and add the verdict branch (~:2031) that holds the card with a structured reason and routes through the existing env-cache re-verify machinery — turns T007 green
- [X] T013 [US1] Run the US1 suite + full performer/coordinare unit gates and lint changed files: `.venv/bin/pytest agent/performer/tests/ -q --ignore=agent/performer/tests/integration && .venv/bin/pytest tests/unit/graph/nodes/ -q && .venv/bin/ruff check agent src`

**Checkpoint**: SC-001 replay green; honest pass/fail behavior unchanged (SC-006). US1 is independently shippable as the MVP.

---

## Phase 4: User Story 2 — Terminal success respects environment health (Priority: P1)

**Goal**: A terminal success whose status payload carries `env_cache_health_failed` is never advanced silently — the card is held with a structured reason and re-runs the stage after the cache is repaired (FR-004).

**Independent Test**: Feed monitor_performer a terminal-success status payload with `env_cache_health_failed=True` → card does not advance; `mark_runtime_health_failed()` path taken; blocked reason `terminal_success_env_health_failed`. Same payload without the flag advances exactly as today.

### Tests for User Story 2 (RED)

- [X] T014 [US2] RED: add tests in tests/unit/graph/nodes/test_monitor_performer.py — terminal success + `env_cache_health_failed` in the same status payload ⇒ no `_advance_stage()`, `mark_runtime_health_failed()` invoked, card blocked with structured reason `terminal_success_env_health_failed`; control case without the flag advances normally

### Implementation for User Story 2 (GREEN)

- [X] T015 [US2] At the terminal-success branch of src/coordinare/graph/nodes/monitor_performer.py (~:2031), consult `env_cache_health_failed` from the same payload before advancing; on hit, call the existing `mark_runtime_health_failed()` path and block with reason `terminal_success_env_health_failed` — turns T014 green
- [X] T016 [US2] Run `.venv/bin/pytest tests/unit/graph/nodes/test_monitor_performer*.py -q` and `.venv/bin/ruff check src tests`

**Checkpoint**: Tainted successes hold instead of propagating; US1+US2 together close the entire false-advance surface.

---

## Phase 5: User Story 3 — Every backend gets the same environment contract (Priority: P2) ✅ DONE EARLY

**Goal**: One shared env-merge policy (image PATH first, deduped cache PATH appended) used by all 8 backends.

> **Note**: Implemented early on merged PR #111 (main `08bcffb`): `_env_policy.build_subprocess_env()` is now used by all 8 backends with tests, and the performer image was rebuilt with the merged code. Tasks below are recorded for traceability only — do NOT regenerate this work.

- [X] T017 [P] [US3] Create shared env-merge helper `build_subprocess_env(*, cache_env, git_env=None, tool_env=None, extra=None, base_env=None)` in agent/performer/src/performer/backends/_env_policy.py *(done — PR #111)*
- [X] T018 [P] [US3] Unit tests for the helper (layering, PATH append-dedup, SYSTEM_DEFAULT_PATH fallback) in agent/performer/tests/unit/backends/test_env_policy.py *(done — PR #111)*
- [X] T019 [US3] Adopt `_env_policy` in all 8 backends (claude_code, openclaw, opencode, opencode_compat, codex, pi, hermes, junie) deleting per-backend inline PATH logic, in agent/performer/src/performer/backends/*.py *(done — PR #111; fixes hermes/junie full-cache-PATH crash class)*
- [X] T020 [US3] Per-backend conformance tests asserting image-PATH-first + cache-appended subprocess env in agent/performer/tests/unit/backends/test_claude_code.py, test_openclaw_backend.py, test_opencode.py, test_codex.py, test_hermes_backend.py, test_junie.py, agent/performer/tests/unit/test_pi_backend.py *(done — PR #111; SC-002)*

**Checkpoint**: SC-002 already verifiable on main: `grep -rn 'PATH' agent/performer/src/performer/backends/*.py` shows PATH composition only in `_env_policy.py`.

---

## Phase 6: User Story 4 — A failing bootstrap stops burning slots (Priority: P2)

**Goal**: Bootstrap retries are bounded: escalating cooldown (`BOOTSTRAP_RETRY_COOLDOWN_S * 2**attempts`), terminal `bootstrap_exhausted` state at `env_bootstrap_max_attempts` (config, default 3) with exactly one notification, SHA-change reset, and consumer holds that name the exhaustion (FR-009/FR-010).

**Independent Test**: Simulate N consecutive bootstrap failures for one `readme_sha` → dispatch gaps grow ×2 each attempt; at attempt 3 state flips to exhausted, one `circuit_breaker_trip` notification fires, no further dispatch; changing the SHA resets attempts to 0 and re-enables dispatch.

### Tests for User Story 4 (RED)

- [X] T021 [P] [US4] RED: add snapshot round-trip tests in tests/unit/test_state_store.py — `EnvCacheStateSnapshot` persists `bootstrap_attempts` (int, default 0) and `bootstrap_exhausted` (bool, default False); an old snapshot JSON without the fields loads with defaults (no migration)
- [X] T022 [P] [US4] RED: add circuit-breaker tests in tests/unit/test_060_env_cache.py — failure increments attempts; cooldown escalates `BOOTSTRAP_RETRY_COOLDOWN_S * 2**attempts`; `attempts >= env_bootstrap_max_attempts` ⇒ `bootstrap_exhausted=True`, `env_cache.bootstrap_exhausted` event + exactly ONE notification (existing notify service, `circuit_breaker_trip` event type), and dispatch stops; success resets attempts=0/exhausted=False; SHA change resets the budget; consumer-hold detail string names the exhaustion

### Implementation for User Story 4 (GREEN)

- [X] T023 [US4] Add `bootstrap_attempts` / `bootstrap_exhausted` fields to `EnvCacheState` and `EnvCacheStateSnapshot` (snapshot at :162–188) in src/coordinare/state_store.py — turns T021 green
- [X] T024 [US4] Add config knob `env_bootstrap_max_attempts: int = 3` to the coordinare configuration model in src/coordinare/config.py
- [X] T025 [US4] Implement the circuit breaker in the retry path of src/coordinare/services/env_cache.py (~:386–403): escalating cooldown, exhausted transition + one notification via the notify service, dispatch stop, SHA-change/success reset, exhaustion-naming consumer holds — turns T022 green
- [X] T026 [US4] Run `.venv/bin/pytest tests/unit/test_060_env_cache.py tests/unit/test_state_store.py -q` and `.venv/bin/ruff check src tests`

**Checkpoint**: SC-003 — an impossible-runtime spec produces exactly 3 dispatches, one notification, then silence.

---

## Phase 7: User Story 5 — Restarting coordinare doesn't repeat finished work (Priority: P3)

**Goal**: Bootstrap success survives restart: `on_bootstrap_complete()` flushes the snapshot immediately; on restart with persisted success + matching `readme_sha`, coordinare runs the cheap clean verify instead of a full ~13-min bootstrap — verify pass ⇒ ready, verify fail ⇒ full bootstrap (phantom-success protection preserved) (FR-011, SC-004 <2 min).

**Independent Test**: Complete a bootstrap → snapshot → reload state store → `last_bootstrap_succeeded is True` (reproduces the observed 16:16:52-success / 17:55-restart-loaded-False bug). Restart with persisted success + SHA match dispatches a clean verify, never a bootstrap; a failing verify falls back to full bootstrap.

### Tests for User Story 5 (RED)

- [X] T027 [P] [US5] RED: add flush-on-completion test in tests/unit/test_daemon_snapshot_persistence.py — complete a bootstrap (`on_bootstrap_complete(success=True)`), take the snapshot, reload, assert `last_bootstrap_succeeded is True` (diagnoses the save-cadence bug per research R8 part 1)
- [X] T028 [P] [US5] RED: add restart honor-path tests in tests/unit/test_060_env_cache.py — loaded state with `last_bootstrap_succeeded=True` and `readme_sha == current_sha` on a fresh boot triggers the clean-verify path (no `env_cache.bootstrap_dispatched`); verify pass ⇒ cache ready / consumers dispatch; verify fail ⇒ full bootstrap; SHA mismatch ⇒ full bootstrap; existing activate.sh-missing retrigger (env_cache.py:405–433) still fires

### Implementation for User Story 5 (GREEN)

- [X] T029 [US5] Trigger an immediate state-store save from `on_bootstrap_complete()` in src/coordinare/services/env_cache.py — turns T027 green
- [X] T030 [US5] Wire the startup honor path in src/coordinare/daemon.py: persisted success + SHA match + fresh boot ⇒ run the existing `_verify_env_cache_clean` instead of dispatching a bootstrap; pass ⇒ mark ready, fail ⇒ full bootstrap — turns T028 green
- [X] T031 [US5] Run `.venv/bin/pytest tests/unit/test_060_env_cache.py tests/unit/test_daemon_snapshot_persistence.py -q` and `.venv/bin/ruff check src tests`

**Checkpoint**: SC-004 — restart with an intact verified cache resumes consumer dispatch in <2 min with no bootstrap dispatch line in the log.

---

## Phase 8: User Story 6 — Failures are visible where they happen (Priority: P3)

**Goal**: Secret-refresh failures retry once then degrade the session with an attributable reason; services-start failures become error-level structured events that QA can cite as environment blockers; malformed performer JSON is logged with its parse error before becoming a generic error (FR-012/FR-013/FR-014).

**Independent Test**: Kill the token-mint path mid-session ⇒ one immediate retry, then `http_performer.secret_refresh_degraded` (error) + session flagged `secret_refresh_failed_at`; later auth failures attribute "stale credentials (refresh failed at T)". Make services-start.sh exit 1 / time out ⇒ `env_cache.services_start_failed` (error) with script, returncode|timeout, output_tail. Feed malformed job-result JSON ⇒ `http_performer.job_result_malformed_json` (warning) with performer id, parse error, summary[:200].

### Tests for User Story 6 (RED)

- [X] T032 [P] [US6] RED: add secret-refresh tests in tests/unit/services/test_http_performer_token_refresh.py — refresh failure retries once immediately; second failure sets session `secret_refresh_failed_at` and emits `http_performer.secret_refresh_degraded` at error level; session continues (not killed); a single transient failure followed by retry success leaves the session clean
- [X] T033 [P] [US6] RED: add malformed-JSON tests in tests/unit/services/test_http_performer_service.py — unparseable job-result JSON emits `http_performer.job_result_malformed_json` (warning) carrying performer id, parse error, and summary[:200] before the existing generic-error mapping
- [X] T034 [P] [US6] RED: add services-start failure tests in agent/performer/tests/unit/test_workspace.py — timeout and non-zero exit both emit `env_cache.services_start_failed` at ERROR level (currently warning, and nonzero uses a different event name) with script path, returncode|timeout, output_tail (last 500 chars), and the failure string is stored so the QA flow can cite it as an environment blocker (joins `environment_error`)
- [X] T035 [P] [US6] RED: add monitor attribution test in tests/unit/graph/nodes/test_monitor_performer.py — an auth-class failure on a session with `secret_refresh_failed_at` set gets the structured reason "stale credentials (refresh failed at T)" and routes degraded→blocked instead of generic error

### Implementation for User Story 6 (GREEN)

- [X] T036 [US6] Implement retry-once-then-degrade in src/coordinare/services/http_performer_service.py (:418–431): immediate retry on refresh failure, then set the new session field `secret_refresh_failed_at: datetime | None` and emit `http_performer.secret_refresh_degraded` (error) — turns T032 green
- [X] T037 [US6] Add malformed-JSON structured logging in src/coordinare/services/http_performer_service.py (:469–482): `http_performer.job_result_malformed_json` (warning) with performer id, parse error, summary[:200] — turns T033 green
- [X] T038 [US6] Upgrade services-start failure handling in agent/performer/src/performer/workspace.py (~:380–420): unify timeout + non-zero under `env_cache.services_start_failed` at error level with script, returncode|timeout, output_tail (500 chars), and store the failure string for the QA environment-blocker channel — turns T034 green
- [X] T038a [US6] FIX (gap found in live QA, "No visual artifacts captured… PostgreSQL test database cannot be created without a password; server fails to start"): T038 stored the services-start failure but nothing consumed it. Wire `consume_services_start_failure()` into the QA verdict in agent/performer/src/performer/main.py (:2253) so the recorded failure joins `env_error` even when the agent's QA JSON omits it — without this a zero-evidence pass under a Postgres/services-start blocker misclassifies as unsubstantiated `qa_failed` (a code defect) instead of `qa_env_blocked` (a held environment blocker). RED→GREEN test `test_qa_services_start_failure_folds_into_env_blocked` in agent/performer/tests/unit/test_main.py
- [X] T039 [US6] Surface `secret_refresh_failed_at` in src/coordinare/graph/nodes/monitor_performer.py: attribute later auth-class failures to the stale credentials with timestamp and route the session degraded→blocked — turns T035 green
- [X] T040 [US6] Run `.venv/bin/pytest tests/unit/services/ tests/unit/graph/nodes/test_monitor_performer*.py -q`, `.venv/bin/pytest agent/performer/tests/unit/test_workspace.py -q`, `.venv/bin/ruff check src tests agent`

**Checkpoint**: SC-005 — both failure classes produce actionable error-level events in the live log.

---

## Phase 9: Polish & Cross-Cutting Concerns

**Purpose**: Full gates, contract conformance, and live-validation readiness.

- [X] T041 [P] Verify contract registries match the implementation: every field/status in specs/088-qa-verdict-integrity/contracts/qa-evidence-result.md and contracts/env-cache-state.md exists with the documented type/default at the listed sites (main.py, monitor_performer.py, state_store.py)
- [X] T042 [P] Run full CI-mirror gates: `.venv/bin/ruff check src tests agent`; `.venv/bin/pytest --cov=coordinare --cov-report=term-missing --cov-fail-under=90 -q`; `.venv/bin/pytest agent/performer/tests/ -q --ignore=agent/performer/tests/integration` — coverage must not regress vs the T001 baseline
- [X] T043 Execute quickstart.md unit-level validation blocks (Cluster A `-k` selections incl. `pr159_replay`, Cluster B conformance, coordinare-side suite) from specs/088-qa-verdict-integrity/quickstart.md
- [X] T044 Deployment per quickstart.md order: merge + restart coordinare FIRST (qa_env_blocked handling, circuit breaker, restart-resume), then rebuild performer images (`coordinare-performer:base` then `:full`) so new spawns pick up the verdict changes

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — run first
- **Foundational (Phase 2)**: empty — user stories may start immediately after Setup
- **US1 (Phase 3)**: Independent. Performer-side (T002–T011) and coordinare-side (T007, T012) halves touch different packages; T012 depends only on the status name agreed in the contract, not on performer code
- **US2 (Phase 4)**: Independent of US1 in behavior, but shares tests/unit/graph/nodes/test_monitor_performer.py and monitor_performer.py with T007/T012 — run Phase 4 after Phase 3 (or coordinate edits) to avoid same-file conflicts
- **US3 (Phase 5)**: DONE (PR #111) — no execution
- **US4 (Phase 6)**: Independent of US1/US2 (different files: state_store.py, env_cache.py, config.py)
- **US5 (Phase 7)**: Shares env_cache.py + test_060_env_cache.py with US4 — run after US4 (or coordinate)
- **US6 (Phase 8)**: http_performer_service/workspace tasks (T032–T034, T036–T038) independent of everything above; T035/T039 touch monitor_performer.py — run those after US2
- **Polish (Phase 9)**: After all desired user stories

### Within Each User Story (TDD ordering — non-negotiable)

- Every RED test task MUST be written and observed failing before its GREEN implementation task starts
- GREEN tasks in the same file are sequential (T008 → T009 → T010 → T011 all edit main.py)
- The story's gate task (lint + suite) closes the story

### Parallel Opportunities

- All US1 RED tasks T002–T007 are [P] (distinct test concerns; T002–T006 same file but independent test functions — write together in one pass if sequencing matters)
- US4 RED T021 ∥ T022; US6 RED T032 ∥ T033 ∥ T034 ∥ T035
- Cross-story: US4 (Phase 6) can proceed in parallel with US1/US2 (different packages/files); US6's http_performer/workspace half can proceed in parallel with US4/US5

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1 baseline → Phase 3 (US1) complete
2. **STOP and validate**: PR #159 replay green; honest pass/fail unchanged; deploy (coordinare restart + image rebuild) and live-validate SC-001
3. US1 alone closes the false-pass hole that motivated this spec

### Incremental Delivery

1. US1 (P1, MVP) → US2 (P1) — together they close the entire false-advance surface — deploy/demo
2. US4 (P2) → US5 (P3) — env-cache reliability pair (shared files, natural sequence) — deploy/demo
3. US6 (P3) — observability polish — deploy
4. US3 already delivered (PR #111, image rebuilt)

### Deployment note (compatibility)

Coordinare must deploy BEFORE the performer image at every increment: old performers never emit `qa_env_blocked`, and the new coordinare tolerates its absence; the reverse order would surface an unknown status (contracts/qa-evidence-result.md).

---

## Summary

- **Total tasks**: 44 (40 executable now + 4 pre-completed US3 tasks)
- **Per story**: Setup 1 · US1 12 (T002–T013) · US2 3 (T014–T016) · US3 4 done (T017–T020) · US4 6 (T021–T026) · US5 5 (T027–T031) · US6 9 (T032–T040) · Polish 4 (T041–T044)
- **MVP scope**: User Story 1 (Phase 3)
- **TDD**: every behavior change has a RED task preceding its GREEN task
