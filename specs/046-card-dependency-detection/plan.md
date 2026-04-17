# Implementation Plan: Card Dependency Detection

**Branch**: `046-card-dependency-detection` | **Date**: 2026-04-16 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/046-card-dependency-detection/spec.md`

## Summary

Add card-dependency awareness to the coordinare's multi-card orchestration. Cards declare dependencies via issue-body syntax ("Depends on #N", "Blocked by #N", etc.). The coordinare builds a dependency graph on each poll cycle, filters the TODO queue to skip cards whose blockers haven't reached DONE, detects circular dependencies, and surfaces dependency state in the dashboard and Slack notifications. The assessor additionally analyses card titles against active board cards to flag implicit dependencies.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: structlog (logging), pydantic (models), asyncio (stdlib), gql[aiohttp] (GitHub GraphQL — existing)
**Storage**: N/A — dependency graph is rebuilt from board state + issue bodies on each poll cycle. No new persistence.
**Testing**: pytest + pytest-asyncio (existing test infrastructure)
**Target Platform**: Linux/macOS server (coordinare daemon)
**Project Type**: Single project (src/coordinare/)
**Performance Goals**: Dependency graph construction + filtering must complete in < 50ms for a board with 50 cards and up to 100 dependency edges. Must not add observable latency to the poll cycle.
**Constraints**: Zero additional GitHub API calls for explicit dependencies (parsed from descriptions already fetched by `poll_board`). Assessor implicit-dependency detection adds one Claude API call per card assessed (existing flow — just more context in the prompt).
**Scale/Scope**: Boards with up to 50 active cards, up to 5 dependencies per card.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Single-responsibility modules: dependency parsing, graph building, filtering are separate functions. No new external dependencies. Type annotations on all public interfaces. |
| II. Testing Discipline | PASS | Unit tests for parser, graph builder, cycle detector, check_board filter, assessor injection. Contract test for dashboard payload shape. |
| III. User Experience Consistency | PASS | Dashboard dependency display follows existing blocked-card patterns (badge + link). Slack messages follow existing template structure. |
| IV. Performance by Design | PASS | Graph built from in-memory dicts already fetched. O(V+E) cycle detection. No new API calls for explicit deps. Performance budget: <50ms for 50 cards. |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers remain. Scope bounded to same-repo issue numbers. Assessor detection is best-effort (documented assumption). |

## Project Structure

### Documentation (this feature)

```text
specs/046-card-dependency-detection/
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
│   └── dependency.py        # NEW — parse_dependencies(), build_graph(), detect_cycles()
├── graph/nodes/
│   ├── check_board.py       # MODIFY — filter eligible_todo by dependency satisfaction
│   └── assess_card.py       # MODIFY — inject active card titles into assessor context
├── dashboard.py             # MODIFY — add blocked_by_dependencies to snapshot payload + render
├── graph/state.py           # MODIFY — add blocked_by_dependencies field to CoordinareState
└── models/
    └── dependency.py        # NEW — CardDependency, DependencyGraph dataclasses

tests/unit/
├── services/
│   └── test_dependency.py   # NEW — parser, graph, cycle detection tests
├── graph/nodes/
│   ├── test_check_board.py  # MODIFY — dependency filtering tests
│   └── test_assess_card.py  # MODIFY — active-card injection test
└── test_dashboard.py        # MODIFY — snapshot shape test for blocked_by_dependencies
```

**Structure Decision**: All new code lives in two new files (`services/dependency.py` and `models/dependency.py`). Everything else is modifications to existing files at identified insertion points.

## Complexity Tracking

No constitution violations. No complexity justification needed.
