# Phase 1 Data Model: Unify Card Pickup Paths

**Spec**: [./spec.md](./spec.md) | **Plan**: [./plan.md](./plan.md)

No schema change. This document captures the post-066 invariants on the
existing `CoordinareState` `TypedDict` (`src/coordinare/graph/state.py`).

## CoordinareState — Post-066 Shape

| Field | Type | Role | Mutation surface |
|---|---|---|---|
| `active_sessions` | `dict[str, CardSession]` | **Authoritative** record of every card currently in flight | Mutated by `check_board`, per-session inner graph steps, daemon snapshot restore |
| `active_card_id` | `str \| None` | Index pointer into `active_sessions` for the card that owns the current step | Set when a per-session graph invocation begins; cleared at end of cycle |
| `current_card` | `dict \| None` | **Derived mirror** of `active_sessions[active_card_id]["current_card"]` | Mutated *only* by `_rederive_current_card`, called from end of `check_board`, end of each per-session step, and end of snapshot restore |
| `feedback_cycle_count` | `int` | Per-card counter, lives on the session entry | Read from session; the top-level field, if any, is derived |
| `total_feedback_cycles` | `int` | Monotonic global counter | Untouched by 066 |
| `triage_blocks` | `int` | Monotonic global counter | Untouched by 066 |
| `board_snapshot` | `dict` | Latest GitHub board read | Untouched by 066 |

## CardSession Shape

```text
{
    "current_card": {            # the card dict — id, status, title, etc.
        "id": str,
        "status": "TODO" | "IN_PROGRESS" | "IN_REVIEW" | "BLOCKED",
        "previous_status": str,
        ...
    },
    "performer_stage": "dispatching" | "monitoring_performer" | ...,
    "feedback_cycle_count": int,
    ...
}
```

## Status-aware Phase Derivation Table

The unified pickup function derives initial `(status, previous_status,
performer_stage)` from the card's GitHub column at pickup time:

| Card GH status | `current_card.status` | `current_card.previous_status` | initial `performer_stage` |
|---|---|---|---|
| TODO | `"IN_PROGRESS"` | `"TODO"` | `"dispatching"` |
| IN_PROGRESS | `"IN_PROGRESS"` | (preserved from snapshot, else `"IN_PROGRESS"`) | re-derived from session if rehydrated, else `"dispatching"` |
| IN_REVIEW | `"IN_REVIEW"` | `"IN_REVIEW"` | `"monitoring_pr"` |
| BLOCKED | (untouched — handled by un-block detection path) | n/a | n/a |

## Invariants

- **I1**: When `active_card_id` is non-None, it MUST be a key in `active_sessions`.
- **I2**: `current_card` is `None` ↔ `active_card_id` is `None`.
- **I3**: When non-None, `current_card == active_sessions[active_card_id]["current_card"]`. Tests assert this post-cycle.
- **I4**: `active_sessions` has at most `max_concurrent_cards` entries.
- **I5**: A v1 snapshot (only `current_card` set) is rehydrated to a single-entry `active_sessions` with `active_card_id = current_card.id`.
- **I6**: No code outside `_rederive_current_card` mutates `state["current_card"]`. Enforced by code review; a lint rule may be added in a follow-up.

## State Transitions Affected by 066

- **TODO → IN_PROGRESS pickup**: unchanged from operator's perspective; routed through unified path.
- **IN_REVIEW re-adopt on daemon restart**: unchanged from operator's perspective; same status-aware pickup.
- **Operator un-block**: `feedback_cycle_count` reset fires for every concurrency setting (FR-003); event `dispatcher.feedback_cycle_reset` payload unchanged.
- **Q&A answer detected**: previous_status mutation moves from the top-level mirror to the session entry; mirror is re-derived (FR-011); externally observable behaviour unchanged.

No new transitions; no removed transitions.
