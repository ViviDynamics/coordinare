# Phase 1 Data Model: Dashboard Activity Feed

No persisted state. Everything here is in-memory, per-process, and discarded on daemon restart (FR-019). **No `state_store.py` schema-version bump is required** — this feature adds nothing to `PersistedSession` or the snapshot on disk.

---

## ActivityEntry

One immutable record of one thing that happened to one card.

| Field | Type | Constraint | Requirement |
|---|---|---|---|
| `seq` | `int` | Monotonic, assigned by the log on append. Never reused within a run. | FR-007 |
| `timestamp` | `datetime` (UTC, tz-aware) | Set at ingest | FR-005 |
| `card_id` | `str` | May be `""` when unattributable; never `None` | FR-005, FR-008 |
| `card_number` | `int \| None` | Issue number when known | FR-005 |
| `card_title` | `str` | Truncated to 80 chars | FR-005, FR-036 |
| `stage` | `str` | Stage or persona; `""` when unknown | FR-005, FR-008 |
| `activity_type` | `ActivityType` | See enum below | FR-006 |
| `text` | `str` | Truncated to 200 chars at ingest | FR-035, FR-037 |
| `truncated` | `bool` | `True` when `text` was cut | FR-035 |

**Ordering (FR-007)**: `seq` is the sort key, not `timestamp`. Two entries created in the same clock tick still order deterministically, and the client never has to break a timestamp tie. Newest-first display is descending `seq`. Newest-first is a **display** rule, applied by the client; the server never reverses. `snapshot()` and every live batch travel oldest-first so the client can prepend each entry without re-sorting.

**Immutability**: entries are never mutated after append. Quiet state is *not* a field here — see Quiet State below.

**Serialisation**: `timestamp` is emitted as an ISO-8601 string through the existing `_json_default` helper (`dashboard.py`), consistent with every other datetime the dashboard sends.

---

## ActivityType

Closed set. Drives both meaning and CSS class selection (FR-006, FR-017).

| Value | Source | Reuses CSS class |
|---|---|---|
| `stage_change` | Watcher diff on `performer_stage` / `phase` | `.ev-progress` |
| `progress` | Pushed — `monitor_performer.py:3232`, source event `type == "progress"` | `.ev-progress` |
| `tool_use` | Pushed — `monitor_performer.py:3232`, source event `type == "tool_use"` | `.ev-tool_use` |
| `thinking` | Pushed — `monitor_performer.py:3232`, source event `type == "thinking"` | `.ev-thinking` |
| `cost` | Pushed — `monitor_performer.py:3232`, source event `type == "cost"` | `.ev-cost` |
| `quiet` | Client-derived (never stored) | new severity class |
| `stall` | Pushed — `monitor_performer.py:3560` | new severity class |
| `stuck` | Pushed — `daemon.py:3149` | new severity class |
| `recovered` | Pushed — `check_board.py:534` | `.ev-progress` |
| `error` | Pushed — terminal error / blocked | `.ev-error` |

Any new severity class must be defined as a CSS custom property alongside the existing `--color-ev-*` tokens (`dashboard.py:713-715`) and pass the R6 contrast check. Every entry also carries a visible text severity prefix, so type is never conveyed by colour alone (R5).

---

## ActivityLog

The bounded collection. Lives in `src/coordinare/services/activity_log.py`, owned by `DashboardStore`, and referenced from `CoordinareState["activity_log"]` so graph nodes can reach it.

### State

| Attribute | Type | Bound | Requirement |
|---|---|---|---|
| `_entries` | `deque[ActivityEntry]` | `maxlen=2000` | FR-019, FR-020, FR-021 |
| `_seen` | `dict[str, set[str]]` | ≤ 256 keys per card | FR-022, FR-024 |
| `_seen_order` | `dict[str, deque[str]]` | `maxlen=256` per card | FR-023 |
| `_next_seq` | `int` | Monotonic | FR-007 |
| `sink` | `Callable[[list[ActivityEntry]], None] \| None` | Set once by `DashboardStore`; `None` elsewhere | FR-001, FR-004 |

`deque(maxlen=2000)` gives FR-021 (oldest discarded first) for free — no eviction code to write or test beyond asserting the invariant.

### Operations

| Method | Behaviour |
|---|---|
| `record(...) -> ActivityEntry \| None` | Truncate, build key, check `_seen`. Returns `None` if already seen (FR-022); otherwise assigns `seq`, appends, records the key, returns the entry. |
| `record_many(...) -> list[ActivityEntry]` | Batch form; returns only the entries actually appended. |
| `snapshot(limit=None) -> list[dict]` | **Oldest-first** serialised entries, in `deque` order, for backfill (FR-003). Matches the wire contract's oldest-first batch ordering; the client reverses for display. `limit` returns the newest `limit` entries, still oldest-first. |
| `forget_card(card_id) -> None` | Drops that card's `_seen` / `_seen_order`. Entries are retained (FR-024). |

### Sink fan-out

`record` / `record_many` call `sink(appended)` with the entries actually appended, and never with an empty list. Exceptions from the sink are suppressed — a broken transport must not break recording, and `DashboardStore` sets the sink to `broadcaster.broadcast_activity`, which is already non-blocking (`put_nowait` under `suppress(QueueFull)`), so FR-004 is satisfied by the existing mechanism.

This exists because writers live in graph nodes (`monitor_performer`, `daemon`, `check_board`) that hold an `ActivityLog` but no `SSEBroadcaster`. Without the sink, every pushed entry — agent activity, stall, stuck, recovered, error — would reach an already-open browser only on its next reconnect backfill, which defeats FR-009/FR-010/FR-012 and SC-001 for the operator who is watching the dashboard right now. One callback set once beats threading a broadcaster through `CoordinareState` to four call sites.

### Dedup key

```
key = f"{activity_type}|{card_id}|{stage}|{text}"
```

Built from the already-truncated `text`, so the key is bounded. Stored verbatim — no digest (research R3).

**Not part of the key**: `timestamp` and `seq`. Including either would defeat suppression entirely, since a re-reported event arrives with a new observation time. This is the single most important invariant in the feature and the one most likely to be broken by a well-meaning edit.

**Pushed entries bypass dedup** by including a distinguishing token in `text` (elapsed seconds for stall/stuck). Their repetition is governed instead by the existing cooldown at the call site (FR-013), which is why FR-013 says to reuse that cooldown rather than add a second policy here.

### Invariants (each maps to a unit test)

1. `len(_entries) <= 2000` always — FR-019/021, SC-013
2. `len(entry.text) <= 200` for every entry — FR-035, SC-013
3. `len(_seen[card_id]) <= 256` for every card — FR-024
4. `seq` strictly increases; no duplicates — FR-007
5. Recording an identical event twice appends exactly once — FR-022, SC-009
6. After 256 distinct events for a card, a *new* event still appends — FR-023
7. `forget_card` never removes entries, only bookkeeping — FR-024

---

## Quiet State (derived, never stored)

Per FR-030, quiet is computed from retained entry timestamps and **never persisted as state**. It is evaluated **client-side**, in the feed renderer, on a display timer.

| Aspect | Rule |
|---|---|
| Input | Per active card: newest entry timestamp **and** the `agent_dispatch_at` already in the session summary (`dashboard.py:495-499`); `activity_quiet_threshold_seconds` from the snapshot |
| Anchor | `last_heard = max(newest_entry_timestamp, agent_dispatch_at)`, skipping whichever is absent. Both absent → not evaluable, card is not marked |
| Condition | Card is in `active_sessions` **and** `now - last_heard > threshold` |
| Clearing | Any new entry for that card (FR-032) |
| Presentation | Marker on the card plus one synthetic feed row per episode (FR-032) |
| Authority | None — never sent to the server, never counted as progress (FR-031, FR-033) |

**Why the dispatch-timestamp fallback (FR-028)**: without it, a card with no retained entry is never evaluable — and the case that produces exactly that is a **session restored after a daemon restart**. The feed starts empty (FR-018), and a card restored straight into `monitoring_performer` produces no stage transition for the watcher to observe, so it would have no anchor at all. That is the one card this feature exists to surface, after the restart most likely to have been caused by it. `agent_dispatch_at` is already in the snapshot and already persisted on the session, so the fallback costs one `max()` and no new field. It keeps FR-030 intact: still only timestamps the dashboard already receives, still zero new daemon detection.

**Why client-side**: it makes FR-031 and SC-012 true by construction. A computation that lives in the browser cannot terminate a turn, consume retry budget, or change a phase — there is no code path by which it could. Server-side evaluation would require reviewers to verify that absence of effect; here it is structural.

Because quiet rows are synthetic, they never enter `_entries`, so a persistently quiet card cannot advance the stored feed or consume retention — which is what FR-032 and FR-033 are protecting.

---

## Config addition

| Field | Location | Default | Constraint | Requirement |
|---|---|---|---|---|
| `quiet_threshold_seconds` | `StuckAlertConfig` (`config.py:305`) | `300` (5 min) | `ge=0`; `0` disables | FR-028, FR-029 |

Added to the existing `StuckAlertConfig` rather than a new block: it belongs to the same family of "this card is not moving" thresholds that already lives there, and one field does not justify a new config surface.

FR-029 requires the default to be shorter than the smallest applicable stuck threshold. Smallest is 1800 s (`threshold_seconds`); the monitoring-phase overrides are 3600 s. At 300 s the quiet marker fires **6× sooner** than the fastest stuck alert and **12× sooner** than the monitoring-phase alert that governs the reported scenario.

Exposed to the browser as one new key in `build_snapshot`.

---

## CoordinareState addition

| Field | Type | Requirement |
|---|---|---|
| `activity_log` | `ActivityLog \| None` | FR-009, FR-010, FR-012 |

Declared on `CoordinareState` (`graph/state.py:102`, beside `notification_service`) and populated in the initial state dict (`__main__.py:844`) from the `DashboardStore`-owned instance, passed into `_bootstrap_services` as a keyword argument from the call site at `:959`. Nodes read it with `state.get("activity_log")` and no-op when absent, so tests and any embedding that runs without a dashboard keep working unchanged.

---

## Relationships

```
CoordinareState ──"activity_log"──┐
                                 ▼
DashboardStore ──owns──────► ActivityLog ──holds──► deque[ActivityEntry]  (max 2000)
      │                          │  ▲               └──tracks──► seen keys per card (max 256)
      │                          │  └──record()/record_many()── watcher (stage_change)
      │                          │                              monitor_performer (events,
      │                          │                                stall, error)
      │                          │                              daemon (stuck)
      │                          │                              check_board (recovered)
      │                          └──sink()──┐
      │                                     ▼
      └──owns──► SSEBroadcaster ──fans out──► activity_event  (new)
                                            └─► state_update   (unchanged + 1 additive key)
```

Writers: `monitor_performer.py` (agent-event batches, stall, terminal error), `daemon.py` (stuck), `check_board.py` (recovered), and the watcher (`stage_change`, plus `forget_card` on departure).
Readers: `sse_stream` for on-connect backfill; the `sink` for live fan-out.
`DashboardStore` wires `activity_log.sink = broadcaster.broadcast_activity` once at construction — the only place the two halves meet.

Single-process and single-threaded under asyncio — all writes happen on the event loop, so no lock is required. This is the same concurrency assumption `DashboardStore.history` already relies on.
