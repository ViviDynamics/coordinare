# Tasks: BLOCKED-Card Auto-Recovery + QA Visual-Capture Env Resilience

**Feature**: `129-blocked-card-recovery` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

Tests included (constitution NON-NEGOTIABLE; TDD for the pure evaluator). `[P]`=parallelizable.

## Phase 1: Setup

- [x] T001 Green baseline: `env -u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW .venv/bin/pytest tests/unit tests/contract -q` + `ruff check src/coordinare`.

## Phase 2: Foundational

- [x] T002 [P] `EventType.card_auto_recovered` in `src/coordinare/models/notification.py`.
- [ ] T003 [P] Per-card block-reason record + anti-thrash marker on `PersistedSession` (`state_store.py`) + `CardSession`/`_SESSION_FIELDS` (`session.py`) + `CoordinareState`/`initial_state` (`graph/state.py`); schema v16→17 (backward-compat). Update `workflow-snapshot.schema.json` (top-level + per-card) + `schema_version` enum + move the current-version pin to 17.
- [x] T004 [P] TDD tests FIRST `tests/unit/test_blocked_recovery.py`: STILL_BLOCKED vs RECOVER for each reason (stale-review-addressed→IN_REVIEW, env-recovered→prior stage, CI red→green, clarification answered), multi-reason (all must clear), anti-thrash cooldown, safety (unresolved verdict/open clarification never recovers).
- [x] T005 Implement pure `services/blocked_recovery.py` `evaluate_recovery(block_record, signals, config)` → `StillBlocked | Recover(target_stage, reason)` (FR-001..006, reuses spec-128 `classify_review_staleness`) — make T004 pass.

## Phase 3: US1 — BLOCKED-card auto-recovery (P1) 🎯 MVP

- [x] T006 [US1] TDD test `tests/unit/graph/nodes/test_check_board_recovery.py` (mocked github): a recoverable BLOCKED card → moved to the correct column + one `card_auto_recovered` notification + anti-thrash marker set; a still-blocked card → untouched; re-eval error → card stays blocked, cycle continues; operator manual-move not overridden.
- [x] T007 [US1] Implement `_attempt_blocked_card_recovery(state, github, blocked)` in `check_board.py`, hooked at the blocked-list (~L705): gather cleared-signals (review context, CI, issue answers, env-cache health), call the evaluator, `move_card` recovered cards + notify (deduped) + set anti-thrash marker. Fail-safe per card; feature-guarded — make T006 pass.
- [~] T008 [US1] Signal gatherers (fail-safe): review-decision/staleness (spec-128 `get_pr_review_context`) **[done]**; env recovered (env-cache health) **[done — increment 129a]**; CI green (`check_mergeability`) and clarification-answered (issue comments) **[still deferred — no authoritative per-card source signal / ambiguous "answered" detection; recovering on a weak signal risks a wrong unblock]**. Unit-tested with mocks.

## Phase 4: US2 — QA visual-capture env resilience (P2)

- [x] T009 [P] [US2] TDD test `tests/unit/test_qa_verdict_env_capture.py`: capture-tooling-unavailable + app healthy → env_blocked (recoverable), not hard fail; non-visual criteria met + capture unavailable → honest limited-pass (states no visual evidence), never claims absent evidence; app genuinely failed → real fail; ambiguous → fail toward real fail.
- [x] T010 [US2] Extend `qa_verdict.py`: distinguish capture-tooling-unavailable from app failure; route to the recoverable env-blocked outcome + honest limited-pass (FR-010..012, honors spec-120 floor) — make T009 pass.

## Phase 5: Polish

- [ ] T011 [P] Anti-thrash cooldown configurable (safe default); structured logging (`blocked_recovery.*`).
- [x] T012 [P] Operator doc `docs/operators/blocked-card-recovery.md`.
- [ ] T013 Full suite green (unit + **contract**) + coverage not decreased + ruff clean; adversarial review before merge.

## Dependencies

Phase 2 blocks all. US1 (P3) = MVP on foundation. US2 (P4) independent (QA-side) — parallel after Phase 2. Polish last. MVP = US1.

## Notes

- **Run the WHOLE `tests/` tree** (unit + contract) before pushing — spec-128's CI break was a skipped `tests/contract`.
- The `check_board` hook is high-blast-radius: guard + fail-safe + adversarial review (esp. FR-004 human-gate safety, FR-006 anti-thrash, FR-009 no-override-operator).


## Notes (post-build)
- Anti-thrash marker is IN-MEMORY (no schema bump → tests/contract untouched). T003 heavy persistence deferred.
- US1 wiring scoped to the stale-review signal (T008 env/CI/clarification gatherers deferred); evaluator already supports all four.
- Default-OFF (COORDINARE_BLOCKED_RECOVERY); US2 limited-pass advance deferred (HOLD default).

## Notes (post-adversarial-review)
Adversarial review (46 agents) confirmed 4 distinct defects — all fixed on-branch with regression tests:
1. **Stale-snapshot re-adoption** (`check_board.py`): a recovered card stayed in `state["board_snapshot"]["BLOCKED"]`, so the downstream blocked-handling branch re-adopted it as BLOCKED (phase↔GitHub divergence). Fix: prune the recovered card from the snapshot BLOCKED list (in-place, identity-preserving) + the live blocked list after `move_card`.
2. **`browser`/`display`/`capture` keyword over-match** (`qa_verdict.py`, FR-012): bare keywords matched real app-failure text (`"browser console: assertion failed"`) → a real bug hidden behind a recoverable HOLD. Fix: require unambiguous capture-tooling phrases only.
3. **US2 stuck-forever coupling** (`qa_verdict.py`): capture-unavailable→HOLD depends (per US2's own spec) on US1's env-recovery pickup, which is the deferred T008 gatherer — so an enabled HOLD would sit in BLOCKED forever, worse than the prior bounce. Fix: gate the HOLD behind `capture_recovery_enabled` (wired to the same `COORDINARE_BLOCKED_RECOVERY` flag at the monitor call site). Default-OFF ⇒ unchanged bounce behavior, no regression. **Follow-up:** wire T008 env-recovery to re-dispatch capture-unavailable HOLDs before enabling US2 in production.
4. **Hardcoded notification target** (`check_board.py`): payload hardcoded `"IN_REVIEW"`; now uses `decision.target_stage`/`decision.reason` so it stays correct when multi-reason targets land.

The T003-persistence / T008-gatherer flags the review raised are the documented increment boundary (stale-review-only), not regressions.

## Notes (increment 129a — env-recovery gatherer)
Closes the highest-value part of T008 so US2 can be safely enabled: the ENV_BLOCKED recovery gatherer.
- A BLOCKED card carrying the spec-095 per-card `env_blocked` marker is auto-recovered when the symphony's env-cache is healthy again — `_env_cache_recovered()` = `cache_dir_ready and not runtime_health_failed and last_bootstrap_succeeded is True`. This is a **positive** oracle: an env block calls `mark_runtime_health_failed`, which forces a cache regen next cycle; on success the predicate flips on its own. Env causes not reflected in the cache (e.g. an unreachable assessor backend) leave it False → the card stays blocked (never a false recovery).
- The recovery target is the card's pre-BLOCKED working column (`current_card.previous_status`, else IN_PROGRESS); `performer_stage` drives what re-runs.
- The gatherer is unified with the stale-review one: per card we build the set of *detected active* reasons and pass them to the evaluator, which requires ALL to clear (FR-005). No detected reason ⇒ no recovery (safe). A card with both an env block and an unaddressed human CR stays blocked.
- **Still deferred:** CI-red (no persisted per-card CI-block marker) and clarification-answered (ambiguous "answered" detection). Left out deliberately — not recovering is always safe; wrongly recovering is not.
