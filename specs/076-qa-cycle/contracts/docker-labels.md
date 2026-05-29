# Contract — Performer Container Docker Labels

**Module:** `src/coordinare/services/performer_lifecycle.py` (extended) + `src/coordinare/services/http_performer_service.py` (caller)
**FRs:** FR-009 (mandatory labels), FR-002 (used by reconciliation)

## Required labels (all 6 MUST be present on every coordinare-spawned ephemeral container)

| Label key | Type | Example | Set by |
|---|---|---|---|
| `coordinare.performer.id` | str | `claude-ephemeral` | EXISTING; preserved unchanged from `PerformerEndpointConfig.id` |
| `coordinare.session_id` | str (≤64) | `9fcadf3c-93ee-4fa0-8a40-f943b69c00fe` | NEW; dispatcher allocates the session id BEFORE `start_ephemeral` |
| `coordinare.card_id` | str (≤64) | `PVTI_lADOBjmjsc4ApgHnzgq-8Gc` | NEW; from `state.active_card.id` |
| `coordinare.performer_stage` | str | `implementing` | NEW; from `state.performer_stage` |
| `coordinare.daemon_started_at` | str (ISO8601 UTC) | `2026-05-28T21:14:17Z` | NEW; captured once at daemon init |
| `coordinare.spec_version` | str (constant) | `076` | NEW; hard-coded module constant |

## API change to `start_ephemeral`

```python
# BEFORE (existing):
async def start_ephemeral(config: PerformerEndpointConfig) -> StartedContainer: ...

# AFTER (076):
async def start_ephemeral(
    config: PerformerEndpointConfig,
    *,
    extra_labels: dict[str, str] | None = None,
) -> StartedContainer: ...
```

The function:
- Always adds `--label coordinare.performer.id={config.id}` (existing behaviour, unchanged).
- For each entry in `extra_labels`, adds `--label key=value`.
- Validates label keys and values: keys MUST match `^coordinare\.[a-z0-9._-]+$`; values MUST be non-empty strings ≤256 chars. Raises `LifecycleError` on invalid input — the caller is responsible for hygiene.

## Caller pattern (in `http_performer_service.dispatch_card`)

```python
session_id = str(uuid.uuid4())                      # allocate BEFORE start_ephemeral so label can carry it
labels = {
    "coordinare.session_id": session_id,
    "coordinare.card_id": card_context["card_id"],
    "coordinare.performer_stage": state["performer_stage"],
    "coordinare.daemon_started_at": state["daemon_started_at"],
    "coordinare.spec_version": "076",
}
started = await performer_lifecycle.start_ephemeral(effective_config, extra_labels=labels)
...
ephemeral_job = _EphemeralJob(container_id=started.container_id, endpoint=started.endpoint, client=...)
self._active_jobs[session_id] = ephemeral_job       # NOTE: keyed on our pre-allocated session_id, not the job-runner's job_id
```

**Important shift from today:** today `_active_jobs` is keyed on the `job_id` the performer's job-runner returns. The 076 change keys it on `session_id` (which coordinare allocates and labels onto the container) so the label↔registry link is direct. The `job_id` becomes a sub-field on `_EphemeralJob`.

## Label-vs-snapshot matching rules (used by reconciliation pass)

For each container `c` enumerated via `docker ps --filter label=coordinare.spec_version=076`:

1. Read `c.labels["coordinare.session_id"]` — call it `cid_session_id`.
2. Read `c.labels["coordinare.card_id"]` — call it `cid_card_id`.
3. Look up `state.active_sessions[cid_card_id]`:
   - If missing: container is an **orphan** → sweep (FR-005).
   - If present, compare `state.active_sessions[cid_card_id].agent_dispatch.session_id` with `cid_session_id`:
     - Match: candidate for **adoption** → probe job-runner health.
     - Mismatch (different session id in snapshot): container is from a **prior session** that was already replaced; sweep.

For containers missing the `coordinare.spec_version` label (pre-076 launches): treat as legacy orphan → sweep on next startup, do not adopt.

## Test coverage

- `tests/contract/test_docker_label_schema.py` — asserts every coordinare-spawned container has all 6 labels with valid types.
- `tests/unit/services/test_reconciliation.py` — `_classify_container` exercise of the 4 match outcomes (adopt / sweep-orphan / sweep-mismatch / sweep-legacy).
