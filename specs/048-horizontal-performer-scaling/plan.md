# Implementation Plan: Horizontal Performer Scaling

**Branch**: `048-horizontal-performer-scaling` | **Date**: 2026-04-18 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/048-horizontal-performer-scaling/spec.md`

## Summary

Add per-role configurable concurrency (`max_concurrency`) so multiple cards can be served by the same performer role simultaneously. The coordinare maintains a SlotManager that tracks active instances per role, dispatches to free slots, and frees slots on completion/crash. Assessor and closer are hard-capped at 1. The dashboard shows per-role utilization.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: structlog (logging), asyncio (stdlib), pydantic-settings (config)
**Storage**: N/A — slot state is in-memory, derived from active_sessions on each cycle
**Testing**: pytest + pytest-asyncio (existing infrastructure)
**Target Platform**: Linux/macOS server (coordinare daemon)
**Project Type**: Single project (src/coordinare/)
**Performance Goals**: Slot allocation + dispatch must complete in < 10ms per card. Transport instances are pre-built at startup (one per max_concurrency slot), so dispatch has no transport creation latency.
**Constraints**: Assessor and closer hard-capped at max_concurrency=1. Config hot-reload must not kill running performers. max_concurrency=0 disables the role.
**Scale/Scope**: Up to 10 concurrent cards, up to 5 instances per role.

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | New SlotManager module, single responsibility. Config extension via existing PerformerRoleConfig. |
| II. Testing Discipline | PASS | Unit tests for SlotManager, dispatch integration, singleton enforcement, config hot-reload. |
| III. User Experience Consistency | PASS | Dashboard utilization view follows existing per-card status pattern. |
| IV. Performance by Design | PASS | Slot allocation is O(1) per role (counter check). Transports pre-built at startup (one per max_concurrency slot). |
| V. Clarity Before Action | PASS | No NEEDS CLARIFICATION markers. Scope bounded to per-role concurrency. |

## Project Structure

### Source Code (repository root)

```text
src/coordinare/
├── services/
│   └── slot_manager.py      # NEW — SlotManager, RolePool, PerformerSlot
├── config.py                # MODIFY — add max_concurrency to PerformerRoleConfig
├── __main__.py              # MODIFY — build multiple transports per role, inject SlotManager
├── daemon.py                # MODIFY — use SlotManager for slot allocation in _invoke_multi_session
├── graph/nodes/
│   └── dispatch_performer.py  # MODIFY — resolve service from SlotManager instead of dict lookup
├── graph/state.py           # MODIFY — add slot_manager field
├── dashboard.py             # MODIFY — add per-role utilization to snapshot + JS
└── lifecycle.py             # MODIFY — add SINGLETON_STAGES constant

tests/unit/
├── services/
│   └── test_slot_manager.py # NEW — SlotManager tests
├── graph/nodes/
│   └── test_dispatch_performer.py  # MODIFY — multi-slot dispatch tests
└── test_dashboard.py        # MODIFY — utilization snapshot tests
```

**Structure Decision**: New `services/slot_manager.py` owns all slot tracking logic. Config extension is minimal (one new field). `performer_services` stays `dict[str, AgentService]` (primary per role for backward compat); full per-role service lists are stashed on `_build_performer_services._service_lists` and registered on the SlotManager. `dispatch_performer` resolves via `SlotManager.acquire` when available, falls back to the primary.

## Complexity Tracking

No constitution violations. No complexity justification needed.
