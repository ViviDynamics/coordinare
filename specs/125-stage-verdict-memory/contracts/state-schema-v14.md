# Contract: snapshot schema v14 (125)

Consumed by `tests/contract/test_state_persistence_v13_to_v14.py` (mirrors the v11→v12 contract test pattern).

## Version

- `SCHEMA_VERSION` bumps 13 → 14 in `state_store.py`.
- A v13 snapshot loads in a v14 daemon with all fields round-tripping losslessly.
- A v13 (or any earlier) snapshot loads in a v14 daemon with: `stage_verdicts == {}`, `processed_issue_comment_ids == []` and `last_issue_comment_id is None` - all three per session. No error, no migration warnings.
- **Ownership correction (adversarial review):** the comment-watermark fields live on `PersistedSession` (per-card), because `route_issue_comments` reads the active card's linked issue; they were initially drafted as top-level.

## Field Registry

| Field | Owner | Type (serialized) | Default | Bound |
|---|---|---|---|---|
| `stage_verdicts` | `PersistedSession` (per-card) | `dict[str, {head_sha: str, verdict: str, recorded_at: str}]` | `{}` | ≤5 keys (one per verdict stage) |
| `processed_issue_comment_ids` | `PersistedSession` (per-card) | `list[int]` (sorted ascending) | `[]` | largest 2000 retained at save |
| `last_issue_comment_id` | `PersistedSession` (per-card) | `int \| null` | `null` | — |

## Round-trip requirements

1. Save→load preserves `stage_verdicts` exactly (keys, `head_sha`, `verdict`, `recorded_at`).
2. `StageVerdict` rejects extra keys (`extra="forbid"`) and empty `head_sha`/`verdict` at construction — corrupted entries fail closed at load into a **dropped entry + warning**, never a crashed daemon (wrap per-entry validation; a bad slot is equivalent to no slot: dispatch).
3. `processed_issue_comment_ids` restores as `set[int]` into the per-card session; save serialises `sorted(...)[-2000:]`.
4. Serialisation stays JSON-safe (no datetime objects — `recorded_at` is a string at the model boundary).
5. No dispatch-payload change: nothing in this feature adds fields to the coordinare→performer dispatch payload (`specs/contracts/dispatch-payload.md` untouched).
