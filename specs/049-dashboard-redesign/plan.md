# Implementation Plan: Dashboard Redesign

**Branch**: `049-dashboard-redesign` | **Date**: 2026-04-20 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/049-dashboard-redesign/spec.md`

## Summary

Redesign the single-page coordinare dashboard into a multi-page SPA with client-side routing (`pushState`), a persistent navbar, and a restructured main page that leads with the active-performer view. The persona editor moves to `/personas`. A new `/performers` page surfaces per-role status and event history. The workflow diagram is reduced to ≤25% viewport height. History page is a stub ("Coming soon").

## Technical Context

**Language/Version**: Python 3.12+ (backend), vanilla JavaScript + HTML/CSS (frontend)
**Primary Dependencies**: FastAPI + Starlette (existing), asyncio (stdlib), structlog (existing)
**Storage**: N/A — dashboard is a read-only view of existing state; personas already persisted
**Testing**: pytest (backend routes), manual browser testing (frontend layout)
**Target Platform**: Web browser (Chrome, Firefox, Safari); served by coordinare's FastAPI daemon
**Project Type**: Single project — all frontend embedded in `src/coordinare/dashboard.py`
**Performance Goals**: Page navigation < 200ms (client-side, no reload). Dashboard first paint with active-performer view above fold on 1080p.
**Constraints**: Vanilla JS only — no React/Vue/bundler. `pushState` routing. SSE stream unchanged. No new backend endpoints needed for routing (routes `/`, `/performers`, `/personas`, `/history` all return the same HTML shell; JS renders the right page).
**Scale/Scope**: 4 pages (Dashboard, Performers, Personas, History stub). ~1600-line dashboard.py remains a single file.

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Restructure JS into page-render functions, one per route. CSS variables for theming. |
| II. Testing Discipline | PASS | Backend route tests for new paths. Frontend visual correctness verified by quickstart scenarios. |
| III. User Experience Consistency | PASS | Navbar present on all pages; active state highlighted; back/forward work. |
| IV. Performance by Design | PASS | Budget: navigation <200ms (pure JS). SSE stream unchanged. |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers. History stub is explicit. |

## Project Structure

### Source Code

```text
src/coordinare/
└── dashboard.py          # MODIFY — all HTML/CSS/JS lives here (~1625 lines → ~1900 lines)
                          # New routes: /performers, /personas, /history
                          # New JS: router(), renderDashboard(), renderPerformers(),
                          #         renderPersonas(), renderHistory()
                          # Existing: SSE stream, build_snapshot(), API endpoints unchanged
```

**Structure Decision**: Keep everything in `dashboard.py` — no new files. The frontend is embedded HTML/JS/CSS. Adding routes is a small FastAPI change. The big work is restructuring the JS render loop and HTML template.

### Key JS Changes

1. **Router**: `router()` function reads `location.pathname`, renders the matching page, updates nav active state
2. **Nav bar**: `<nav>` with 4 links; `pushState`-based navigation via `addEventListener('click')` on nav items
3. **Page functions**: `renderDashboard()`, `renderPerformers()`, `renderPersonas()`, `renderHistory()` — each controls which DOM sections are visible
4. **Active-performer view**: New `#active-performers` section at the top of the dashboard page with per-card tiles showing role, card title, elapsed time
5. **Workflow diagram**: CSS max-height capped at 25vh; moved below the active-performer section
6. **Personas page**: Existing `#personas-section` content shown on `/personas`, hidden on other pages

### New FastAPI Routes

```python
@app.get("/performers")  # returns same HTML shell; JS renders performers page
@app.get("/personas")    # returns same HTML shell; JS renders personas page
@app.get("/history")     # returns same HTML shell; JS renders history stub
```

All routes return the same `_DASHBOARD_HTML` template. JS handles rendering based on `location.pathname`.

## Complexity Tracking

No constitution violations. No complexity justification needed.
