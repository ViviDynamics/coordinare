# SSE Snapshot Contract: Dashboard UX Redesign (059)

This feature makes no changes to backend API endpoints or the SSE snapshot schema. This document records the **consumed subset** of the existing SSE snapshot that the redesigned dashboard panels depend on, to serve as a stability contract during implementation.

## Event: `state_update`

The `/events` SSE stream emits `state_update` events. The dashboard JS listens and calls `applyState(data)`.

### Consumed fields (stability contract)

These fields MUST remain present and type-stable in the snapshot for this feature to function correctly. Any future backend change that removes or renames these fields will break the redesigned dashboard.

```jsonc
{
  // Global state
  "phase": "idle",                    // string — raw phase name
  "phase_label": "Idle",             // string — human-readable (server-formatted)

  // Active sessions (multi-card support)
  "active_sessions": [
    {
      "card_id": "string",
      "card_title": "string",
      "issue_number": 123,            // int | null
      "issue_url": "https://...",     // string | null
      "pr_url": "https://...",        // string | null — non-null = awaiting review
      "phase": "monitoring_agent",    // string — raw
      "performer_stage": "string",    // string — raw sub-stage
      "card_tokens_total": 0,         // int
      "card_cost_estimate": 0.0,      // float (USD)
      "agent_dispatch_at": "ISO8601", // string | null — session start time
      "container_id": "string"        // string | null
    }
  ],

  // Subsystem health
  "subsystems": [
    {
      "name": "string",
      "status": "healthy",           // "healthy" | "degraded" | "unavailable"
      "required": true,              // bool
      "checked_at": "ISO8601",
      "details": "string"            // string | null
    }
  ],

  // Cycle history (newest-first, max 20)
  "cycle_history": [
    {
      "timestamp": "ISO8601",
      "phase": "string",             // raw — must be passed through formatPhaseLabel()
      "duration_seconds": 1.234,
      "outcome": "success"           // "success" | "error"
    }
  ],

  // Metrics
  "cycles_completed": 0,
  "last_cycle_duration_seconds": null, // float | null
  "consecutive_error_count": 0,
  "daemon_start_time": "ISO8601",      // string | null

  // Board / idle state — keys are column names from the GitHub project board
  "board_summary": {
    "TODO": 0,
    "IN_PROGRESS": 0,
    "IN_REVIEW": 0,
    "DONE": 0,
    "BLOCKED": 0,
    "BACKLOG": 0
    // additional column keys allowed; JS sums all values via Object.values().reduce()
  },
  "last_poll_at": "ISO8601",           // string | null
  "assignee_filter": "string",         // string | null

  // Role utilization
  "role_utilization": [
    {
      "role": "string",
      "active": 0,
      "max": 0,
      "queued": 0
    }
  ],

  // Dependency blockers
  "blocked_by_dependencies": [
    {
      "issue_number": 0,
      "title": "string",
      "column": "string",
      "issue_url": "string"
    }
  ],

  // Performer log lines — flat list from the single active agent service
  "performer_logs": ["string"]            // array of recent log line strings (last ~20)
}
```

## No new endpoints

This feature introduces no new HTTP endpoints, no new SSE event types, and no changes to existing endpoint request/response schemas.

## Backward compatibility

The dashboard JS is designed to handle null/missing fields gracefully (defensive reads). The new panels follow the same defensive pattern: `s.active_sessions || []`, `s.board_summary || {}`, etc.
