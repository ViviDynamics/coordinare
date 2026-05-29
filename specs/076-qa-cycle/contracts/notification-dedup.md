# Contract — Notification Dedup Across Reconciliation

**Module:** `src/coordinare/graph/nodes/notify.py` (modified) + `src/coordinare/services/reconciliation.py` (calls into notify)
**FRs:** FR-010, SC-004

## Decision matrix

For each reconciliation outcome, the notification layer's behaviour for the `card_dispatched` event is:

| Reconciliation outcome | Emit `card_dispatched`? | Rationale |
|---|---|---|
| `ADOPTED` | **No** | The container was already dispatched in a prior daemon process; the operator already saw that notification |
| `REAPED_AND_REPLACED` | **Yes (exactly once)** | This IS a new dispatch from the operator's perspective; the prior container failed health check and is being replaced |
| `FRESH_DISPATCHED` (no prior container) | **Yes (exactly once)** | Normal first-time dispatch |
| `SKIPPED_PERSISTENT` | **No** (no new dispatch happened) | Persistent endpoints have no per-dispatch notification model |
| `ORPHAN_SWEPT` | **No** | No new dispatch; the swept container is just cleanup |

## Mechanism: pre-emission consultation

The notification path for `card_dispatched` MUST read the most recent `ReconciliationDecision` for the affected card from `state.reconciliation_decisions_last_startup` (data-model section 8) before emitting:

```python
def should_emit_card_dispatched(state: CoordinareState, card_id: str) -> bool:
    last_decision = (state.get("reconciliation_decisions_last_startup") or {}).get(card_id)
    if last_decision == "adopted":
        return False
    if last_decision == "skipped_persistent":
        return False
    return True
```

The `reconciliation_decisions_last_startup` field is cleared at the end of the FIRST poll cycle after startup (so it doesn't suppress legitimate notifications for cards dispatched mid-run).

## Dedup key for `card_dispatched` notifications (existing behaviour, retained)

Reuses the existing dedup mechanism in the notification channel config (`dedup_window_seconds`). This contract does not change the channel-level dedup; it only ensures the notification layer correctly chooses to emit-or-not at the source.

## Required log events

| Event | When | Fields |
|---|---|---|
| `notify.card_dispatched_suppressed` | Notification suppressed because of `ADOPTED` / `SKIPPED_PERSISTENT` decision | `card_id`, `reconciliation_decision` |
| `notify.card_dispatched_emitted` | Notification actually fired | `card_id`, `reason` (`new_dispatch` / `reap_and_replace` / `fresh_dispatched`) |

## Test coverage

- `tests/unit/graph/nodes/test_notify_dedup_076.py` — for each row in the decision matrix, assert `should_emit_card_dispatched` returns the expected boolean and the corresponding log event is emitted.
- SC-004 validation in `tests/integration/test_restart_no_duplicate_dispatch.py` — after 100 simulated restarts, count exactly 0 notification emissions from `ADOPTED` decisions.
