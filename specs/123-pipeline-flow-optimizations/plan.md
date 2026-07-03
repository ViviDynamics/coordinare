# Implementation Plan: Coordinare Pipeline Flow Optimizations

**Branch**: `123-pipeline-flow-optimizations` | **Date**: 2026-06-30 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/123-pipeline-flow-optimizations/spec.md`

## Summary

Seven independently-shippable optimizations that eliminate redundant AI dispatches (US1: doc gate, US2: QA scope, US7: closer scope), prevent false `blocked` escalations (US3: split bounce budget), reduce context loss across bounce cycles (US4: Q&A carryover), sharpen routing logic (US5: multi-concern gate), and remove wasted AI calls per poll cycle (US6: comment dedup). All changes are backward-compatible with existing `coordinare.state.json` — new state fields default to 0/empty, legacy field migrated on read.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod on 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (PersistedSession model extension), structlog (observability); no new external dependencies
**Storage**: Existing single-host single-process JSON snapshot via `state_store.py`; three new fields on `PersistedSession` with defaults
**Testing**: pytest via `.venv/bin/pytest`; ruff via `.venv/bin/ruff check`
**Target Platform**: Linux server (coordinare process)
**Project Type**: Single Python project under `src/coordinare/`
**Performance Goals**: Reduce AI classification calls per board poll by ≥50% after first poll (US6); reduce QA stage duration by ≥15% (US2); tech_writer dispatches eliminated for ≥30% of qualifying cards (US1)
**Constraints**: No schema migration tooling; backward-compat defaults required; no new external dependencies; no resets of live state on restart

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | ✓ PASS | Editing existing modules; single-responsibility maintained; type annotations on new fields |
| II. Testing Discipline | ✓ PASS | All new logic requires unit tests; existing test files extended |
| III. UX Consistency | N/A | Backend-only changes; no UI surfaces modified |
| IV. Performance by Design | ✓ PASS | SC-001 through SC-007 define measurable outcomes |
| V. Clarity Before Action | ✓ PASS | All NEEDS CLARIFICATION items resolved in research.md |

No constitution violations. Complexity Tracking table not required.

## Project Structure

### Documentation (this feature)

```text
specs/123-pipeline-flow-optimizations/
├── plan.md            ← this file
├── research.md        ← Phase 0 output (written)
├── data-model.md      ← Phase 1 output (written)
├── quickstart.md      ← Phase 1 output (written)
├── checklists/
│   └── requirements.md
└── tasks.md           ← Phase 2 output (speckit.tasks command)
```

### Source Code — Files Modified

```text
src/coordinare/
├── graph/
│   ├── nodes/
│   │   ├── dispatch_performer.py   # doc gate, prior_clarifications injection, multi-concern routing
│   │   ├── monitor_performer.py    # split bounce budget, open_questions persistence
│   │   └── check_board.py          # dedup before classification
│   └── state.py                    # new PersistedSession fields + legacy migration
└── services/
    └── persona_service.py          # QA scope trim, closer scope rewrite

tests/unit/
├── graph/
│   ├── nodes/
│   │   ├── test_dispatch_performer.py  # doc gate, prior_clarifications, multi-concern
│   │   ├── test_monitor_performer.py   # split budget, open_questions
│   │   └── test_check_board.py         # pre-classification dedup
│   └── state/
│       └── test_persisted_session.py   # legacy migration
└── services/
    └── test_persona_service.py         # QA/closer persona smoke tests
```

## Implementation Phases

### Phase 0 — State model changes (blocking prerequisite)

Add three fields to `PersistedSession` in `src/coordinare/graph/state.py` with defaults and legacy migration. All subsequent phases depend on these fields being in place.

Key tasks:
- Add `content_feedback_cycles: int = 0` to `PersistedSession`
- Add `transient_error_cycles: int = 0` to `PersistedSession`
- Add `open_questions: list[dict] = []` (or `list[OpenQuestion]` with a small TypedDict) to `PersistedSession`
- Implement legacy migration: `model_validator(mode="before")` that reads `feedback_cycle_count` into `content_feedback_cycles` if new field is absent/0

### Phase 1 — monitor_performer split budget (US3) + Q&A persistence (US4)

Modify `monitor_performer.py`:
- `_feedback_cycle_exhausted()`: branch on failure type before incrementing (`changes_requested` → `content_feedback_cycles`; `env_blocked`/`system_error`/`unknown` → `transient_error_cycles`). Check each counter against its respective limit.
- After successful assessor result: extract `open_questions` from performer report JSON, write to `session.open_questions`.

### Phase 2 — dispatch_performer doc gate (US1) + multi-concern routing (US5) + prior_clarifications (US4)

Modify `dispatch_performer.py`:
- Add `_should_skip_documenting(changed_files: list[str]) -> bool`: returns True if no path starts with `docs/`
- Before dispatching "documenting" stage: call `_should_skip_documenting`; if True, log `stage_skipped: reason=no_doc_changes` and advance card without dispatch
- `_build_card_context()` (or equivalent): inject `prior_clarifications` from `session.open_questions` when dispatching assessor
- After reviewer returns `changes_requested`: classify concern categories from feedback; if `len(set(categories)) >= 2`, route to `assessing` instead of `implementing`

### Phase 3 — check_board comment dedup (US6)

Modify `check_board.py`:
- Move `processed_issue_comment_ids` filter to before `_classify_with_ai()` call
- If filtered list is empty, skip classification entirely for this poll

### Phase 4 — Persona updates (US2 + US7)

Modify `persona_service.py`:
- QA persona: remove instructions to run linters, check test coverage, or report code style issues; retain acceptance-criteria verification and screenshot capture instructions
- Closer persona: rewrite to thread-resolution-only: confirm open reviewer threads are resolved + CI is passing; remove code review / linting instructions

### Phase 5 — Tests

For each phase above: write/extend unit tests. See `quickstart.md` for example test patterns. Coverage must not decrease.

## Delivery Order

US3 state model (Phase 0) → US3/US4 monitor_performer (Phase 1) → US1/US4/US5 dispatch_performer (Phase 2) → US6 check_board (Phase 3) → US2/US7 personas (Phase 4) → Tests throughout.

US2 and US7 (persona text edits) are fully independent and can be done at any point; they don't touch state or graph logic.
