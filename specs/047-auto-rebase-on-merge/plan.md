# Implementation Plan: Auto-Rebase Active Branches on Merge

**Branch**: `047-auto-rebase-on-merge` | **Date**: 2026-04-17 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/047-auto-rebase-on-merge/spec.md`

## Summary

After the closer squash-merges a PR to main, the coordinare rebases every other in-flight PR branch onto the new main. Clean rebases are force-pushed with lease. Conflicted rebases dispatch a performer (implementer role) to attempt automated resolution; only genuinely unresolvable conflicts block the card with a diagnostic comment. The operator receives a single Slack summary per rebase round and the dashboard shows per-card rebase status.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: structlog (logging), asyncio (stdlib), subprocess (git CLI via async wrapper), httpx (GitHub REST for force-push-with-lease)
**Storage**: N/A — rebase state is transient per merge event. `last_known_main_sha` added to CoordinareState for merge detection.
**Testing**: pytest + pytest-asyncio (existing infrastructure)
**Target Platform**: Linux/macOS server (coordinare daemon)
**Project Type**: Single project (src/coordinare/)
**Performance Goals**: Rebase round (clone + rebase + push per branch) must complete within 60s per branch for a clean rebase. Conflict resolution adds performer dispatch time (backend-dependent).
**Constraints**: No concurrent rebase on a branch while a performer is actively writing to it. Force-push-with-lease only (never bare force-push). Only coordinare-managed branches (`coordinare/PVTI_...` pattern).
**Scale/Scope**: Up to 10 concurrent in-flight branches per board.

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | New rebase service module, isolated from existing graph nodes. Git operations via existing `_run_git` async pattern. |
| II. Testing Discipline | PASS | Unit tests for merge detection, rebase orchestration, conflict classification. Integration test with mock git repos. |
| III. User Experience Consistency | PASS | Dashboard rebase status follows existing card-status tile pattern. Slack summary follows existing notification template. |
| IV. Performance by Design | PASS | Budget: 60s per clean rebase. Branches rebased independently (parallel-safe). Active-session guard prevents concurrent writes. |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers. Scope bounded to coordinare-managed branches. |

## Project Structure

### Documentation (this feature)

```text
specs/047-auto-rebase-on-merge/
├── spec.md
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 output (from /speckit.tasks)
```

### Source Code (repository root)

```text
src/coordinare/
├── services/
│   └── rebase.py            # NEW — clone, rebase, push, conflict detection
├── graph/nodes/
│   ├── merge_pr.py          # MODIFY — trigger rebase round after successful merge
│   └── check_board.py       # MODIFY — detect main HEAD change on non-coordinare merges
├── graph/state.py           # MODIFY — add last_known_main_sha, rebase_status fields
├── dashboard.py             # MODIFY — add rebase status to snapshot + JS rendering
└── models/
    └── rebase.py            # NEW — RebaseJob, RebaseRound, RebaseOutcome

tests/unit/
├── services/
│   └── test_rebase.py       # NEW — rebase service tests
├── graph/nodes/
│   ├── test_merge_pr.py     # MODIFY — rebase trigger tests
│   └── test_check_board.py  # MODIFY — main HEAD change detection
└── test_dashboard.py        # MODIFY — rebase status in snapshot
```

**Structure Decision**: New rebase service (`services/rebase.py`) owns all git operations (clone, fetch, rebase, push). New models (`models/rebase.py`) for RebaseJob/Round. merge_pr triggers the round; check_board detects external merges.

## Complexity Tracking

No constitution violations. No complexity justification needed.
