# Contract: `notify` emission of `card_blocked`

This contract governs `src/coordinare/graph/nodes/notify.py`. It is enforced
by unit tests under `tests/unit/graph/nodes/test_notify.py`.

## Inputs

- `state`: `CoordinareState` dict, with at least:
  - `state["phase"]` — top-level phase string (may be `"blocked"`)
  - `state["open_questions"]` — `list[str]`
  - `state["active_sessions"]` — `dict[str, CardSession]`
  - `state["last_blocked_notified_at"]` — `datetime | None` (top-level mirror)
- `card`: current card dict, with `id`, `status`, etc.
- In-memory dedup cache on the notification service (already exists).

## Preconditions

The contract applies only when `notify` would otherwise emit
`EventType.card_blocked` (i.e., `phase == "blocked"`).

## Emission rules

`notify` MUST emit a `card_blocked` event **iff all** of:

1. `open_questions` is non-empty. *(FR-003 — empty list never emits)*
2. There is no active session for the card whose `phase` is one of
   `{"dispatching", "monitoring_performer", "monitoring_agent"}`. *(FR-005 —
   fresh dispatch supersedes)*
3. The dedup key
   `f"{event_type.value}:{card_id}:{status}:{performer_stage}:{questions_hash}"`
   is not present in the in-memory dedup cache. *(unchanged dedup behavior,
   extended key per FR-006)*
4. NOT both of: *(FR-004 — restart suppression)*
   - per-session `last_blocked_notified_at` is non-null, AND
   - the dedup cache is empty (no entry for *any* key matching
     `f"{EventType.card_blocked.value}:{card_id}:"`).

When all four hold, the event is dispatched and the dedup key is written to
the cache. When (4) is the only blocking condition, the dedup key is still
written to the cache (priming) but no dispatch occurs.

## Postconditions

- For exactly the inputs in User Story 2 (rehydrated `phase=blocked` plus an
  active `dispatching`/`monitoring_*` session), `notification_service.dispatch`
  is called zero times with `EventType.card_blocked`.
- For exactly the inputs in User Story 3 (empty `open_questions`),
  `notification_service.dispatch` is called zero times with
  `EventType.card_blocked`.
- For a card whose `open_questions` content changes between two ticks (e.g.,
  a new clarifying question is appended), a fresh `card_blocked` event IS
  emitted — the new `questions_hash` makes the dedup key unique. *(FR-006)*

## Removed behavior

The literal `"needs input"` fallback at the previous `notify.py:91` is removed.
There is no code path where `card_blocked` is dispatched with the summary
string `"needs input"`.
