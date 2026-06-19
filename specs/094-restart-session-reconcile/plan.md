# Implementation Plan: Restart-Time Board Reconciliation for Restored Sessions

**Branch**: `094-restart-session-reconcile` | **Date**: 2026-06-18 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/094-restart-session-reconcile/spec.md`

## Summary

On daemon restart, coordinare restores per-card sessions from the JSON snapshot and reconciles only the **top-level `active_card_id` focus** against the live board (`_reconcile_with_board`, daemon.py:805). The per-card `active_sessions` entries — the authoritative state in multi-card mode (`max_concurrent_cards > 1`, which the website symphony runs) — have their `phase` restored verbatim and **never reconciled**. A session restored as `BLOCKED`/`idle` for a card the board has since moved to TODO therefore stays wedged: skipped each cycle while holding a work slot, until a human edits the state file (the issue #158 incident, twice).

The fix extends restart reconciliation from the single top-level focus to **every restored session**: look up each session's card on the live board, correct a diverging phase (board wins) using the existing `_infer_phase_from_board_column`, retire sessions whose card is gone/DONE (existing `_retire_active_session`), preserve still-valid in-flight sessions (`monitoring_pr` / `monitoring_performer`), reconcile the top-level focus to stay consistent, emit a structured per-correction event, and degrade safely on board-lookup failure. No new dependencies; reuses the board poll and helpers already present.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (WorkflowSnapshot/PersistedSession models — reused, unchanged), structlog (reconciliation observability), the existing `github_service.poll_board()` board read, langgraph (unchanged). No new external dependencies.
**Storage**: Existing single-host single-process JSON snapshot via `state_store.py` (`coordinare.state.json`). No schema change — reconciliation reads the live board and corrects in-memory restored state before the first cycle; the next persist captures the corrected state.
**Testing**: pytest (`.venv/bin/pytest`), unit tests in `tests/unit/` (daemon reconciliation), reusing the existing snapshot/board fixtures.
**Target Platform**: Linux/macOS host daemon (single process).
**Project Type**: single (coordinare daemon).
**Performance Goals**: Reconciliation runs once per restart over the restored session set (cardinality = active cards, typically ≤ `max_concurrent_cards`); it reuses the single board poll already performed at startup — no additional GitHub round-trips per session beyond the existing one.
**Constraints**: Must not block daemon startup; must not delete a session on a failed/absent board lookup (FR-009); secret-free observability (FR-007); convergent (re-running on consistent state is a no-op, FR-008).
**Scale/Scope**: Bounded by active sessions per symphony (single digits in practice). Touches `src/coordinare/daemon.py` reconciliation path; no graph-node or model changes anticipated.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First**: PASS. Extends an existing, single-purpose method (`_reconcile_with_board`) and reuses existing helpers (`_infer_phase_from_board_column`, `_retire_active_session`); no new dependency; type-annotated. Risk: the method could grow to "do too much" — mitigated by extracting per-session reconciliation into a focused helper.
- **II. Testing Discipline (NON-NEGOTIABLE)**: PASS (planned). Unit tests cover: BLOCKED→TODO correction, truly-blocked stays blocked, monitoring_pr/monitoring_performer preserved, card-absent retire, board-lookup-failure degrade, top-level/session consistency, convergence (no-op on consistent state). Tests are deterministic (board poll mocked). Coverage must not decrease.
- **III. User Experience Consistency**: N/A (no user-facing UI surface; the "user" is the operator reading logs — addressed by the structured reconciliation event, FR-006).
- **IV. Performance by Design**: PASS. Reuses the single existing startup board poll; per-session work is in-memory map lookups. Budget captured in Success Criteria (SC-001 within one board cycle; SC-006 convergence). No new hot-loop.
- **V. Clarity Before Action**: PASS. The three design questions (board-wins, preserve-in-flight, don't-force-truly-blocked) were resolved in spec Clarifications; no `NEEDS CLARIFICATION` markers remain. One implementation-level question (exactly which field — phase, current_card column, or both — must be corrected to re-enable dispatch) is flagged as a Phase 0 research item with a reproduction test, not an open scope ambiguity.

**Result**: PASS — no violations; Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/094-restart-session-reconcile/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (reconciliation event + helper contract)
└── tasks.md             # Phase 2 output (/speckit.tasks — not created here)
```

### Source Code (repository root)

```text
src/coordinare/
├── daemon.py                 # PRIMARY: extend _reconcile_with_board to iterate
│                             #   active_sessions; add per-session reconcile helper;
│                             #   reconcile top-level focus consistency; emit event.
│                             #   Reuses _infer_phase_from_board_column (795),
│                             #   _retire_active_session (imported), poll_board (820).
└── state_store.py            # READ-ONLY: WorkflowSnapshot / PersistedSession shapes
                              #   (no schema change needed).

tests/unit/
└── test_daemon_restart_reconcile.py   # NEW: US1/US2/US3 + edge-case coverage
                                       #   (extends patterns in existing daemon
                                       #   reconciliation/snapshot tests).
```

**Structure Decision**: Single-project coordinare daemon. The change is localized to `src/coordinare/daemon.py`'s restart-reconciliation path; `state_store.py` is reused unchanged. New unit-test module mirrors existing daemon reconciliation tests.

## Complexity Tracking

> No constitution violations — section intentionally empty.
