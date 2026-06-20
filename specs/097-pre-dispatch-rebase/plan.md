# Implementation Plan: Pre-Dispatch Rebase Guard

**Branch**: `097-pre-dispatch-rebase` | **Date**: 2026-06-20 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/097-pre-dispatch-rebase/spec.md`

## Summary

Add a rebase check at the **dispatch decision point**. Spec 096 already heals conflicting/behind in-flight branches via a proactive sweep, but (inheriting 047 FR-006) it skips a branch with an active performer — so a conflicting branch that keeps getting a performer re-dispatched onto it livelocks (the performer can't fix a stale-base conflict; 096 never gets a rebase window).

The fix places the rebase **before** the performer starts: in `dispatch_performer`, after the in-flight guard confirms no performer is running for this `(card, stage)` and just before `_dispatch_performer_body`, if the card has an open PR whose branch is `CONFLICTING`/`BEHIND` main, rebase it first (reusing 096's `check_mergeability` + `should_attempt_rebase` + `run_rebase_round` + `last_rebase_attempt` marker). Clean → proceed to dispatch on the rebased head; conflict → block/route and do **not** dispatch onto the unfixable base; unknown/no-head → defer the dispatch one cycle.

This is **the same machinery as 096, invoked at a different point** — `dispatch_performer` instead of `check_board`. No new state, no new dependency, 047/096 triggers untouched.

## Technical Context

**Language/Version**: Python 3.14 (project min 3.12; prod 3.14.5 via uv)  
**Primary Dependencies**: existing `coordinare.services.rebase` (run_rebase_round / rebase_branch / should_attempt_rebase / prepare_conflict_resolution / fetch_main_sha — from 047/096), `github_service.check_mergeability` (096; returns mergeable_raw / merge_state_status / head_ref_oid), `dispatch_performer` node, structlog. **No new external dependencies.**  
**Storage**: single-host single-process JSON snapshot via `state_store.py`, **unchanged** — reuses 096's top-level `last_known_main_sha` and per-card `last_rebase_attempt`. No schema bump.  
**Testing**: pytest (`.venv/bin/pytest`); node-level tests on `dispatch_performer` (guard rebases-then-dispatches / blocks-on-conflict / defers-on-unknown / skips-when-current / disabled-noop / per-card isolation) + reuse of the 096 thrash helper.  
**Target Platform**: Linux/macOS daemon.  
**Project Type**: single project (coordinare daemon).  
**Performance Goals**: at most one mergeability read per dispatch decision for a card that already has a PR (dispatch is comparatively rare); gated by the anti-thrash marker so a blocked branch does no repeated work.  
**Constraints**: must not regress 047/096; lease-only publish; never rebase under a live performer (the injection point guarantees this); per-card isolation; secret-free observability.  
**Scale/Scope**: O(1) per dispatch decision.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. One new guard step in `dispatch_performer` reusing the 096/047 service functions; single responsibility (decide-then-rebase-before-dispatch). No new deps.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. TDD: failing tests first for rebase-then-dispatch (clean), block-no-dispatch (conflict), defer (unknown), no-op (current / no-PR / disabled), per-card isolation.
- **III. User Experience Consistency** — PASS. Reuses the existing rebase Slack/dashboard surfaces + 096's `rebase.triggered` record (new `reason=pre_dispatch`).
- **IV. Performance by Design** — PASS. One mergeability read per dispatch of an already-PR'd card; anti-thrash marker prevents repeat work.
- **V. Clarity Before Action** — PASS. The spec is unambiguous; the injection point and reuse are settled in research.md.

No violations → Complexity Tracking omitted.

## Project Structure

### Documentation (this feature)

```text
specs/097-pre-dispatch-rebase/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── pre-dispatch-guard.md
└── tasks.md            # /speckit.tasks — not created here
```

### Source Code (repository root)

```text
src/coordinare/
├── graph/nodes/
│   └── dispatch_performer.py     # MODIFY: pre-dispatch rebase guard before _dispatch_performer_body
├── services/
│   └── rebase.py                 # REUSED: run_rebase_round, rebase_branch, should_attempt_rebase,
│                                 #   prepare_conflict_resolution, fetch_main_sha (047/096)
├── services/github.py            # REUSED: check_mergeability (096; mergeable_raw/merge_state_status/head_ref_oid)
└── state_store.py / session.py   # REUSED unchanged: 096's last_known_main_sha + last_rebase_attempt

tests/
└── unit/graph/nodes/test_dispatch_performer*.py   # guard behavior + isolation
```

**Structure Decision**: Single project. The change is a guard inside `dispatch_performer`, reusing `services/rebase.py` + `github.check_mergeability` wholesale. No persistence change.

## Phase 0 — Research

See [research.md](research.md): the exact injection point (after `check_inflight`, before `_dispatch_performer_body`) and why it satisfies FR-004 for free; how a conflict result maps to a blocked/held return without dispatching; reusing 096's mergeability + thrash marker; sourcing the current main sha.

## Phase 1 — Design & Contracts

See [data-model.md](data-model.md) (no new entities; reuses 096's fields), [contracts/pre-dispatch-guard.md](contracts/pre-dispatch-guard.md) (the guard decision table + invariants), and [quickstart.md](quickstart.md) (the #169/#171 livelock replayed as an acceptance walkthrough).

## Phase 2 — Tasks

Created by `/speckit.tasks` (not here).
