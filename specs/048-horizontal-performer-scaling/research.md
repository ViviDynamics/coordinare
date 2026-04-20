# Research: Horizontal Performer Scaling

## R1: Slot Tracking Strategy

**Decision**: In-memory SlotManager that derives active slot counts from `active_sessions` on each poll cycle. No persistent slot state — the SlotManager rebuilds its view from the sessions dict (same stateless-rebuild pattern as the dependency graph in 046).

**Rationale**: Active sessions already track card→stage associations. The SlotManager counts how many sessions are in each stage's `monitoring_performer` phase and compares against `max_concurrency`. No new persistence needed.

**Alternatives considered**:
- Persistent slot database: Over-engineered for a single-daemon system. Sessions are already in memory.
- Pre-allocated transport pool: Wastes resources when roles are idle. On-demand creation is cheap for subprocess transports.

## R2: Transport Multiplexing

**Decision**: Change `performer_services` from `dict[str, AgentService]` to `dict[str, list[AgentService]]`. At startup, create `max_concurrency` AgentService instances per role (each wrapping its own transport). The SlotManager assigns a free service instance to each card on dispatch.

**Rationale**: Each AgentService owns one subprocess transport. Multiple cards need independent transports to avoid stdin/stdout interleaving. Creating N services at startup is simple and matches the existing `_build_transport_for_role` pattern.

**Alternatives considered**:
- Single service with transport pooling: Requires AgentService to manage multiple transports internally, breaking single responsibility.
- Create transports on demand at dispatch time: More complex lifecycle management. Pre-creating at startup with a fixed pool is simpler and the count is bounded by config.

## R3: Singleton Enforcement

**Decision**: Hard-code `SINGLETON_STAGES = {"assessing", "closing_review"}` in `lifecycle.py`. The SlotManager clamps `max_concurrency` to 1 for these roles regardless of config. Log a warning if config specifies a higher value.

**Rationale**: Assessor needs a consistent board view (spec 046 dependency detection), and closer needs exclusive merge access. These constraints are fundamental, not configurable.

**Alternatives considered**:
- Config validation (reject >1 for singletons): Too strict — prevents experimentation. Clamping with a warning is friendlier.
- Per-role flag in PerformerRoleConfig: Unnecessary indirection. The singleton set is static and small.

## R4: Dispatch Integration

**Decision**: `dispatch_performer` calls `slot_manager.acquire(stage, card_id)` which returns an AgentService if a slot is free, or None if at capacity. On None, the card's phase stays at `dispatching` and it retries next cycle. On success, the SlotManager records the (stage, card_id, service_index) mapping.

**Rationale**: Minimal change to dispatch_performer — one function call replaces the dict lookup. The "retry next cycle" behavior is already how the coordinare handles transient failures.

**Alternatives considered**:
- Queuing mechanism with explicit wait-list: Adds complexity. The existing poll-cycle retry is equivalent to a queue with FIFO ordering.
- Priority-based slot allocation: Premature optimization. The priority field from spec 025 already orders the TODO queue; slot allocation just needs first-come-first-served within a stage.

## R5: Config Hot-Reload

**Decision**: On each dispatch, the SlotManager reads the current `max_concurrency` from config (hot-reloaded). If the limit decreased, it doesn't kill running performers — it simply refuses to allocate new slots until the active count drops below the new limit. The slot count is not persisted; it's always derived from active sessions.

**Rationale**: Config hot-reload is already supported for personas (spec 018). Applying the same pattern to concurrency limits is natural. Killing running performers would waste LLM tokens and leave cards in inconsistent states.

## R6: Dashboard Utilization

**Decision**: Add `role_utilization` to the dashboard snapshot: a list of `{role, active, max, queued}` dicts. The dashboard JS renders this as a table or bar chart near the active-performers section.

**Rationale**: Follows the same snapshot → SSE → JS rendering pattern used for `blocked_by_dependencies` (046) and `last_rebase_round` (047).
