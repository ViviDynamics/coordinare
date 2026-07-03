# Data Model: 123-pipeline-flow-optimizations

## New / Modified Fields on `PersistedSession`

All changes are additive. No existing fields are removed or renamed.

### `content_feedback_cycles: int`

| Attribute | Value |
|-----------|-------|
| Type | `int` |
| Default | `0` |
| Persisted | Yes (in `coordinare.state.json`) |
| Replaces | `feedback_cycle_count` for content-driven exhaustion checks |
| Migration | If `feedback_cycle_count > 0` and this field absent/0 on load, set `content_feedback_cycles = feedback_cycle_count` |

Incremented by `monitor_performer` when a performer returns `changes_requested` (reviewer or QA feedback). Checked against `config.max_feedback_cycles` (default 5). When exhausted → card enters `blocked` with `reason=feedback_cycle_limit`.

---

### `transient_error_cycles: int`

| Attribute | Value |
|-----------|-------|
| Type | `int` |
| Default | `0` |
| Persisted | Yes |
| Distinct from | Per-dispatch retry budget in `monitor_performer` (spec-098, max 3 per dispatch) |

Incremented by `monitor_performer` when a performer exits via `env_blocked`, `system_error`, or `unknown` backend failure. When exhausted (limit=3 per card) → card takes the spec-095 ENV_BLOCKED hold path. Counter is **not reset** between dispatches on the same card; it accumulates across the card's lifetime.

---

### `open_questions: list[dict]`

| Attribute | Value |
|-----------|-------|
| Type | `list[dict]` — each entry is `{"question": str, "answer": str}` |
| Default | `[]` |
| Persisted | Yes |

Written by `monitor_performer` after a successful assessor run, by extracting the `open_questions` field from the performer's JSON result. Read by `dispatch_performer` when re-dispatching the assessor — injected into the card context payload as `prior_clarifications`.

---

## Derived / Runtime (not persisted)

### `prior_clarifications` (card context payload field)

Not a state field. Injected by `dispatch_performer` into the card context JSON at assessor dispatch time, populated from `open_questions` if non-empty. Absent on first dispatch; present on all subsequent assessor dispatches.

---

## No New Storage

All three fields extend the existing `PersistedSession` Pydantic model in `src/coordinare/graph/state.py`. They are serialized/deserialized by the existing `state_store.py` JSON snapshot mechanism. No new store, no schema migration tooling required.

## State Transitions

```
Card created → content_feedback_cycles=0, transient_error_cycles=0, open_questions=[]

Assessor completes → open_questions updated from result

Reviewer returns changes_requested → content_feedback_cycles += 1
  if content_feedback_cycles >= max_feedback_cycles:
    → phase=blocked, reason=feedback_cycle_limit

Performer returns env_blocked/system_error/unknown → transient_error_cycles += 1
  if transient_error_cycles >= 3:
    → spec-095 ENV_BLOCKED hold path

Assessor re-dispatched → prior_clarifications injected from open_questions
```
