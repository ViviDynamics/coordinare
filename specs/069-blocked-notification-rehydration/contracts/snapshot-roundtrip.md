# Contract: Snapshot round-trip for `last_blocked_notified_at`

This contract governs `src/coordinare/state_store.py` and the rehydration
path in `src/coordinare/daemon.py`. It is enforced by
`tests/unit/test_state_store.py` and `tests/unit/graph/nodes/test_handle_blocked.py`.

## Persist path

Given a live `CoordinareState` where
`state["active_sessions"][card_id].last_blocked_notified_at == T`:

- `state_store.persist(state)` MUST write a `PersistedSession` entry for
  `card_id` whose `last_blocked_notified_at == T`.
- For sessions where the field is `None`, the persisted value MUST also
  be `None` (or absent, equivalent under pydantic default).

## Rehydrate path

Given a snapshot whose `PersistedSession` for `card_id` carries
`last_blocked_notified_at == T`:

- After `daemon._rehydrate()`, the reconstructed
  `state["active_sessions"][card_id]["last_blocked_notified_at"]` MUST equal
  `T` (identity preserved to second precision; pydantic ISO-8601 round-trip).
- The reconstructed dict MUST be passed through the same `_set_current_card`
  / session-state mirror path established in spec 066, with no parallel
  writes to top-level scalar except via that path.

## v1 snapshot compatibility

Given a v1 snapshot whose `PersistedSession` entries lack
`last_blocked_notified_at` entirely:

- Deserialization MUST succeed without error.
- The reconstructed session's `last_blocked_notified_at` MUST be `None`.
- The reconstructed session MUST then pass through the v1 synthesis path in
  `daemon.py` (existing lines ~495–510) unchanged in all other respects.

## `handle_blocked` interaction

Given a rehydrated state where session-level `last_blocked_notified_at == T`
and `T` is within `blocked_reminder_hours`:

- `handle_blocked` MUST NOT repost the GitHub reminder comment on this tick.
  *(FR-002)*
- The session-level value is consulted before any top-level fallback. *(FR-001)*

Given a rehydrated state where session-level `last_blocked_notified_at == T`
and `T` is older than `blocked_reminder_hours`:

- `handle_blocked` MAY repost the reminder (existing behavior preserved for
  legitimate reminder cadence — see spec Risks: "Reminder cadence drift").
