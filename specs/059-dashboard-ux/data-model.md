# Data Model: Dashboard UX Redesign (059)

This feature is display-layer only. No new persistent data models or database schema changes are introduced. This document describes the **view models** — the shapes of data consumed by each dashboard panel — and the **derived values** computed client-side from the SSE snapshot.

---

## SSE Snapshot Fields (consumed by redesigned views)

All fields below are already present in the `build_snapshot()` return value. No new backend fields are added by this feature.

### Top-level snapshot

| Field | Type | Used by |
|---|---|---|
| `phase` | str | Global phase colour and idle detection |
| `phase_label` | str | Human-readable phase display (always use this, never `phase` raw) |
| `active_sessions` | list[SessionSummary] | Active Work panel, Awaiting Review panel |
| `subsystems` | list[SubsystemStatus] | Health summary panel |
| `cycle_history` | list[CycleEntry] | History page |
| `board_summary` | dict \| None | Idle state panel |
| `last_poll_at` | ISO str \| None | Idle state panel |
| `cycles_completed` | int | Metrics card |
| `last_cycle_duration_seconds` | float \| None | Metrics card |
| `consecutive_error_count` | int | Metrics card |
| `daemon_start_time` | ISO str \| None | Metrics card |
| `role_utilization` | list[RoleUtilization] | Performer utilization |
| `blocked_by_dependencies` | list[DependencyBlocker] | Blocked card indicators |

### SessionSummary (element of `active_sessions`)

| Field | Type | Notes |
|---|---|---|
| `card_id` | str | Unique key |
| `card_title` | str | Display title |
| `issue_number` | int \| None | For `#N` label |
| `issue_url` | str \| None | External link |
| `pr_url` | str \| None | Non-null → awaiting review |
| `phase` | str | Raw phase — always pass through `formatPhaseLabel()` before display |
| `performer_stage` | str | Raw sub-stage |
| `card_tokens_total` | int | Token count |
| `card_cost_estimate` | float | USD; show `"—"` when `agent_dispatch_at` is null |
| `agent_dispatch_at` | ISO str \| None | Session start; compute elapsed from this |
| `container_id` | str \| None | Optional Docker container ID |

### SubsystemStatus (element of `subsystems`)

| Field | Type | Notes |
|---|---|---|
| `name` | str | Subsystem identifier |
| `status` | `"healthy"` \| `"degraded"` \| `"unavailable"` | |
| `required` | bool | Only required subsystems affect overall health |
| `checked_at` | ISO str | Last probe time |
| `details` | str \| None | Plain-language description |

### CycleEntry (element of `cycle_history`)

| Field | Type | Notes |
|---|---|---|
| `timestamp` | ISO str | Cycle completion time |
| `phase` | str | Raw phase — pass through `formatPhaseLabel()` |
| `duration_seconds` | float | |
| `outcome` | `"success"` \| `"error"` | |

---

## Derived View Values (computed client-side)

### `isAwaitingReview(session)`

```
session.pr_url !== null && session.pr_url !== ""
```

Sessions where this is true are shown in the Awaiting Human Review panel; others in Active Work.

### `elapsedMs(session)`

```
Date.now() - Date.parse(session.agent_dispatch_at)
```

Returns `null` when `agent_dispatch_at` is null (no active session).

### `isStale(session)`

```
elapsedMs(session) > 30 * 60 * 1000   // 30 minutes
```

Stale sessions get the elapsed-time warning treatment (⚠ icon + warning colour).

### `costDisplay(session)`

```
session.agent_dispatch_at !== null
  ? "$" + session.card_cost_estimate.toFixed(4)
  : "—"
```

### `overallHealth(subsystems)`

```
const required = subsystems.filter(s => s.required)
if (required.every(s => s.status === "healthy"))   → "healthy"
if (required.some(s => s.status === "unavailable")) → "unavailable"
→ "degraded"
```

### `isIdle(snapshot)`

```
snapshot.active_sessions.length === 0 &&
snapshot.phase === "idle"
```

---

## CSS Design Tokens (`:root` custom properties)

Defined once in the `<style>` block; all colour references in CSS and inline styles use `var(--token)`.

```css
:root {
  --color-bg-base:      #0d1117;
  --color-bg-surface:   #161b22;
  --color-bg-elevated:  #21262d;
  --color-border:       #30363d;
  --color-border-subtle:#21262d;
  --color-text-primary: #c9d1d9;
  --color-text-muted:   #8b949e;
  --color-accent-blue:  #58a6ff;
  --color-accent-green: #3fb950;
  --color-accent-yellow:#d29922;
  --color-accent-red:   #f85149;
  /* Semantic aliases */
  --color-healthy:      var(--color-accent-green);
  --color-degraded:     var(--color-accent-yellow);
  --color-error:        var(--color-accent-red);
  --color-active:       var(--color-accent-blue);
}
```

These replace all hard-coded hex colour values scattered across the CSS and inline `style=` attributes in JS-generated HTML.

---

## Panel View Models

### Active Work Panel

Rendered from `active_sessions` where `isAwaitingReview === false`.

| Display element | Source |
|---|---|
| Card title | `session.card_title` |
| Issue link (`#N`) | `session.issue_number` + `session.issue_url` |
| Phase label | `formatPhaseLabel(session.phase)` |
| Performer stage | `formatPhaseLabel(session.performer_stage)` or `"—"` |
| Elapsed time | `fmtElapsed(elapsedMs(session))` + stale warning if `isStale` |
| Cost | `costDisplay(session)` |

### Awaiting Review Panel

Rendered from `active_sessions` where `isAwaitingReview === true`.

| Display element | Source |
|---|---|
| Card title | `session.card_title` |
| PR link | `session.pr_url` |
| Waiting duration | `fmtElapsed(elapsedMs(session))` |
| Cost | `costDisplay(session)` |

### Health Summary Panel

Rendered from `subsystems`.

| Display element | Source |
|---|---|
| Overall badge | `overallHealth(subsystems)` |
| Badge icon + colour | Healthy → ● green / Degraded → ⚠ yellow / Unavailable → ✕ red |
| Detail rows (on expand) | Full `subsystems` table |
| Degraded detail message | `subsystem.details` (shown inline when degraded/unavailable) |

### Idle State Panel

Rendered when `isIdle(snapshot)`.

| Display element | Source |
|---|---|
| Board total cards | `snapshot.board_summary.total_cards` |
| Board in-progress | `snapshot.board_summary.in_progress` |
| Assignee filter hint | `snapshot.assignee_filter` (if set) |
| Last poll time | `snapshot.last_poll_at` (formatted relative) |
| Cycles completed | `snapshot.cycles_completed` |

### Performer Detail View

| Display element | Source |
|---|---|
| Phase label | `formatPhaseLabel(session.phase)` |
| Assigned card | `session.card_title` + `session.issue_url` |
| Elapsed time | `fmtElapsed(elapsedMs(session))` + stale warning |
| Cost | `costDisplay(session)` |
| Log stream | Existing performer log rendering (last 20 lines) |
