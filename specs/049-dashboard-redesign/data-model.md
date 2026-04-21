# Data Model: Dashboard Redesign

## No new backend entities.

The dashboard redesign is purely a frontend restructuring. All data comes from the existing SSE snapshot (`build_snapshot()`) and existing API endpoints. No new backend models, state fields, or database tables are introduced.

## Frontend Entities (JS runtime state)

### DashboardRoute

| Field | Type | Description |
|-------|------|-------------|
| path | string | URL path (e.g. "/", "/performers", "/personas", "/history") |
| title | string | Page title shown in `<title>` tag |
| render | function | JS function called to render this page's content |

### ActivePerformerTile (JS render object)

Derived from `state.active_sessions` in the SSE snapshot.

| Field | Type | Description |
|-------|------|-------------|
| role | string | Performer stage name (e.g. "implementing", "reviewing") |
| card_title | string | Title of the card being worked on |
| card_number | int | GitHub issue number |
| card_url | string | GitHub issue URL |
| elapsed_seconds | int | Seconds since this session started (from agent_dispatch_at) |
| last_event | string | Most recent performer event summary |
| phase | string | Current session phase |

### PerformerRow (JS render object, /performers page)

Derived from `state.role_utilization` and `state.active_sessions`.

| Field | Type | Description |
|-------|------|-------------|
| role | string | Performer stage name |
| status | "active" \| "idle" | Whether any instance is running |
| active_count | int | Number of running instances |
| max_count | int | Configured max_concurrency |
| queued_count | int | Cards waiting for a free slot |
| current_cards | list[string] | Card titles currently being served |
| elapsed_seconds | int \| null | Session duration for active instance (null if idle) |
| last_events | list[string] | Last 10 event summaries for this role |

## Snapshot Extensions

No changes to `build_snapshot()` output structure. The `/performers` page uses existing fields:
- `active_sessions`: for per-card session data
- `role_utilization`: for slot counts (from spec 048)
- `performer_events`: for recent event history
- `performer_stage`: for active role

## New FastAPI Routes

| Route | Method | Returns | Purpose |
|-------|--------|---------|---------|
| `/performers` | GET | HTML (same shell) | JS renders performers page |
| `/personas` | GET | HTML (same shell) | JS renders personas page |
| `/history` | GET | HTML (same shell) | JS renders history stub |

All routes return `HTMLResponse(_DASHBOARD_HTML)` — same template as `/`. The JS `router()` handles rendering based on `location.pathname`.
