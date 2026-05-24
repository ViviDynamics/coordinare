# Data Model: Block-Notification Dedup on State Rehydration

This feature touches three data shapes. Only `PersistedSession` changes its
serialized form; the other two are in-memory derivations.

## 1. `PersistedSession` (src/coordinare/state_store.py)

### Delta

Add one optional field:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `last_blocked_notified_at` | `datetime \| None` | `None` | ISO-8601 in JSON. Pydantic v2 serializer reuses the same datetime handling as `WorkflowSnapshot.last_blocked_notified_at`. |

### Validation rules

- Always optional. v1 snapshots (which lack the field) deserialize with `None`.
- When written by `state_store.persist()`, the value is copied verbatim from
  the live `CardSession.last_blocked_notified_at`. No coercion.

### State transitions

`PersistedSession.last_blocked_notified_at` is set:

1. To `None` on session creation (matches `create_session_from_card()`).
2. To `now` whenever `handle_blocked` advances the watermark
   (`handle_blocked.py:207` — currently writes top-level; this feature
   also writes session-level).
3. Carried verbatim through every snapshot round-trip.

It is never cleared except by full session removal.

## 2. `CardSession` (src/coordinare/session.py) — no schema change

The field already exists at `session.py:59` and is listed in `_SESSION_FIELDS`
(`session.py:99`). The only verification needed is that the `_SESSION_FIELDS`
loop continues to round-trip the value between flat state and session dict —
which it already does. **No code change** in `session.py` is required;
this is documented here so reviewers know to *confirm*, not modify.

## 3. Notification dedup key (src/coordinare/graph/nodes/notify.py)

### Current shape (notify.py:133)

```python
dedup_key = f"{event_type.value}:{card_id}:{status}:{performer_stage}"
```

### New shape

```python
questions_hash = ""
if event_type == EventType.card_blocked and open_questions:
    questions_hash = hashlib.sha256(
        "\n".join(open_questions).encode("utf-8")
    ).hexdigest()[:12]
dedup_key = f"{event_type.value}:{card_id}:{status}:{performer_stage}:{questions_hash}"
```

### Validation rules

- `questions_hash` is empty for non-blocked events (key shape preserved
  modulo trailing `:` — alternatively, omit the segment entirely for non-blocked
  events; either choice is implementation detail as long as existing dedup
  behavior for other event types is unchanged).
- For `card_blocked`, `questions_hash` is empty when (and only when)
  `open_questions == []`. In that case the event is suppressed before the
  key is consulted, so the empty-hash key is never written to the dedup
  cache.

## 4. In-memory active-session lookup (no new model)

The check in `notify` consumes the existing
`state["active_sessions"][card_id]` dict — no new entity. Phases consulted:

| Phase | Suppress `card_blocked`? |
|-------|--------------------------|
| `dispatching` | YES (fresh dispatch supersedes) |
| `monitoring_performer` | YES |
| `monitoring_agent` | YES |
| `blocked` | NO (this is the legitimate path) |
| `finalized` / `failed` / `closed` | NO (terminal — no fresh dispatch) |

This table is the authoritative reference for the FR-005 implementation.
