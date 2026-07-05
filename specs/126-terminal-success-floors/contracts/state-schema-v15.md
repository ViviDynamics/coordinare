# Contract: snapshot schema v15 (126)

Consumed by `tests/contract/test_state_persistence_v13_to_v14.py` (mirrors v12→v13).

## Version

- `SCHEMA_VERSION` bumps 14 → 15. The v13 pin test relaxes to `>= 13` per the established precedent.
- v1–v14 snapshots load with `feedback_ledger == []`, `feedback_origin_sha is None`, `noop_success_retries == 0`.

## Field Registry

| Field | Owner | Type (serialized) | Default | Bound |
|---|---|---|---|---|
| `feedback_ledger` | `PersistedSession` | `list[{id: str, raiser: str, origin_sha: str, body_digest: str, disposition: str, dispute_reason: str, re_raised: bool, round_status: str}]` | `[]` | current + 1 prior round (pruned at stamp time) |
| `feedback_origin_sha` | `PersistedSession` | `str \| null` | `null` | — |
| `noop_success_retries` | `PersistedSession` | `int ≥ 0` | `0` | reset on head move / new round |

`FeedbackItemRecord` is a pydantic model with `extra="forbid"`; `disposition ∈ {open, addressed, disputed, dispute_accepted, dispute_rejected, superseded}`; `round_status ∈ {current, previous}`. Malformed entries are dropped at persist/load (bad entry == no entry), mirroring `StageVerdict`/`repair_audit` tolerance.

## Round-trip requirements

1. Save→load preserves all three fields losslessly (model_dump(mode="json") → model_validate).
2. `body_digest` is capped at 200 chars at stamp time — never the full comment body, never secrets.
3. The JSON-schema contract (`specs/003-state-persistence/contracts/workflow-snapshot.schema.json`) gains v14 in the enum + the three per-session properties.
4. No new WorkflowSnapshot top-level fields.
