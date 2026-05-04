# API Contracts: Dashboard State Schema

**Status**: Phase 1 Design  
**Version**: 1.0  
**Last Updated**: 2026-04-30

## SSE Event: state_update (Existing, Extended)

**Purpose**: Push updated coordinare state to all connected browser tabs (spec 010).

**File**: `src/coordinare/dashboard.py` → `DashboardStore.snapshot()`

### Event: Every Poll Cycle (30s or on force-poll)

**Topic**: `/events` (Server-Sent Events)

### Payload Schema

```json
{
  "type": "state_update",
  "timestamp": "2026-04-30T15:30:45.123Z",
  "event_id": "evt-abc123",
  
  "coordinare": {
    "daemon_running": true,
    "cycle_active": false,
    "cycles_completed": 456,
    "last_cycle_duration_seconds": 2.34,
    "consecutive_error_count": 0,
    "daemon_start_time": "2026-04-30T08:00:00Z"
  },
  
  "symphonies": [
    {
      "name": "backend",
      "github_project_number": 43,
      "priority": 0,
      "state": {
        "last_poll_at": "2026-04-30T15:30:45Z",
        "cycle_count": 456,
        "error_count": 2,
        "last_error": "GitHub API rate limit (429)",
        "last_error_at": "2026-04-30T15:25:00Z",
        
        "active_card": {
          "id": "GH-42",
          "title": "Fix auth bug",
          "column": "IN_PROGRESS",
          "url": "https://github.com/orgs/acme-corp/projects/43?pane=issue&itemId=123",
          "assigned_to": "coordinare-bot"
        },
        
        "board_snapshot": {
          "TODO": 12,
          "IN_PROGRESS": 1,
          "IN_REVIEW": 3,
          "DONE": 45,
          "BLOCKED": 2,
          "BACKLOG": 8
        },
        
        "active_sessions": [
          {
            "session_id": "sess-abc123",
            "phase": "monitoring",
            "performer_id": "performer-1",
            "card_id": "GH-42",
            "started_at": "2026-04-30T15:25:00Z",
            "age_seconds": 345
          }
        ],
        
        "session_skip_reasons": {
          "GH-40": "Card has blocker: GH-39",
          "GH-41": "Assigned to different user"
        }
      }
    },
    {
      "name": "frontend",
      "github_project_number": 42,
      "priority": 1,
      "state": {
        "last_poll_at": "2026-04-30T15:30:30Z",
        "cycle_count": 455,
        "error_count": 0,
        "active_card": null,
        "board_snapshot": {
          "TODO": 8,
          "IN_PROGRESS": 0,
          "IN_REVIEW": 1,
          "DONE": 92,
          "BLOCKED": 0,
          "BACKLOG": 5
        },
        "active_sessions": [],
        "session_skip_reasons": {}
      }
    }
  ],
  
  "metrics": {
    "coordinare_cycles_completed_total": 456,
    "coordinare_errors_total": 2,
    "coordinare_active_sessions": 1,
    "coordinare_card_tokens_total": {
      "main-persona": 45000
    },
    "coordinare_card_cost_estimate_dollars": 1.23,
    "per_symphony_metrics": {
      "backend": {
        "cycles_completed": 456,
        "cards_processed": 234,
        "errors": 2,
        "agent_dispatch_seconds": 5.6
      },
      "frontend": {
        "cycles_completed": 455,
        "cards_processed": 210,
        "errors": 0,
        "agent_dispatch_seconds": 0
      }
    }
  },
  
  "performers": {
    "total_registered": 1,
    "total_idle": 1,
    "total_busy": 0,
    "total_excluded": 0,
    "list": [
      {
        "id": "performer-1",
        "mode": "subprocess",
        "availability": "idle",
        "endpoint": null,
        "current_job_id": null,
        "consecutive_failures": 0,
        "excluded_until_recovery": false,
        "last_status_at": "2026-04-30T15:30:40Z"
      }
    ]
  },
  
  "subsystems": {
    "github_client": {
      "status": "healthy",
      "checked_at": "2026-04-30T15:30:45Z"
    },
    "performer_pool": {
      "status": "healthy",
      "checked_at": "2026-04-30T15:30:45Z"
    },
    "notification_service": {
      "status": "degraded",
      "details": "Slack webhook rate limited",
      "checked_at": "2026-04-30T15:29:00Z"
    }
  },
  
  "health": {
    "overall_status": "degraded",
    "probes": [
      {
        "subsystem_name": "github_client",
        "status": "healthy",
        "is_required": true,
        "checked_at": "2026-04-30T15:30:45Z"
      },
      {
        "subsystem_name": "notification_service",
        "status": "degraded",
        "is_required": false,
        "checked_at": "2026-04-30T15:29:00Z",
        "details": "Slack webhook rate limited"
      }
    ]
  }
}
```

### Key Fields for Symphony Management (NEW)

#### `symphonies: Array[SymphonyState]`

Each symphony gets its own state object:
- `name: string` — symphony identifier
- `github_project_number: number` — board reference
- `priority: number` — index in symphony list (0 = highest)
- `state: SymphonyRuntimeState` — all per-symphony metrics and active work

#### `metrics.per_symphony_metrics: Dict[string, PerSymphonyMetrics]`

Prometheus metrics partitioned by symphony:
- `cycles_completed: number` — cycles conducted for this symphony only
- `cards_processed: number` — cards completed for this symphony
- `errors: number` — errors specific to this symphony
- `agent_dispatch_seconds: number` — time to dispatch agent for this symphony

#### `coordinare.cycle_active: boolean`

When true, a poll cycle is in progress (may be within a specific symphony or global step).

---

## Dashboard UI Pages (New)

### Page: `/symphonies`

**Navigation**: Main menu → "Symphonies"

**View**: List of all symphonies with:
- Symphony name and board link
- Current active card (if any)
- Board column snapshot (pie chart or bar)
- Recent errors (last 3 with timestamps)
- Cycle count and age (time since last poll)

**Actions** (Per-Symphony):
- Click symphony name → detail view
- (Future) Pause / resume orchestration for this symphony
- (Future) Force-poll this symphony only

### Page: `/symphonies/{name}`

**Navigation**: Click symphony name from list

**View**: Detailed symphony dashboard
- Board snapshot (columns with card counts)
- Active card (if any) with performer details
- Recent activity timeline (last 10 events for this symphony)
- Error history (last 5 errors, sorted by date)
- Configuration (effective config for this symphony, read-only)
- Personas (active personas for this symphony)

**Actions**:
- (New) Edit Symphony Config
- (Future) Force-poll this symphony
- (Future) Skip next N cycles for this symphony

### Page: `/admin/config`

**Navigation**: Main menu → "Admin" → "Configuration"

**View**: Current configuration
- Global Config section
- Symphonies list with edit buttons
- Orchestra configuration
- Config file path and last reload time

**Actions**:
- Edit global config (modal form)
- Add new symphony (modal form)
- Edit symphony overrides (modal form)
- Delete symphony (with confirmation)
- **Reload Config** button (triggers hot-reload)

**Forms** (for editing symphonies):
- Symphony name (required, alphanumeric + dash)
- GitHub project number (required, integer)
- Overrides (optional, key-value pairs for ProjectConfiguration fields)
- Personas (optional, JSON editor)

---

## Validation Rules (Client-Side UX Hints)

When user edits symphony config via dashboard form:

1. **Symphony Name**
   - Must match regex: `^[a-z0-9][a-z0-9\-]*[a-z0-9]$`
   - Must be unique (check against existing symphonies)
   - Show error immediately if invalid

2. **GitHub Project Number**
   - Must be positive integer
   - Must be unique (check against existing symphonies)
   - UI: warn if coordinare cannot access the board (optional cross-check with API)

3. **Overrides (JSON)**
   - Must be valid JSON
   - Must be valid ProjectConfiguration subset (keys only from global config)
   - Warn if overriding global settings that affect all symphonies (e.g., github_token)

4. **Personas (JSON)**
   - Must be valid JSON
   - Must match PersonaOverride schema

---

## Real-Time Updates (SSE)

Every poll cycle (~30s), the browser receives a new `state_update` event:

1. **Browser connects**: `EventSource("/events")`
2. **Daemon completes cycle**: Calls `SSEBroadcaster.broadcast(snapshot)`
3. **Browser receives**: Updates UI with latest state
4. **User sees**: Live refresh of active cards, board snapshots, metrics

### Stale Indication

If no `state_update` received for > 60 seconds:
- Show "disconnected" banner at top
- Dim the UI
- Offer "Reconnect" button (refreshes page)

### Per-Symphony Update Example

When cycle completes for "backend" symphony:
1. `state_update` event includes `symphonies[0].state.last_poll_at = now()`
2. Frontend detects `last_poll_at` changed
3. Refreshes "backend" detail view (if user is viewing it)
4. Updates board snapshot, error count, etc.

---

## Error Handling (Client-Side)

### Validation Error (400 Bad Request)

```json
{
  "error": "validation_failed",
  "details": "Invalid configuration",
  "validation_errors": [
    {
      "field": "symphonies[0].name",
      "message": "must match pattern ^[a-z0-9][a-z0-9-]*[a-z0-9]$"
    }
  ]
}
```

UI Response:
- Show error modal with list of issues
- Highlight invalid fields in form
- Disable "Save" button

### Conflict Error (409 Conflict)

```json
{
  "error": "cycle_in_progress",
  "details": "Cannot update during orchestration cycle",
  "current_symphony": "backend",
  "retry_after_seconds": 30
}
```

UI Response:
- Show modal with message
- Offer "Retry After Cycle Completes" (waits 35s, retries)
- Or "Cancel" (user edits later)

