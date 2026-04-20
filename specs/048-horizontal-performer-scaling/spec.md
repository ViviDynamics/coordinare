# Feature Specification: Horizontal Performer Scaling

**Feature Branch**: `048-horizontal-performer-scaling`  
**Created**: 2026-04-16  
**Status**: Draft  
**Input**: Per-role configurable max concurrency for performers. Assessor and closer are singletons (max 1). Other roles scale out to serve multiple cards simultaneously. Coordinare manages performer start/stop based on slot availability.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Multiple cards served by the same role concurrently (Priority: P1)

When the coordinare has three cards in the implementing stage and the implementer role is configured with `max_concurrency: 3`, three separate performer instances run simultaneously — each working on a different card's branch. As one finishes and advances to the reviewer stage, its implementer slot frees up for the next TODO card.

**Why this priority**: This is the core throughput multiplier. Without horizontal scaling, cards queue behind each other at every stage. A single reviewer blocking for 10 minutes on card A delays card B even if a second reviewer could be running.

**Independent Test**: Configure `implementer.max_concurrency: 2`. Place three cards in TODO. Verify two implementer performers launch simultaneously for the first two cards. When one completes, verify the third card's implementer starts within one poll cycle.

**Acceptance Scenarios**:

1. **Given** `implementer.max_concurrency: 2` and three cards dispatched to the implementing stage, **When** the coordinare schedules performers, **Then** two implementer performers run concurrently and the third waits.
2. **Given** one of two running implementer performers finishes, **When** the coordinare polls on the next cycle, **Then** the waiting card's implementer is dispatched into the freed slot.
3. **Given** `reviewer.max_concurrency: 1` (default), **When** two cards reach the reviewing stage, **Then** only one reviewer runs at a time; the second waits until the first completes.

---

### User Story 2 — Assessor and closer remain singletons (Priority: P2)

Regardless of the configured concurrency for other roles, the assessor and closer always run one at a time. The assessor needs a consistent view of the full board to detect dependencies (spec 046). The closer needs exclusive access to the merge flow to avoid race conditions on the default branch.

**Why this priority**: Assessor and closer have ordering and consistency constraints that make parallelism unsafe. Violating this could cause two cards to merge simultaneously (closer race) or miss a dependency because the assessor saw a stale board snapshot (assessor race).

**Independent Test**: Configure `assessor.max_concurrency: 5` in config. Verify the coordinare ignores the override and runs at most one assessor at a time. Place three cards in TODO — verify they are assessed sequentially.

**Acceptance Scenarios**:

1. **Given** `assessor.max_concurrency: 5` in config and three cards pending assessment, **When** the coordinare schedules, **Then** only one assessor runs at a time; the other two wait.
2. **Given** two cards reach the closing_review stage simultaneously, **When** the coordinare schedules closers, **Then** only one closer runs; the second waits for the first to finish merging.
3. **Given** all other roles have `max_concurrency: 3`, **When** the operator checks the dashboard, **Then** assessor and closer always show max 1 in the utilization view.

---

### User Story 3 — Dashboard shows performer utilization (Priority: P3)

The dashboard displays a per-role utilization view: how many instances of each role are currently active vs. the configured maximum. When a role is at capacity, the queued-card count is shown so the operator can decide whether to increase the limit.

**Why this priority**: Scaling knobs are useless without visibility into whether they're constraining throughput. The operator needs to see "implementer: 2/3 active, 1 queued" to decide whether to raise the limit or if the current config is sufficient.

**Independent Test**: Configure three roles with different max_concurrency values. Dispatch cards so at least one role is at capacity with a card waiting. Verify the dashboard shows active/max/queued per role.

**Acceptance Scenarios**:

1. **Given** `implementer.max_concurrency: 2` with 2 active and 1 queued, **When** the operator views the dashboard, **Then** the implementer row shows "2 / 2 active, 1 queued".
2. **Given** `reviewer.max_concurrency: 1` with 1 active and 0 queued, **When** the operator views the dashboard, **Then** the reviewer row shows "1 / 1 active".
3. **Given** a role with 0 active performers (all slots idle), **When** the operator views the dashboard, **Then** the role shows "0 / N active" with no queued count.

---

### Edge Cases

- **Performer crash at capacity**: If one of 3 running implementers crashes, the slot should be freed on the next cycle and either the crashed card is retried (system_error path) or a waiting card takes the slot.
- **Config hot-reload reduces max_concurrency**: If the operator reduces `implementer.max_concurrency` from 3 to 1 while 3 are running, the coordinare should NOT kill running performers. It simply stops dispatching new ones until the active count drops below the new limit.
- **All slots full across all roles**: If every configured role is at capacity, new TODO cards stay in the queue. The coordinare does not error — it simply reports "all slots occupied" in the dashboard.
- **Transport failure on one instance**: A transport error on one performer instance does not affect others sharing the same role. Each instance has its own transport/process.
- **Session timeout at scale**: Each active session has its own AGENT_TIMEOUT countdown. One session timing out does not affect others.
- **max_concurrency: 0**: Treat as "role disabled" — equivalent to not listing the role in the performers section.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Each performer role MUST support a configurable `max_concurrency` value (default 1) that limits how many instances of that role can run simultaneously.
- **FR-002**: The assessor and closer roles MUST be hard-capped at `max_concurrency: 1` regardless of configuration.
- **FR-003**: The coordinare MUST maintain a per-role slot counter tracking how many instances are currently active. When a dispatch is needed and all slots for the target role are occupied, the card waits in a queue.
- **FR-004**: When a performer finishes (any terminal status) or crashes, its slot MUST be freed within one poll cycle.
- **FR-005**: The coordinare MUST NOT kill running performers when `max_concurrency` is reduced via config hot-reload. It simply stops dispatching new instances until the count drops below the new limit.
- **FR-006**: Each performer instance MUST have its own independent transport (subprocess, SSH, or Kubernetes). Instances of the same role do not share a process.
- **FR-007**: `max_concurrency: 0` MUST disable the role entirely (equivalent to removing it from the performers section).
- **FR-008**: The dashboard MUST show per-role utilization: active instances / configured max, and queued-card count when at capacity.
- **FR-009**: The coordinare MUST track each running instance with its card ID, session ID, and start time for observability and timeout enforcement.
- **FR-010**: Multi-card mode (`max_concurrent_cards`) becomes the baseline operating mode. Single-card mode remains supported as `max_concurrent_cards: 1`.

### Key Entities

- **PerformerSlot**: Represents one active performer instance. Contains: role name, card ID, session ID, transport reference, start timestamp, current state (running / stopping / crashed).
- **RolePool**: The set of slots for a single role. Contains: role name, max_concurrency, list of active PerformerSlots, queue of waiting card IDs.
- **SlotManager**: Orchestrates all RolePools. Allocates slots on dispatch, frees them on completion/crash, enforces singleton constraints on assessor/closer.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With `implementer.max_concurrency: 3`, three cards in the implementing stage are worked on simultaneously — total elapsed time is less than 1.5x the time for a single card (not 3x).
- **SC-002**: Assessor and closer never run more than one instance regardless of configuration. Zero race conditions on board assessment or merge flow.
- **SC-003**: When a performer slot frees up, the next queued card is dispatched within one poll cycle (under 30 seconds).
- **SC-004**: Operator can see at a glance which roles are saturated and how many cards are queued per role on the dashboard.
- **SC-005**: Reducing `max_concurrency` via config change does not interrupt running performers — existing sessions complete normally.

## Assumptions

- The existing multi-card infrastructure from spec 035 (`active_sessions`, `max_concurrent_cards`) is the foundation. This spec extends it with per-role concurrency rather than replacing it.
- Transport instances (subprocess, SSH, Kubernetes) are cheap to create and destroy. The coordinare spawns them on demand rather than pre-allocating a pool of warm instances.
- Cost and token-budget tracking (spec 034) still applies per-card. Scaling up performers does not change the per-card budget — it increases total throughput cost proportionally.
- The default `max_concurrency: 1` for all roles preserves backward compatibility. Existing single-card deployments see no behavior change.
