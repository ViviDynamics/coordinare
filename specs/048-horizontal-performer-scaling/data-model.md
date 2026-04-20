# Data Model: Horizontal Performer Scaling

## Entities

### PerformerSlot

One active performer instance serving a card.

| Field | Type | Description |
|-------|------|-------------|
| role | str | Performer stage name (e.g. "implementing", "reviewing") |
| card_id | str | Project item ID (PVTI_...) of the card being served |
| session_id | str | Session ID from the dispatch response |
| service_index | int | Index into the role's service list (which transport instance) |
| started_at | datetime | When this slot was allocated |
| state | SlotState | Current state of the performer instance |

### SlotState (enum)

| Value | Description |
|-------|-------------|
| RUNNING | Performer is actively working (monitoring_performer phase) |
| STOPPING | Performer returned a terminal status; slot pending cleanup |
| CRASHED | Transport error or session timeout; slot pending cleanup |

### RolePool

Tracks slots for a single performer role.

| Field | Type | Description |
|-------|------|-------------|
| role | str | Performer stage name |
| max_concurrency | int | Configured maximum concurrent instances (clamped to 1 for singletons) |
| services | list[AgentService] | Pre-built service instances (one per max_concurrency slot) |
| active_slots | dict[str, PerformerSlot] | card_id → active slot mapping |

### SlotManager

Orchestrates all RolePools.

| Field | Type | Description |
|-------|------|-------------|
| pools | dict[str, RolePool] | stage_name → RolePool |

## Config Extension

### PerformerRoleConfig additions

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| max_concurrency | int | 1 | Maximum concurrent instances of this role. Clamped to 1 for assessor/closer. 0 disables the role. |

## State Extensions

### CoordinareState additions

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| slot_manager | SlotManager or None | None | Reference to the SlotManager instance. Set during daemon bootstrap. |

## Methods

### SlotManager

| Method | Returns | Description |
|--------|---------|-------------|
| `acquire(stage, card_id)` | `AgentService or None` | Allocate a free slot for the card. Returns None if at capacity. |
| `release(stage, card_id)` | `None` | Free the slot after performer completes or crashes. |
| `active_count(stage)` | `int` | Number of currently active slots for a role. |
| `is_at_capacity(stage)` | `bool` | True if active_count >= max_concurrency. |
| `utilization()` | `list[dict]` | Per-role `{role, active, max, queued}` for dashboard. |
| `sync_from_sessions(active_sessions)` | `None` | Rebuild active slots from session phases (stateless reconciliation). |

## State Transitions

```
PerformerSlot lifecycle:

  [dispatch requested]
       │
       ├── slot available? → acquire() → RUNNING
       │                          │
       │                          ├── performer completes → release() → slot freed
       │                          ├── performer errors → release() → slot freed
       │                          └── session timeout → release() → slot freed
       │
       └── at capacity? → card waits (phase stays "dispatching")
                          → retries next poll cycle
```

## Relationships

```
SlotManager
  └── pools: dict[str, RolePool]
         └── active_slots: dict[str, PerformerSlot]
                └── card_id → AgentService instance
```
