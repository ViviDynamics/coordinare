# Implementation Plan: Auto-Rebase Restart Resilience

**Branch**: `096-auto-rebase-restart` | **Date**: 2026-06-20 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/096-auto-rebase-restart/spec.md`

## Summary

Spec 047's auto-rebase already iterates every in-flight branch and skips the up-to-date ones (`run_rebase_round` → `detect_stale_branches` → `rebase_branch`). The defect is purely the **trigger**: `check_board` calls `run_rebase_round` only on the in-memory edge `current_main != last_known_main_sha`, and `last_known_main_sha` is **not persisted** — so the first cycle after a restart re-baselines to the live main and the edge never fires for merges that landed while the daemon was down.

The fix adds two trigger conditions on top of the existing machinery, changing **no** rebase mechanics:

1. **Persist `last_known_main_sha`** in the snapshot and restore it on startup, so a genuine cross-restart main advance is seen as drift and the existing edge fires (FR-001/FR-002, US1).
2. **Proactively trigger a rebase round when an in-flight branch is stale/conflicting**, independent of the main-moved edge — covering branches stranded from *before* the persisted baseline (the live incident) and any missed edge (FR-003, US2). Reuses `run_rebase_round`, which already SKIPs up-to-date branches.

Plus an anti-thrash guard (FR-007), observability (FR-008/009), and backward-compatible state (FR-010).

## Technical Context

**Language/Version**: Python 3.14 (project min 3.12; prod 3.14.5 via uv)  
**Primary Dependencies**: existing `coordinare.services.rebase` (run_rebase_round / detect_stale_branches / rebase_branch / force_push_with_lease / prepare_conflict_resolution), `github_service` (current-main SHA + per-PR mergeable state), langgraph (`check_board` node), structlog. **No new external dependencies.**  
**Storage**: single-host single-process JSON snapshot via `state_store.py`. Adds a persisted top-level `last_known_main_sha` (and a small per-card anti-thrash marker); schema-version bump, backward-compatible default.  
**Testing**: pytest (`.venv/bin/pytest`); unit (rebase trigger logic) + node-level (`check_board` / startup reconciliation) + a contract test for the snapshot field.  
**Target Platform**: Linux/macOS daemon.  
**Project Type**: single project (coordinare daemon).  
**Performance Goals**: at most one `ls-remote` for main per cycle (already cached via `_main_sha_cache`); the proactive mergeable check adds at most one board/PR read per in-flight card per cycle, gated by the anti-thrash marker so it does no work in steady state.  
**Constraints**: must not regress spec 047 (merge-time path untouched); lease-only publish; per-card isolation; secret-free observability/state.  
**Scale/Scope**: O(in-flight cards) per symphony (typically ≤ max_concurrent_cards, e.g. 3).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. Reuses the existing rebase service; new code is two small trigger conditions + a persisted field + an anti-thrash marker. Single responsibility (triggering, not rebasing). No new dependencies.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. TDD: failing tests first for (a) restart drift fires a rebase round, (b) a conflicting in-flight branch triggers a rebase independent of the edge, (c) up-to-date branch → no rebase / no thrash, (d) snapshot round-trips `last_known_main_sha`, (e) per-card isolation on failure.
- **III. User Experience Consistency** — PASS. Reuses the existing rebase Slack/dashboard surfaces (RebaseRound summary); adds a distinct observability record for the startup/drift/conflict trigger.
- **IV. Performance by Design** — PASS. Reuses the per-cycle main-SHA cache; the proactive mergeable check is gated by the anti-thrash marker so steady state is zero extra work.
- **V. Clarity Before Action** — PASS. The spec resolved scope; the one open implementation choice (persisted-baseline vs proactive-detection) is resolved in research.md as "both, complementary," not deferred ambiguity.

No violations → Complexity Tracking omitted.

## Project Structure

### Documentation (this feature)

```text
specs/096-auto-rebase-restart/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── rebase-triggers.md   # behavioral contract for the new trigger conditions
└── tasks.md             # Phase 2 (/speckit.tasks — not created here)
```

### Source Code (repository root)

```text
src/coordinare/
├── services/
│   └── rebase.py                 # REUSED unchanged: run_rebase_round, detect_stale_branches,
│                                 #   rebase_branch, force_push_with_lease, prepare_conflict_resolution
├── graph/nodes/
│   └── check_board.py            # MODIFY: seed prev_main from persisted value (FR-001); add
│                                 #   proactive stale/conflicting trigger + anti-thrash gate (FR-003/007)
├── daemon.py                     # MODIFY: persist + restore last_known_main_sha; optional startup sweep
│                                 #   hook (aligns with the 094 startup-reconciliation pass)
└── state_store.py                # MODIFY: add persisted last_known_main_sha (+ per-card rebase marker);
                                  #   schema-version bump, backward-compatible default

tests/
├── unit/
│   └── services/test_rebase*.py            # trigger-eligibility + anti-thrash unit tests
├── unit/graph/nodes/test_check_board*.py   # edge-fires-from-persisted-baseline + proactive-conflict
├── unit/test_daemon*.py                    # persist/restore last_known_main_sha
└── contract/test_state_persistence_v*.py   # snapshot schema round-trip for the new field
```

**Structure Decision**: Single project. The change is concentrated in the trigger sites (`check_board.py`, `daemon.py`) + the persistence layer (`state_store.py`), reusing `services/rebase.py` wholesale.

## Phase 0 — Research

See [research.md](research.md). Resolves: where to hook startup drift (094 reconciliation pass vs check_board first cycle); persist-baseline vs proactive-mergeable (both, complementary); the anti-thrash marker shape; how to read per-PR mergeable state without a new dependency.

## Phase 1 — Design & Contracts

See [data-model.md](data-model.md) (persisted `last_known_main_sha`, per-card rebase-thrash marker, reused RebaseOutcome), [contracts/rebase-triggers.md](contracts/rebase-triggers.md) (the trigger decision table + invariants), and [quickstart.md](quickstart.md) (the #168/#169/#171 incident replayed as an acceptance walkthrough).

## Phase 2 — Tasks

Created by `/speckit.tasks` (not here).
