# Contract: Activity Event Stream & ActivityLog API

Two contracts: the **wire contract** between daemon and browser over SSE, and the **Python API** between `ActivityLog` and its callers. Both have contract tests per Constitution Principle II.

---

## 1. Wire contract — `GET /events`

Unchanged endpoint, unchanged transport, one new event name.

### 1.1 Event kinds

| Event name | Status | Payload |
|---|---|---|
| `state_update` | **UNCHANGED** | Full `DashboardState` snapshot from `build_snapshot()` |
| `activity_event` | **NEW** | Append-only batch of activity entries |
| `heartbeat` | **NEW** | Liveness ping, empty object |
| `: keepalive` | **UNCHANGED** | SSE comment, byte-identical to today |

### 1.2 `activity_event` payload

```json
{
  "_event": "activity_event",
  "entries": [
    {
      "seq": 1487,
      "timestamp": "2026-07-30T14:22:07.412000+00:00",
      "card_id": "PVTI_lADO...",
      "card_number": 142,
      "card_title": "Add retry budget to the closer",
      "stage": "implementer",
      "activity_type": "tool_use",
      "text": "Edit src/coordinare/services/notify.py",
      "truncated": false
    }
  ]
}
```

**Field guarantees**

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `seq` | integer | no | Strictly increasing within a run; the ordering key |
| `timestamp` | ISO-8601 string, UTC, tz-aware | no | Display only — never the sort key |
| `card_id` | string | no | `""` when unattributable, never `null` (FR-008) |
| `card_number` | integer | **yes** | `null` when unknown |
| `card_title` | string | no | ≤ 80 chars |
| `stage` | string | no | `""` when unknown |
| `activity_type` | string enum | no | One of the ten values in data-model.md |
| `text` | string | no | ≤ 200 chars |
| `truncated` | boolean | no | `true` when `text` was cut |

**Invariants**

1. `entries` is ordered **oldest-first** on the wire; the client reverses for display. Batches append cleanly without re-sorting.
2. `entries` is never empty — no event is emitted with nothing to say.
3. `_event` is reserved. A `state_update` payload never contains it, which is what makes the discriminator safe (research R2).
4. An entry with a given `seq` is transmitted **at most once** per connection. Reconnect is the sole exception: backfill may resend entries the client already had, and the client de-duplicates on `seq`.

### 1.3 Connect sequence

```
client                          server
  │──── GET /events ──────────────►│
  │◄─── event: state_update ───────│   (unchanged; always first)
  │◄─── event: activity_event ─────│   (NEW: backfill, up to 2000 entries, oldest-first)
  │                                 │
  │◄─── event: activity_event ─────│   (live, as entries are recorded)
  │◄─── event: state_update ────────│   (live, unchanged cadence)
  │                                 │
  │◄─── : keepalive ────────────────│   (on 15 s idle — byte-identical to today)
  │◄─── event: heartbeat ───────────│   (NEW: immediately after the comment)
```

**Ordering requirements**

- `state_update` MUST remain the first message on connect (preserves existing client behavior and FR-002).
- Backfill MUST follow it (FR-003).
- On idle timeout the keepalive comment MUST be emitted **before** the heartbeat. This is load-bearing: `tests/unit/test_dashboard.py:890` reads exactly one message after the snapshot and asserts `": keepalive\n\n"`. Reversing the order breaks that test and SC-007 (research R4).

### 1.4 `heartbeat` payload

```json
{"_event": "heartbeat"}
```

Exists solely to give the browser a JS-visible liveness signal. SSE comments are consumed by the browser's parser and never surface to `EventSource` listeners, so the comment alone cannot drive client-side silence detection.

**Client rule**: any received message — `state_update`, `activity_event`, or `heartbeat` — resets the silence timer. Exceeding ~2.5 keepalive intervals (≈ 40 s) marks the feed not-live (FR-026), reusing the existing `#nav-sse-dot` and `#disconnected-banner` state (`dashboard.py:3479-3504`). Retained entries stay visible and are marked possibly-stale (FR-027).

### 1.5 Backpressure

Unchanged from today. `SSEBroadcaster.broadcast` uses `put_nowait` inside `contextlib.suppress(asyncio.QueueFull)` (`dashboard.py:201-203`), so a slow client drops messages rather than stalling the daemon (FR-004). A client that drops an `activity_event` recovers on its next reconnect via backfill.

### 1.6 Contract tests

| Test | Asserts |
|---|---|
| `test_state_update_shape_additive_only` | Every pre-138 snapshot key present with unchanged type and semantics; none removed, renamed, or repurposed. Exactly one key is added — `activity_quiet_threshold_seconds` — and the test names it explicitly, so a *second* unplanned addition fails (FR-002, SC-007) |
| `test_state_update_has_no_underscore_event_key` | Discriminator can never collide |
| `test_activity_event_emitted_with_entries` | Event name and payload schema |
| `test_backfill_follows_initial_state_update` | Connect ordering (FR-003) |
| `test_keepalive_precedes_heartbeat` | Existing keepalive assertion still holds (SC-007) |
| `test_entries_ordered_oldest_first` | Wire ordering |
| `test_slow_client_drops_without_blocking` | FR-004 |

---

## 2. Python API — `ActivityLog`

`src/coordinare/services/activity_log.py`

### 2.1 Surface

```python
class ActivityLog:
    def __init__(self, *, maxlen: int = 2000, max_text: int = 200,
                 max_seen_per_card: int = 256) -> None: ...

    sink: Callable[[list[ActivityEntry]], None] | None
    """Live fan-out, set once by DashboardStore to broadcaster.broadcast_activity.
    Called by record/record_many with the entries actually appended, never with an
    empty list. Exceptions are suppressed — a broken transport must not break
    recording. None outside the dashboard (tests, non-dashboard embeddings)."""

    def record(self, *, activity_type: str, card_id: str = "",
               card_number: int | None = None, card_title: str = "",
               stage: str = "", text: str = "") -> ActivityEntry | None:
        """Append one entry. Returns None if suppressed as a duplicate."""

    def record_many(self, items: Iterable[Mapping[str, Any]]) -> list[ActivityEntry]:
        """Batch form. Returns only entries actually appended."""

    def snapshot(self, limit: int | None = None) -> list[dict]:
        """Oldest-first serialised entries, for backfill (matches wire order)."""

    def forget_card(self, card_id: str) -> None:
        """Release per-card dedup bookkeeping. Entries are retained."""
```

### 2.2 Behavioural guarantees

| # | Guarantee | Requirement |
|---|---|---|
| G1 | `record` returns `None` and appends nothing for an already-seen `(type, card_id, stage, text)` | FR-022 |
| G2 | `text` is truncated to `max_text` before the key is built, so key size is bounded | FR-035, FR-036 |
| G3 | Truncation preserves the prefix and sets `truncated=True` | FR-037 |
| G4 | `len(entries) <= maxlen` always; oldest evicted first | FR-019, FR-021 |
| G5 | `seq` strictly increases and is never reused within a run | FR-007 |
| G6 | Per-card seen-set never exceeds `max_seen_per_card` | FR-024 |
| G7 | A new distinct event still appends after seen-set eviction | FR-023 |
| G8 | `forget_card` removes bookkeeping only, never entries | FR-024 |
| G9 | Missing or empty attribution never raises; entry is still appended | FR-008 |
| G10 | No method raises on malformed input — the feed never breaks the dashboard | FR-008 |
| G11 | `sink` is called with exactly the entries appended, never with an empty list, and a raising sink neither propagates nor prevents the append | FR-001, FR-004 |

### 2.3 Caller contract

| Caller | Site | Type | Gate change |
|---|---|---|---|
| Monitor (pushed) | `monitor_performer.py:3232-3233`, where `new_events` is already in hand | `progress`, `tool_use`, `thinking`, `cost` — one `record_many` per poll | none |
| Watcher (derived) | `dashboard.py` `_watch_active_sessions` | `stage_change`, plus `forget_card` on departure | none |
| Daemon (pushed) | `daemon.py:3149` | `stuck` | **Drop the dead `and notification_service is not None` at `:3137`; add an inner `is not None` guard around the dispatch; move the cooldown stamp out of the `try` (`:3177`)** |
| Monitor (pushed) | `monitor_performer.py:3560` | `stall` | none — already ungated |
| Monitor (pushed) | `monitor_performer.py` terminal error / blocked | `error` | none |
| Board check (pushed) | `check_board.py:534` | `recovered` | none — **record before the existing `if notif is not None` branch** |

**Agent events are pushed, not derived.** The coordinare cannot learn of agent activity before it polls the performer, and the per-session fanout invokes the graph on a *copy* of state (`daemon.py:1501-1513`, merged back at `:1528`), so a watcher reading `daemon.state` sees events only at writeback — after the rest of the cycle. Recording at `:3232` emits in the same invocation that observed the batch, which is what makes SC-004 measurable. See research R1 for the superseded derived design and why it was reversed.

**Only one gate is removed, and it is dead code in the production wiring**: `build_notification_service` returns a service even with zero channels (`services/notification.py:315-342`), so detection at `:3137` already runs today. The removal matters only for the `None`-service path in tests and non-dashboard embeddings — which is precisely why the dispatch needs its own guard once the outer one is gone.

**Delivery semantics are unchanged where a channel exists.** Routing, dedup, and the cooldown *duration* are untouched, so a configured channel behaves as it does today (FR-011). Two things do change by design: the dispatch gains an inner `is not None` guard, and the cooldown stamp lands whether or not the dispatch ran — without that move, a `None` service raises into the `except` at `:3178`, the stamp never lands, and the feed takes a `stuck` entry every cycle (FR-013, SC-006).

Every call site MUST tolerate `state.get("activity_log")` returning `None` and no-op — tests and non-dashboard embeddings run without one.

### 2.4 Unit tests

One test per guarantee G1–G11, plus:

| Test | Asserts |
|---|---|
| `test_wedged_agent_reemits_nothing` | Re-recording an unchanged batch 100× appends zero entries (SC-009) — the headline behaviour |
| `test_memory_ceiling_under_flood` | 100k oversized records leave ≤ 2000 entries, each ≤ 200 chars (SC-013) |
| `test_timestamp_not_in_dedup_key` | Same event at two different times appends once — guards the invariant most at risk of being broken |
