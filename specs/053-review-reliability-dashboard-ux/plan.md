# Implementation Plan: Review Reliability and Dashboard UX Completion

**Branch**: `053-review-reliability-dashboard-ux` | **Date**: 2026-04-22 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/053-review-reliability-dashboard-ux/spec.md`

## Summary

This feature closes two operational gaps in one delivery:

1. **Reviewer reliability**: prevent lifecycle hard-blocks caused by non-JSON backend output in JSON-required stages by introducing role-aware prompt contracts, structured format recovery, and monitor-side retry classification.
2. **Dashboard completion**: finish deferred UX in the dashboard redesign by adding meaningful idle summary, replacing History stub with real data, enabling `/performers` drilldown details, and moving workflow + performers cards to one-column desktop footprint.
3. **UX/design hardening**: implement explicit information hierarchy, route-level interaction consistency, keyboard-accessible drilldown controls, and UX-focused validation coverage from the spec's design plan.

## Technical Context

**Language/Version**: Python 3.12 (backend), embedded vanilla JavaScript/CSS in `dashboard.py`  
**Primary Dependencies**: FastAPI/Starlette, structlog, asyncio, existing performer backends (Codex/OpenCode/Claude Code)  
**Storage**: In-memory runtime state + existing `DashboardStore` cycle deque (no new persistence)  
**Testing**: `pytest` unit/integration/e2e suites already in repo  
**Target Platform**: Local web dashboard served by coordinare daemon; performer subprocess backends  
**Project Type**: Single Python project with embedded frontend in `src/coordinare/dashboard.py`  
**Performance Goals**: No added polling loops; dashboard route render remains instant with client-side routing  
**Constraints**: Keep SSE contract additive/backward-compatible; no frontend framework migration; preserve current route shell pattern (`/`, `/performers`, `/personas`, `/history`)  
**Scale/Scope**: Reliability changes in performer + monitor path, plus dashboard UI/JS updates in one file

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| Code Quality First | PASS | Additive, targeted changes in existing modules with role-aware contracts. |
| Testing Discipline | PASS | Extend existing performer + monitor + dashboard tests before behavior changes. |
| User Experience Consistency | PASS | Keep current SPA navigation and SSE model; improve content and layout only. |
| Performance by Design | PASS | Reuse existing snapshot fields and lightweight render paths. |
| Clarity Before Action | PASS | Scope explicitly tied to observed operator failures and deferred redesign items. |

## Project Structure

### Documentation (this feature)

```text
specs/053-review-reliability-dashboard-ux/
|-- plan.md
|-- spec.md
|-- research.md
|-- data-model.md
|-- quickstart.md
`-- tasks.md
```

### Source Code (repository root)

```text
agent/performer/src/performer/
|-- main.py                         # JSON parse recovery + role output contract handling
`-- backends/
    |-- codex.py                    # role-aware prompt tail
    |-- opencode.py                 # role-aware prompt tail
    `-- claude_code.py              # role-aware prompt tail

src/coordinare/
|-- graph/nodes/monitor_performer.py  # classify format failures as retryable system errors
`-- dashboard.py                      # BoardSummary snapshot + history/performers/layout UX fixes

tests/
|-- unit/test_dashboard.py
|-- unit/graph/nodes/test_monitor_performer.py
`-- e2e/test_dashboard_browser.py

agent/performer/tests/unit/
`-- test_main.py
```

**Structure Decision**: Keep all dashboard UI/JS changes in `src/coordinare/dashboard.py` (existing architecture) and keep performer reliability logic in `agent/performer/src/performer/main.py` plus shared prompt builders in each backend adapter.

## Implementation Tracks

### Track A: Reviewer/JSON reliability

1. Add role-aware prompt tail helpers in backend adapters so reviewer-style stages are explicitly output-contract-driven.
2. Extend performer parse-failure handling to execute a structured JSON-format recovery attempt for JSON-required roles before terminal error.
3. Mark terminal format failures with a machine-detectable reason prefix/code.
4. Update `monitor_performer` error branch to classify those failures as retryable system errors (bounded) before final operator block.

### Track B: Dashboard completion

1. Extend snapshot payload with board counts + `last_poll_at` formatting inputs.
2. Update Active Performers idle tile rendering to include board summary + poll timestamp.
3. Replace `/history` stub with functional cycle-history rendering and empty-state handling.
4. Add row drilldown behavior to `/performers` by reusing existing performer detail view logic.
5. Adjust dashboard grid classes so workflow and compact performers cards are one-column each on desktop.

### Track C: UX / design execution

1. Align section ordering and visual hierarchy across routes so active operational state remains the first-scanned region.
2. Standardize empty-state copy and status badge language/color usage across dashboard, performers, and history.
3. Add keyboard-accessible row drilldown behavior and visible focus states for in-page interactions.
4. Ensure responsive behavior matches plan: two-column desktop density and single-column mobile readability without horizontal scroll.
5. Add UX-targeted e2e assertions for route consistency, detail-state stability during SSE updates, and idle summary readability.

## Complexity Tracking

No constitution violations expected. The largest risk is regression in dashboard JS due single-file complexity; mitigated with targeted unit + e2e coverage updates.
