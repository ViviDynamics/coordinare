# Feature Specification: Unify Card Pickup Paths

**Feature Branch**: `066-unify-card-pickup`
**Created**: 2026-05-19
**Status**: Draft
**Input**: Surfaced during 065 QA-cycle. Single-card and multi-card pickup are two divergent code paths in `src/coordinare/graph/nodes/check_board.py`. Every fix to this area in the last three cycles (Fix 2 IN_PROGRESS fall-through, Fix 5 monitoring_performer fall-through, US4 un-block feedback reset) has had to be ported twice because the branches drifted. The unification target is: one pickup path, with `max_concurrent_cards=1` acting as the special case rather than a separate branch.

## Background

`check_board` currently splits at the eligibility/pickup boundary based on `config.max_concurrent_cards`. The single-card branch sets `state["current_card"]` directly and never touches `state["active_sessions"]`. The multi-card branch hydrates `active_sessions` and uses per-session graph invocations (`_invoke_multi_session`) to fan out work.

Concrete divergences that have caused production bugs:

- **Fix 2 (065)** — IN_PROGRESS fall-through to TODO pickup only existed in the single-card branch. Multi-card mode could not pick up additional TODOs while any card was IN_PROGRESS.
- **Fix 5 (065)** — The `dispatching | monitoring_performer | blocked` early-return clobbered open multi-card slots. Single-card mode was unaffected; multi-card mode silently stalled.
- **US4 (065)** — Operator un-block reset of `feedback_cycle_count` lives in the single-card `else` branch only. Multi-card un-block does not reset, even though the same monotonic counters (`total_feedback_cycles`, `triage_blocks`) are wired through the rest of the system.

The cost compounds: any future change to pickup behaviour (advocate-label filtering, dependency-block UX, kicked-back-card handling) has to be implemented twice, tested twice, and reviewed twice. Test surface area is doubled and the two branches drift apart between fixes.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Operator Configures Concurrency Without Behavioural Surprise (Priority: P1)

An operator sets `max_concurrent_cards: 1` in `config.yaml` and expects exactly the behaviour they would get from running the daemon in single-card mode today: one card in flight at a time, identical un-block semantics, identical re-adoption, identical dashboard fields. Conversely, an operator who sets `max_concurrent_cards: 3` and later drops to 1 expects no behavioural surprises — only a concurrency change.

**Why this priority**: Operators have to be able to reason about the system from configuration alone. "max_concurrent_cards=1 takes a completely different code path" is an invisible footgun and the root cause of bug clusters Fix 2 / Fix 5 / US4.

**Independent Test**: Run the daemon end-to-end with `max_concurrent_cards=1` against a board with one IN_PROGRESS and two TODO cards. Observe: (a) only one card ever has `phase ∈ NON_PASSIVE_PHASES` at a time, (b) `active_sessions` has exactly one entry, (c) un-block on the single in-flight card resets `feedback_cycle_count` and emits `dispatcher.feedback_cycle_reset`, (d) the dashboard shows the same fields it shows today in single-card mode.

**Acceptance Scenarios**:

1. **Given** `max_concurrent_cards=1` and a single IN_PROGRESS card on the board, **When** the daemon runs a cycle, **Then** `active_sessions` contains exactly that card, `current_card` is populated from the session, and `phase` is set per the existing single-card pickup rules.
2. **Given** `max_concurrent_cards=1` and an operator un-blocks the in-flight card, **When** the next cycle runs, **Then** `feedback_cycle_count` resets to 0, `triage_blocks` and `total_feedback_cycles` are preserved, and the `dispatcher.feedback_cycle_reset` log record fires.
3. **Given** a v1 snapshot with no `active_sessions` (pre-Fix 7 daemon restart from disk), **When** the unified code loads it, **Then** the daemon rehydrates `active_sessions` with the legacy `current_card` as its sole entry and proceeds without manual intervention.

### User Story 2 — Future Pickup Changes Land Once (Priority: P1)

A future contributor implementing a new pickup-side behaviour (e.g. advocate-label dispatch, dependency-cycle UX, kicked-back-card handling) writes the change in one place, adds one set of tests, and is done. The reviewer does not have to ask "did you also do the multi-card path?" because the path is one.

**Why this priority**: Most of the bug cost in this area is not the original miss — it's the second port six weeks later. Removing the divergence pays back across every subsequent change in `check_board`.

**Independent Test**: Pick a representative pickup-side change (proposed: relocate the advocate-label filter check). Implement it once. Run the full unit suite. Confirm both `max_concurrent_cards=1` and `max_concurrent_cards>1` integration tests still pass without any conditional logic at the pickup site.

**Acceptance Scenarios**:

1. **Given** a pickup-side change implemented once, **When** the suite runs, **Then** both single-card and multi-card scenarios pass with no `if max_cards > 1` branching at the call site.
2. **Given** a code-search for the literal `if max_cards > 1` (or equivalent) in `check_board.py` post-unification, **When** the search runs, **Then** no eligibility/pickup-time branch on that condition exists.

## Requirements

### Functional Requirements

- **FR-001**: `check_board` MUST have exactly one pickup code path. Pickup MUST always hydrate `active_sessions`; `state["current_card"]` MUST be derived from the session set rather than set independently.
- **FR-002**: With `max_concurrent_cards=1`, observable behaviour MUST match the pre-unification single-card behaviour for: TODO pickup ordering, IN_PROGRESS re-adoption, IN_REVIEW re-adoption, BLOCKED-comment Q&A detection (`check_board.py:692`), un-block feedback reset (US4), dependency cycle announcement, and advocate-label filtering. "Match" is defined by the existing unit tests in `tests/unit/graph/nodes/test_check_board.py` and `tests/unit/graph/nodes/test_check_board_multicard*.py` — both suites MUST pass against the unified code without conditional logic forks on `max_concurrent_cards`.
- **FR-003**: The un-block reset path (originally introduced as US4 in 065) MUST fire on un-block regardless of `max_concurrent_cards`. The detection MUST work for: card previously held by an `active_sessions` entry whose card status went BLOCKED, and card whose `current_card` snapshot held `status="BLOCKED"`. The reset MUST emit `dispatcher.feedback_cycle_reset` with `{card_id, prior_count, total_feedback_cycles, triage_blocks}`.
- **FR-004**: `_invoke_multi_session` MUST be the only invocation path. Single-session entries MUST flow through it (with a session count of 1). The legacy direct-state graph invocation path MUST be removed.
- **FR-005**: State snapshot rehydration (`daemon._restore_from_snapshot`) MUST handle three cases: (a) snapshot has `active_sessions` populated (v2+, post-Fix 7), (b) snapshot has only `current_card` set (v1), (c) snapshot is empty (cold start). Cases (b) and (c) MUST land in the same in-memory shape as (a).
- **FR-006**: `current_card`, `performer_stage`, `feedback_cycle_count`, `total_feedback_cycles`, `triage_blocks`, and any other field the dashboard reads MUST remain on the top-level `CoordinareState` for the single in-flight card, even when sourced from a session, to preserve API and SSE compatibility with the dashboard (no client-visible schema change).
- **FR-007**: A migration / compatibility test MUST exist that loads a real production v1 snapshot (or synthetic equivalent: only `current_card` set, no `active_sessions`) and asserts the unified code produces the same `active_sessions` shape it would produce on a fresh start with that card.
- **FR-008**: The full `tests/unit` suite MUST pass without regressions. Tests that currently assume "single-card mode does not touch `active_sessions`" MAY be updated to assert the new invariant (single-card mode always has exactly one entry in `active_sessions`), but MUST NOT be deleted.

### Success Criteria

- **SC-001**: A `grep -n "if max_cards > 1\|max_concurrent_cards == 1\|single.card.mode" src/coordinare/graph/nodes/check_board.py` returns zero pickup-time branches post-merge.
- **SC-002**: The cycle latency for `max_concurrent_cards=1` increases by no more than 50 ms per board scan over the pre-unification baseline (`pytest --benchmark` covers this if it exists; otherwise a wall-clock measurement on a known-shape board).
- **SC-003**: Zero new bugs surface in 30 days of live operator-supervised running across both `max_concurrent_cards=1` and `max_concurrent_cards=3` configurations.

## Out of Scope

- Increasing the default `max_concurrent_cards` value.
- Changing the persistence schema beyond the rehydration changes required by FR-005 (Fix 7's v2 schema is sufficient).
- Modifying `_invoke_multi_session`'s graph topology — only its entry conditions.
- Rewriting `monitor_performer` or `dispatch_performer` — these read `current_card` / `active_sessions` and remain unchanged.
- Operator-facing comment → relay_feedback threading on un-block — that is a separate, higher-priority gap surfaced during 065 testing (planned as 065 Fix 17 or its own spec).

## Files Likely to Change

- `src/coordinare/graph/nodes/check_board.py` — fold single-card pickup into multi-card path; collapse early-returns; one IN_PROGRESS branch; one un-block detection.
- `src/coordinare/daemon.py` — `_restore_from_snapshot` v1 compatibility, `_invoke_multi_session` as the sole entry point, removal of any `if config.max_concurrent_cards == 1: ...` shortcuts.
- `src/coordinare/graph/state.py` — possibly tighten the invariant comment; no schema change required.
- `tests/unit/graph/nodes/test_check_board.py` — update assertions where they assume `active_sessions` is empty in single-card mode.
- `tests/unit/graph/nodes/test_check_board_multicard*.py` — parameterise the `max_concurrent_cards=1` case across existing multi-card tests.
- `tests/unit/test_state_store.py` — extend the v1 forward-compat test to cover the unified rehydration.

## Open Questions

1. Should `current_card` remain a top-level state field for dashboard/SSE compatibility (FR-006 says yes), or do we take the opportunity to deprecate it and route all reads through `active_sessions[active_card_id]`? Deprecation is a larger blast radius (dashboard, monitor_pr, monitor_performer, notify) and probably belongs in its own follow-up.
2. The Q&A-answer-detected path (`check_board.py:692`) currently mutates `state["current_card"]["previous_status"]` directly. In the unified world, does this mutation live on the session or on the top-level mirror? Likely the session, with the top-level mirror re-derived after.
3. Is there appetite to also unify the IN_REVIEW re-adopt path (currently lives in a separate branch from IN_PROGRESS re-adopt)? Probably yes — same divergence risk — but adds scope.
