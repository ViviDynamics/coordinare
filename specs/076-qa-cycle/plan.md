# Implementation Plan: QA Cycle 076

**Branch**: `076-qa-cycle` | **Date**: 2026-05-28 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/076-qa-cycle/spec.md`

## Summary

Close the dispatcher-correctness gap that produced today's live-test incident: coordinare dispatched two performer containers for card #101, lost track of a successful implementer turn that opened PR #148, dispatched yet another implementer after the success, then wedged when that implementer stalled. Five interlocking user stories cover the failure modes — duplicate dispatch / orphan containers, forgotten successful turns, lifecycle stranding, silent branch forking, and board↔state divergence. The technical approach is a defense-in-depth fix: (1) a top-of-`dispatch_performer` in-flight guard backed by a per-`(card_id, performer_stage)` mutex; (2) a startup-time reconciliation pass that enumerates Docker containers, re-adopts matching ones, and reaps orphans; (3) Docker labels on every performer launch so reconciliation can match containers to persisted sessions; (4) deterministic canonical branch names per card that the dispatch path injects into performer input; (5) a per-cycle invariant check that surfaces the wedged state and applies the operator-configured default recovery (release-the-pin); (6) idle-timeout retry bookkeeping bounded to 2 retries per `(card, stage)` per 24 h rolling window; (7) multi-PR detection triggered at dispatch / restart / PR-webhook (not every cycle, to bound GitHub API usage). All five clarifications from the 2026-05-28 session are baked in as concrete design decisions.

## Technical Context

**Language/Version**: Python 3.14 (project minimum: 3.12; production currently on 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x, langgraph, structlog, docker SDK (or subprocess to `docker` CLI), fastapi (dashboard SSE), httpx (performer HTTP transport), pyyaml (config)
**Storage**: JSON snapshot on local disk via `state_store.py` (single-host single-process)
**Testing**: pytest (`.venv/bin/pytest`); ruff for lint
**Target Platform**: macOS/Linux daemon (operator workstation or single VM); Docker daemon co-located on host
**Project Type**: Single Python service (`src/coordinare/...`, `tests/...`)
**Performance Goals**:
- Reconciliation pass: ≤30 s p95, ≤500 ms when zero in-flight cards (SC-002)
- In-flight guard check: ≤5 ms per `dispatch_performer` call (must not add measurable latency to the hot path)
- Wedge-detection invariant: ≤10 ms per poll cycle end
- Multi-PR detection check: ≤2 GitHub API calls per dispatch (one to list PRs by branch prefix, one to confirm)
**Constraints**:
- Daemon restart MUST NOT regress dispatch success rate by more than 1 pp (SC-006)
- Reconciliation pass MUST tolerate a hung Docker daemon (FR-012) without deadlocking coordinare
- No new on-disk state surface (rejected alternative: persistent `_active_jobs`)
- Backward-compatible snapshot schema (existing v6 snapshots must rehydrate cleanly; pre-076 containers without coordinare labels MUST be detected as orphans and reaped, not crash the pass)
**Scale/Scope**:
- 1–5 in-flight cards per symphony, 1 symphony today (configured `max_concurrent_cards: 1` on `website`)
- 8 lifecycle stages possible (assessor/architect/implementer/reviewer/security/qa/tech_writer/closer)
- Maximum 1 host, 1 Docker daemon, 1 coordinare process

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### Principle I — Code Quality First

- **Compliance**: All new modules follow coordinare's existing Python conventions (ruff-clean, type-annotated public interfaces, single-responsibility decomposition). New code lives in `src/coordinare/services/reconciliation.py`, `src/coordinare/services/dispatch_guard.py`, `src/coordinare/services/retry_counter.py`, and additions to `src/coordinare/graph/nodes/dispatch_performer.py` / `daemon.py`. No dead code introduced; no unrelated refactors.
- **Risk**: Adding a startup reconciliation pass touches the daemon's boot path; clarity matters more than cleverness here. Plan: extract the pass into its own module with a single public entry point (`run_startup_reconciliation(state, docker_client) -> ReconciliationReport`).

### Principle II — Testing Discipline (NON-NEGOTIABLE)

- **Compliance**:
  - **Unit tests** for every new public function: `reconciliation._enumerate_containers`, `reconciliation._classify_container`, `reconciliation._adopt`, `reconciliation._reap`, `dispatch_guard.check_inflight`, `dispatch_guard.canonical_branch_name`, `dispatch_guard.compute_title_slug`, `monitor_performer._record_pr_artefacts`, `reconciliation.detect_wedged_state`, `retry_counter.record_idle_timeout`.
  - **Integration tests** simulating today's exact failure modes (FR-014, FR-020, FR-024) with mocked Docker daemon, mocked GitHub service, and a real CoordinareState.
  - **Contract tests** for the Docker label schema, the in-flight guard precondition, the canonical-branch-naming algorithm, and the reconciliation pass's log-event vocabulary.
  - **Coverage**: must not regress; aim for >90 % on new modules.
- **Test-first**: not strictly TDD-required by the spec, but each FR has a paired test before its production code lands.

### Principle III — User Experience Consistency

- **Compliance**:
  - All new log events follow the existing `daemon.*` / `dispatch_performer.*` structlog naming.
  - Dashboard surfacing (FR-027) reuses existing banner/warning components, no new colour or font tokens.
  - Notification dedup (FR-010) is invisible to the operator in the success case (no UI change); divergent state surfaces with the existing "stuck" banner pattern.
- **Risk**: Accessibility — dashboard banner must remain WCAG 2.1 AA compliant. Plan: re-use existing accessibility-validated banner component, do not introduce a new one.

### Principle IV — Performance by Design

- **Compliance**: Every FR has a measurable performance budget in SC-002, SC-006, and the Technical Context above.
- **Measurement**: A benchmark test under `tests/perf/` will validate the reconciliation pass's 30 s budget at 5 in-flight cards. The in-flight guard's ≤5 ms budget will be asserted in a microbenchmark per dispatch.
- **Regression prevention**: CI must run the perf benchmark; a >10 % regression on reconciliation latency must fail the build.
- **Risk**: The per-cycle multi-PR detection FR was the largest perf risk; clarification Q4 already resolved it to dispatch-time + restart + webhook only, keeping the steady-state API budget effectively zero.

### Principle V — Clarity Before Action

- **Compliance**: All five high-impact ambiguities were resolved in `/speckit.clarify` and recorded in spec.md's `## Clarifications` section (Q1–Q5). No `NEEDS CLARIFICATION` markers remain in the spec or this plan.
- **Verification**: `grep -c "NEEDS CLARIFICATION" specs/076-qa-cycle/spec.md` returns 0.

### Quality Gates

All 8 gates from the constitution apply; the relevant ones here are:
- Lint & Format: ruff clean
- Unit + Integration + Contract tests: all must pass before merge
- Coverage: must not regress
- Performance: new perf benchmark must pass
- Code review: required as for all PRs

### Constitution Check Result

✅ **PASS** — no violations, no exceptions requested.

## Project Structure

### Documentation (this feature)

```text
specs/076-qa-cycle/
├── plan.md              # This file
├── spec.md              # Feature spec (already written + clarified)
├── research.md          # Phase 0 output (this command)
├── data-model.md        # Phase 1 output (this command)
├── quickstart.md        # Phase 1 output (this command)
├── contracts/           # Phase 1 output (this command)
│   ├── reconciliation-pass.md
│   ├── in-flight-guard.md
│   ├── canonical-branch.md
│   ├── docker-labels.md
│   └── notification-dedup.md
├── checklists/
│   └── requirements.md  # Spec quality checklist (already written)
└── tasks.md             # Phase 2 output (/speckit.tasks command)
```

### Source Code (repository root)

This is a single-project Python service. Existing tree (relevant slices only):

```text
src/coordinare/
├── daemon.py                      # MODIFIED: add startup reconciliation hook + wedge invariant
├── graph/nodes/
│   ├── dispatch_performer.py      # MODIFIED: in-flight guard + canonical branch resolution
│   ├── monitor_performer.py       # MODIFIED: PR-artefact recording + retry-counter on idle-timeout + relay drain
│   ├── check_board.py             # MODIFIED: stale-session re-dispatch → reconciliation call
│   └── notify.py                  # MODIFIED: notification dedup hooks
├── services/
│   ├── reconciliation.py          # NEW: startup pass + per-cycle wedge invariant
│   ├── dispatch_guard.py          # NEW: in-flight guard + canonical branch name + slug algorithm
│   ├── http_performer_service.py  # MODIFIED: re-adoption support + label-tagged container launch
│   ├── github.py                  # MODIFIED: multi-PR detection query
│   └── retry_counter.py           # NEW: persistent per-(card, stage) idle-timeout retry counter
├── session.py                     # MODIFIED: new _SESSION_FIELDS for retry_counter, pr_artefacts_recorded_at
├── state_store.py                 # MODIFIED: schema v6 → v7 migration (new fields)
└── config.py                      # MODIFIED: new tunables for retry budget, drain budget, reconciliation budget

tests/
├── unit/
│   ├── services/
│   │   ├── test_reconciliation.py            # NEW
│   │   ├── test_dispatch_guard.py            # NEW
│   │   ├── test_retry_counter.py             # NEW
│   │   └── test_canonical_branch_naming.py   # NEW
│   ├── graph/nodes/
│   │   ├── test_dispatch_performer_inflight_guard.py   # NEW
│   │   ├── test_monitor_performer_pr_artefacts.py      # NEW
│   │   ├── test_monitor_performer_idle_timeout.py      # NEW
│   │   └── test_check_board_reconciliation.py          # NEW
│   ├── test_daemon_wedge_invariant.py        # NEW
│   └── test_session_076_round_trips.py       # NEW
├── contract/
│   ├── test_docker_label_schema.py           # NEW
│   ├── test_inflight_guard_contract.py       # NEW
│   ├── test_canonical_branch_contract.py     # NEW
│   └── test_reconciliation_log_events.py     # NEW
├── integration/
│   ├── test_restart_no_duplicate_dispatch.py # NEW (FR-014 regression)
│   ├── test_wedged_state_recovery.py         # NEW (FR-020 regression)
│   ├── test_pr_artefacts_round_trip.py       # NEW (FR-015 regression)
│   └── test_multi_pr_divergence_blocks.py    # NEW (FR-024 regression)
└── perf/
    └── test_reconciliation_latency.py        # NEW (SC-002 budget)
```

**Structure Decision**: Single-project Python service. New code clusters in `src/coordinare/services/` (reconciliation, dispatch_guard, retry_counter) so the graph nodes stay focused on flow control while the new mechanics live in dedicated modules.

## Phase 0 — Research Outputs

See [research.md](./research.md) for full content. Key questions resolved:

1. **Docker labelling mechanism** — `docker run --label k=v` flags pass through the existing performer-launch path in `http_performer_service.py`; no Docker SDK dependency change required. Labels are queryable via `docker ps --filter label=coordinare.session_id=…`.
2. **`_active_jobs` lifecycle** — the dict lives on `HTTPPerformerService` instance; populated on `dispatch_card`, cleared on `_cleanup_ephemeral_job`. Re-adoption registers an entry with the same shape from a freshly-constructed `PerformerHTTPClient` pointed at the existing container's host port.
3. **Snapshot schema migration** — extending `_SESSION_FIELDS` is backward-compatible (new fields default to empty); incrementing schema version from v6 to v7 ensures explicit migration path. Old snapshots load with empty retry counters, no behaviour difference.
4. **GitHub API budget** — the existing GraphQL queries on the website repo consume ~5–10 calls per poll cycle. Adding ≤2 calls per dispatch (rare event, ~1–3 per hour in steady state) is well within budget.
5. **Idle-timeout signal source** — `claude code reader idle timeout` is logged by the performer container; coordinare receives the signal via the JSON status emitted on job completion (the performer returns a structured exit code / status). No new transport needed.
6. **Per-card mutex implementation** — `asyncio.Lock` keyed on `(card_id, performer_stage)` in a module-level `defaultdict(lambda: asyncio.Lock())` on `dispatch_performer.py`. No new dependency.
7. **Wedge invariant placement** — runs in `daemon.py` post-cycle as a finally-block to ensure it executes even if the cycle raises. Surfaces via structured `daemon.wedge_*` events.

## Phase 1 — Design & Contracts

### data-model.md (summary; full content in file)

Entities, fields, validations, and state transitions for:
- **PerformerContainerLabels** — the Docker label schema: `coordinare.session_id`, `coordinare.card_id`, `coordinare.performer_stage`, `coordinare.daemon_started_at`, `coordinare.spec_version=076`.
- **ReconciliationDecision** — enum: `adopted | reaped_and_replaced | fresh_dispatched | skipped_persistent | orphan_swept`.
- **WedgeResolution** — enum: `released | blocked | op_override`.
- **IdleTimeoutRetryRecord** — `(card_id, performer_stage, window_start_at, attempt_count, last_at)`; persisted in session.
- **MultiPRDivergence** — record surfaced in state when FR-024 detects >1 PR for a card; includes both PR numbers + canonical pattern + chosen response.
- **CanonicalBranchName** — value object: `card_node_id`, `title_slug`, `full_name`. The slug algorithm is deterministic, idempotent, and bounded ≤60 chars.

### contracts/ (summary; full content in files)

| Contract | What it pins |
|---|---|
| `reconciliation-pass.md` | Public API of `run_startup_reconciliation`, the per-card decision algorithm, the 30 s budget, the 5 log-event names |
| `in-flight-guard.md` | Precondition contract for `dispatch_performer`, the per-card mutex, the refusal log event |
| `canonical-branch.md` | The deterministic slug algorithm (steps a-d from clarification Q2), test vectors for stability |
| `docker-labels.md` | The 5 labels every performer container MUST carry, label-vs-snapshot match rules |
| `notification-dedup.md` | Which reconciliation outcomes do/don't emit `card_dispatched`, the dedup key |

### quickstart.md (summary; full content in file)

Operator's view of the fix: before/after behaviour for each of the 7 anomalies, how to verify the fix in a live test (the exact `docker ps` + `curl /api/symphonies/website` + `grep` recipe), and a section on what changed in the snapshot schema so operators tailing logs can recognise the new events.

### Agent context update

Run `.specify/scripts/bash/update-agent-context.sh claude` to refresh `AGENTS.md` with:
- New module locations (`src/coordinare/services/reconciliation.py`, `…/dispatch_guard.py`, `…/retry_counter.py`)
- New structlog event vocabulary (`daemon.reconciliation_pass_*`, `dispatch_performer.in_flight_guard_*`, etc.)
- New config tunables (idle-timeout retry budget, reconciliation budget, drain budget)

## Re-Evaluation of Constitution Check (post-design)

- **Principle I**: Module decomposition preserves single-responsibility. ✅
- **Principle II**: Test plan covers every new public function and every FR. ✅
- **Principle III**: No UX regressions; dashboard surfacing reuses existing components. ✅
- **Principle IV**: Performance budgets are measurable and have a CI benchmark. ✅
- **Principle V**: Zero NEEDS CLARIFICATION markers remain. ✅

✅ **PASS — design is constitutionally compliant.**

## Complexity Tracking

No constitution violations. Table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |

## Post-Implementation Hardening (Phase 9)

The original 99-task plan shipped all 5 user stories. PR #94 review
rounds (R1–R6) and three 2026-05-29 live tests then surfaced 17 follow-up
fixes, tracked as Phase 9 (T160–T176) in `tasks.md`. The most instructive
are called out below because they are lessons for future specs, not just bugs:

1. **T170 — the idle-timeout detector matched the wrong string
   (CRITICAL, live-only).** Every 076 unit test exercised the FR-019
   retry counter with a synthetic `reason="…idle timeout…"` that
   happened to match the detector's substring. The production
   claude_code backend emits `"claude CLI idle for {N}s with no
   terminal event"` — no `"idle timeout"` substring — so the headline
   US3 retry counter never engaged in reality; the qwen-stall fell
   through to the pre-076 blocked path. **Lesson:** contract tests
   for cross-process error/status strings must assert against the
   *producer's actual output*, not a hand-written stand-in. The fix
   added regression tests using the exact backend strings.

2. **T166 / T167 — snapshot + config forward-compat (HIGH).** A
   field-shape change (`wedge_count_window` flat-list → dict) and a
   new top-level config key (`dispatcher_dedup`) each blocked daemon
   startup until a coercing validator / allowlist entry was added.
   **Lesson:** any new persisted-snapshot field or top-level config
   key needs a load-time compatibility path from day one, validated
   by a "load an old-shape blob" test.

3. **T172 / T175 — cold-path coverage (CRITICAL, live-only).** The
   third 2026-05-29 live test re-ran the architect QA on a pristine
   card with an **empty env cache** — a code path every prior live run
   skipped by reusing a warm cache. It immediately exposed two
   deadlock/thrash bugs: (T172) the new Docker-label code emitted an
   empty-string `coordinare.card_id` for the card-less env_bootstrap
   performer, which failed label validation, never started the
   container, yet left `serialize_env_bootstrap` holding every dispatch
   forever; and (T175) the env_bootstrap's best-effort `service_inference`
   probe timing out at 600 s on slow qwen returned `status="error"`,
   which coordinare read as bootstrap failure and **re-dispatched
   indefinitely** (3 attempts, 0 successes). **Lesson:** "it works in
   live testing" can mean "it works on the warm path"; cold-start /
   first-run paths (empty cache, no branch, fresh process) need explicit
   exercise, and a secondary best-effort step must never be able to fail
   the primary operation it rides along with. Hardened further in **T177**
   with an idle reap (`bootstrap_idle_timeout_seconds`): the wall-clock budget
   bounds total runtime, but diffing the container's meaningful log output
   (filtering coordinare's own status-poll noise) catches a *hung* bootstrap
   within minutes while leaving a slow-but-progressing one alone.

4. **T176 — resume where you left off (US3 correctness).** Re-adopting
   an IN_PROGRESS card with no durable snapshot stage defaulted to
   `implementing`, silently skipping an assessor/architect that never
   finished. Now the resume stage is derived from completed-stage
   artifacts on the branch (earliest incomplete stage wins). **Lesson:**
   "resume" must reconstruct actual progress from durable evidence, not
   assume a blanket downstream default.

Constitution re-check after Phase 9: still ✅ PASS — all 17 fixes carry
regression tests using production-accurate inputs, coverage held ≥90%,
ruff clean. No new violations introduced; the hardening strengthened
Principle II (testing discipline) by closing the unit-vs-reality gaps
that T170 and the T172–T176 cold-path cascade exposed.
