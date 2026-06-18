# Tasks: Env-Cache Toolchain-Readiness Dispatch Gate

**Input**: Design documents from `/specs/093-env-cache-readiness-gate/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/verify-sh-checklist.md, contracts/readiness-gate.md, quickstart.md

**Tests**: Included. The spec's three user stories each carry an explicit "Independent Test"
section, and the regression guard in quickstart.md pins existing tri-state behavior — so test
tasks are first-class here (TDD: write the test, watch it fail, then implement).

**Organization**: Tasks grouped by user story (US1 P1 → US2 P2 → US3 P3) so each story is an
independently testable increment. This feature introduces **no new persisted state, no new
pydantic model, no new dependency, no new graph node** — it is a behavioral extension over
existing, named seams (see plan.md / research.md).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: US1 / US2 / US3 maps to the spec's user stories
- All paths are repo-relative; run tests with `.venv/bin/pytest`, lint with `.venv/bin/ruff check`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Confirm the named seams this feature extends actually exist where the design says,
before any edits. No scaffolding — this feature adds no new files to `src/`.

- [X] T001 Confirm the four extension seams resolve to the documented locations and signatures: the dispatch readiness guard `_current_and_verified` block in `src/coordinare/graph/nodes/dispatch_performer.py:976-1000`, the tri-state `_verify_env_cache_clean` in `src/coordinare/daemon.py:1738`, `render_verify_sh` in `src/coordinare/services/env_manifest.py`, and `EnvCacheService.check_and_trigger` in `src/coordinare/services/env_cache.py:428`. Record any drift from plan.md line references in plan.md before proceeding.
- [X] T002 Confirm the code-running stage set: read `ROLE_TO_STAGE` in `src/coordinare/lifecycle.py:8` and verify the gated stages (qa, implementing, reviewing, security, documenting, architecting, advocate, assessing, closing_review) and the `env_bootstrap` exemption match contracts/readiness-gate.md Applicability table.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Lock down the invariant that the rest of the feature must not break — the
`_verify_env_cache_clean` tri-state contract — and confirm the attempt-budget seam US3 reuses.

**⚠️ CRITICAL**: No user story work begins until the tri-state regression guard is green and the
budget seam is confirmed reusable as-is.

- [X] T003 Read `tests/unit/test_daemon_snapshot_persistence.py` and identify the tests pinning the `_verify_env_cache_clean` tri-state (None when `verify.sh` absent ⇒ MUST NOT block, True on exit 0, False+detail on nonzero). Run them green as the baseline regression guard (data-model.md Entity 3). These MUST stay green through every phase.
- [X] T004 Confirm `env_bootstrap_max_attempts` (`src/coordinare/config.py`, default 3, ge=1, le=20) and the `EnvCacheService.check_and_trigger` / `on_bootstrap_complete` attempt-increment, `bootstrap_exhausted`-at-`>=max`, and spec-sha-change reset behavior are reusable unchanged — **no new counter, no new knob** (data-model.md Entity 4, FR-008). Note the exact method signatures the US3 wiring will call.

**Checkpoint**: Tri-state guard green, budget seam confirmed — user stories may begin.

---

## Phase 3: User Story 1 — Dispatch gated on real toolchain readiness for all code-running stages (Priority: P1) 🎯 MVP

**Goal**: Before dispatching any code-running stage, require a passing readiness run — not
`last_bootstrap_succeeded` alone. `False` ⇒ withhold + release slot; `True` ⇒ proceed; `None`
(degraded) ⇒ MUST NOT block. `env_bootstrap` stays exempt. (FR-001, FR-005, FR-006)

**Independent Test**: For each code-running stage in `ROLE_TO_STAGE`, a cache with
`last_bootstrap_succeeded=True` but readiness `False` withholds dispatch; readiness `True`
proceeds; `None` does not block; `env_bootstrap` is never gated.

### Tests for User Story 1 (write first, must FAIL)

- [X] T005 [P] [US1] Add dispatch-gate decision tests in `tests/unit/graph/nodes/test_dispatch_performer.py` (or a focused `test_dispatch_performer_readiness_gate.py` alongside it): readiness `False` ⇒ dispatch withheld via the existing `_release_slot_on_error()` path and logged under `dispatch_performer.env_cache_not_current`; readiness `True` ⇒ dispatch proceeds; readiness `None` ⇒ dispatch proceeds (degraded passthrough, FR-005). Mock `_verify_env_cache_clean` to return each tri-state.
- [X] T006 [P] [US1] Add a parametrized test over every code-running stage in `ROLE_TO_STAGE` asserting the gate applies (acceptance scenarios 1–2), plus a test that `env_bootstrap` is exempt (scenario 4) and a non-code-running stage is not gated, in the same dispatch test module.
- [X] T007 [P] [US1] Add a regression assertion that the readiness verdict is recomputed each dispatch (no caching on `EnvCacheState`) — two dispatches each invoke `_verify_env_cache_clean` (FR-006, SC-005).

### Implementation for User Story 1

- [X] T008 [US1] In `src/coordinare/graph/nodes/dispatch_performer.py`, after the existing `_current_and_verified` check passes (after line 1000) and for non-`env_bootstrap` code-running stages only, invoke `daemon._verify_env_cache_clean(symphony, svc)` and branch on the tri-state per contracts/readiness-gate.md: `True` ⇒ continue; `False` ⇒ log `dispatch_performer.env_cache_not_current` (with tri-state outcome + captured failing-line detail, keys/paths only) then `_release_slot_on_error()` and `return state`; `None` ⇒ continue (degraded). Reuse the **exact** hold/slot-release machinery the not-current path already uses (single hold point).
- [X] T009 [US1] Ensure the readiness invocation is reachable from the dispatch node's available context (the daemon/service handle and the symphony's env-cache `svc`); thread only existing handles — introduce no new dispatch-payload field (contracts/readiness-gate.md Field Registry, FR-001).
- [X] T010 [US1] Emit the readiness decision via structlog with env-var NAMES and file PATHS only — symphony, spec sha, tri-state outcome, and on `False` the captured failing-line detail (FR-009). Run T005–T007 green; run `.venv/bin/ruff check` on the edited file.

**Checkpoint**: Code-running dispatch is gated on a live readiness check; MVP closes the race for any cache whose `verify.sh` already probes the toolchain.

---

## Phase 4: User Story 2 — Readiness is a manifest-driven checklist of present/running/working items (Priority: P2)

**Goal**: `render_verify_sh` emits one OK/FAIL/WARN line per declared manifest item, distinguishing
*installed* from *running/usable*. Net-new: coordinare-managed service (postgres/redis)
RUNNING/healthy at **FAIL** level. Aggregate exit nonzero iff ≥1 FAIL. (FR-002, FR-003, FR-004,
FR-009, FR-010)

**Independent Test**: Render `verify.sh` from a manifest with a pinned runtime, a native
extension, a postgres service, and a qa-only nicety; assert line tokens per kind, the service
line at FAIL when not running, WARN never driving exit, and nonzero exit iff ≥1 FAIL — with no
secret values and all interpolated tokens `shq`-quoted.

### Tests for User Story 2 (write first, must FAIL)

- [X] T011 [P] [US2] In `tests/unit/test_env_manifest.py`, add `render_verify_sh` assertions for the **net-new** coordinare-managed-service line: postgres ⇒ `pg_isready` probe, redis ⇒ `redis-cli ... PING` expecting `PONG`; installed-but-not-running ⇒ `FAIL:` line; running/healthy ⇒ `OK:` line (contracts/verify-sh-checklist.md probe table; acceptance scenario 3).
- [X] T012 [P] [US2] Add/confirm `render_verify_sh` assertions for the unchanged kinds: runtime version-match ⇒ `OK`, mismatch ⇒ `FAIL` (scenario 1); native-extension boot smoke (`bundle exec ruby -e "require ..."`) loads ⇒ `OK`, raises ⇒ `FAIL` (scenario 2); `system`/`node_pkg` nicety absent ⇒ `WARN` and never `FAIL` (scenario 4); aggregate exit nonzero iff ≥1 `FAIL` (FR-004).
- [X] T013 [P] [US2] Add an invariant test: no rendered line contains a literal secret value, every interpolated name/path passes through the `shq` filter, and any service health probe needing a password uses the redacted `--pwfile=<(...)`-style channel rather than echoing it (FR-009, scenario 6).

### Implementation for User Story 2

- [X] T014 [US2] In `src/coordinare/services/env_manifest.py`, extend `render_verify_sh` (and its Jinja template) to emit a coordinare-managed-service readiness line for each managed service descriptor (postgres/redis from spec-091), at **FAIL** level when installed-but-not-running — using the service-kind health helpers (~156-174: postgres→`pg_isready`, redis→`PING`). This is the only net-new probe; runtime/gem/native-extension/`WARN` lines stay as-is (data-model.md Entity 2 change table).
- [X] T015 [US2] Keep all toolchain/health probe shell in the generated `verify.sh` — coordinare Python only selects a probe *kind* from `ManifestItem.kind` / the service descriptor; emit no rbenv/nvm/asdf knowledge into Python (FR-010, contracts invariant). Pass every interpolated name/path through `shq` (FR-009).
- [X] T016 [US2] Confirm aggregate exit-code logic stays "nonzero iff ≥1 FAIL emitted; WARN never contributes" (FR-003/FR-004). Run T011–T013 green; `.venv/bin/ruff check` the edited file; re-run T003 tri-state guard (the new FAIL line must still surface as `False`, not change the contract).

**Checkpoint**: `verify.sh` is a full manifest-driven readiness checklist; a postgres-installed-but-not-running cache now yields `False` at the gate.

---

## Phase 5: User Story 3 — Readiness failure self-heals via bootstrap, bounded by attempt budget (Priority: P3)

**Goal**: On a `False` readiness for a code-running dispatch, route through the existing
re-bootstrap (`check_and_trigger`) for the current spec sha and the `bootstrap_in_flight` hold;
bound the loop with `env_bootstrap_max_attempts`; on exhaustion surface the existing
`bootstrap_exhausted` env-blocked verdict. No new counter. (FR-007, FR-008, SC-004)

**Independent Test**: Force readiness `False`; confirm a re-bootstrap is triggered for the current
spec sha and dispatch is held; converge bootstrap ⇒ next dispatch proceeds; force persistent
`False` ⇒ at `>= env_bootstrap_max_attempts` the loop stops with an actionable env-blocked verdict;
spec-sha change resets the budget.

### Tests for User Story 3 (write first, must FAIL)

- [X] T017 [P] [US3] In the dispatch readiness test module, assert that a `False` readiness triggers `EnvCacheService.check_and_trigger` for the **current spec sha** and the dispatch is held under the existing `env_cache_not_current` / `bootstrap_in_flight` path (acceptance scenario 1, FR-007).
- [X] T018 [P] [US3] Add a loop-bound test: persistent `False` increments the existing bootstrap attempt counter (no new counter), and at `>= env_bootstrap_max_attempts` the `bootstrap_exhausted` path fires producing an actionable env-blocked verdict instead of looping (scenario 3, FR-008, SC-004). Assert a spec-sha change resets the budget (scenario reset path).
- [X] T019 [P] [US3] Add a convergence test: after a re-bootstrap makes readiness pass, the next dispatch proceeds normally (scenario 2).

### Implementation for User Story 3

- [X] T020 [US3] In `src/coordinare/graph/nodes/dispatch_performer.py`, on the `False` branch from T008 wire the re-bootstrap trigger through the existing `EnvCacheService.check_and_trigger` for the current spec sha so the dispatch falls into the existing `bootstrap_in_flight` hold (FR-007). Do not duplicate the hold/slot-release logic — reuse the single hold point.
- [X] T021 [US3] Confirm the loop bound is the existing `env_bootstrap_max_attempts` budget and exhaustion routes through the existing `bootstrap_exhausted` / `bootstrap_hold_detail` env-blocked verdict (already logged at `dispatch_performer.env_cache_not_current`); add no readiness-specific counter and no new exhaustion path (FR-008). Run T017–T019 green; `.venv/bin/ruff check`.

**Checkpoint**: The gate is self-healing and bounded — a fixable cache re-bootstraps and proceeds; a genuinely-broken one surfaces an actionable verdict within budget.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T022 Run the spec-088 defense-in-depth regression: confirm `monitor_performer.qa_env_blocked` still fires on a hollow pass (an incomplete manifest that slips a false-OK). 093 closes the realization race; 088 remains the false-OK backstop (contracts/readiness-gate.md Defense-in-depth, quickstart.md regression guard).
- [X] T023 Re-run the full `tests/unit/test_daemon_snapshot_persistence.py` tri-state suite and the dispatch + env_manifest suites together to confirm no cross-phase regression; `.venv/bin/ruff check` all edited files.
- [X] T024 Walk quickstart.md US1/US2/US3 scenarios end-to-end against the implemented behavior; confirm SC-001..SC-006 each have a covering test or scenario, and that observability lines carry keys/paths only (FR-009, SC-006).
- [X] T025 [P] Update `CLAUDE.md` "Recent Changes" only if the agent-context script left it stale; confirm no new Active Technologies were actually introduced (this feature adds none).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately.
- **Foundational (Phase 2)**: Depends on Setup. The T003 tri-state guard BLOCKS all stories.
- **US1 (Phase 3)**: Depends on Foundational. The MVP — closes the dispatch race on its own for any cache whose `verify.sh` already probes the toolchain.
- **US2 (Phase 4)**: Depends on Foundational. Independently valuable (auditable `verify.sh`), but the *net-new service FAIL line* is what makes US1's gate catch the postgres-not-running case. Can be built in parallel with US1 (different file: `env_manifest.py` vs `dispatch_performer.py`).
- **US3 (Phase 5)**: Depends on US1 (extends the `False` branch in `dispatch_performer.py`). Sequenced after US1; orthogonal to US2.
- **Polish (Phase 6)**: Depends on all desired stories.

### Within Each User Story

- Tests (T005–T007, T011–T013, T017–T019) written first and FAIL before implementation.
- US1 and US2 touch different files → fully parallelizable across the two stories.
- US3 implementation (T020/T021) edits the same `dispatch_performer.py` block as US1 (T008) → sequential after US1.

### Parallel Opportunities

- T005, T006, T007 (US1 tests) — parallel (same new test module, independent test fns).
- T011, T012, T013 (US2 tests) — parallel.
- T017, T018, T019 (US3 tests) — parallel.
- **US1 (Phase 3) and US2 (Phase 4) can proceed in parallel** — `dispatch_performer.py` vs `env_manifest.py`, no shared file.

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1 Setup → Phase 2 Foundational (tri-state guard green).
2. Phase 3 US1 → gate code-running dispatch on the live readiness tri-state.
3. **STOP and VALIDATE**: the live website race (QA dispatched before ruby built) cannot recur for any cache whose `verify.sh` probes the runtime — `False`/`None` handled correctly, `env_bootstrap` exempt.

### Incremental Delivery

1. Setup + Foundational → guard green, budget seam confirmed.
2. US1 → dispatch gated (MVP).
3. US2 → manifest-driven checklist + net-new service-running FAIL line (catches postgres-installed-not-running).
4. US3 → self-heal + budget bound (no thrash).
5. Polish → 088 defense-in-depth + quickstart + SC coverage.

---

## Notes

- No new persisted state, no new pydantic model, no new dependency, no new graph node — behavioral extension over existing seams (plan.md Constitution Check, all 5 PASS).
- The `_verify_env_cache_clean` tri-state contract (T003) is the load-bearing invariant — keep it green every phase.
- Toolchain-specific probing lives ONLY in generated `verify.sh` (FR-010); coordinare Python selects a probe kind, never hardcodes a version manager.
- Secret invariant (FR-009): names/paths only in lines, logs, persisted state; passwords via the redacted `--pwfile` channel.
- Commit after each task or logical group; mark `[x]`/`[X]` when complete.
