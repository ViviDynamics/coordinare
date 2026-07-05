# Data Model: Terminal-Success Progress Floors (126)

Snapshot schema **v13 → v14** (stacked on 125's v13). All additions default-empty; pre-v14 snapshots load with today's behaviour.

## FeedbackItemRecord (new pydantic submodel, `state_store.py`)

| Field | Type | Semantics |
|---|---|---|
| `id` | `str` | stable per-card id (`fb-<n>`), assigned at bounce time; monotonic counter derived from the ledger |
| `raiser` | `str` | stage that raised the item (`reviewing`/`security`/`qa`/`closing_review`) or `ci` (CI-gate bounce) |
| `origin_sha` | `str` | PR head the raising verdict was issued against ("" when unresolvable → floor fails open) |
| `body_digest` | `str` | first ≤200 chars of the item body (observability; full body rides only the transient relay entry) |
| `disposition` | `str` | `open` → `addressed` \| `disputed` → `dispute_accepted` \| `dispute_rejected`; `superseded` when a newer round replaces it |
| `dispute_reason` | `str` | implementer's stated reason ("" unless disputed) |
| `re_raised` | `bool` | item belongs to a round raised after a rejected dispute of the same raiser |
| `round_status` | `str` | `current` \| `previous` (older rounds pruned) |

## PersistedSession — new fields

| Field | Type | Default | Semantics |
|---|---|---|---|
| `feedback_ledger` | `list[FeedbackItemRecord]` | `[]` | the per-card feedback contract state; tolerant per-entry validation |
| `feedback_origin_sha` | `str \| None` | `None` | head the CURRENT round's feedback was raised against; the implementer floor's comparison reference |
| `noop_success_retries` | `int` | `0` | bounded strengthened-re-dispatch counter (F4/F5); reset on head move or new round |

All three mirrored on `CardSession` + `_SESSION_FIELDS` + daemon persist/restore, following the 125 `stage_verdicts` plumbing exactly.

## Wire additions (no schema break)

- relay_feedback entries (existing `list[dict]`): + `id`, `raiser`, `re_raised`.
- card_context: + `disputed_feedback: list[{id, body, reason}]` (injected only for the raising stage while disputes pend).
- `ProtocolResponse` (both sides): + `feedback_dispositions: list[dict] = []`.

## State transitions

```
bounce with feedback (raiser R, origin S)
  └─> stamp: ledger += items(open, R, S); feedback_origin_sha = S;
      prior round → round_status=previous (older pruned); noop_success_retries = 0

implementer terminal success (stage=implementing):
  F1  no origin SHA ──────────────────────────► accept (today's path)
  F2  head moved ─────────────────────────────► accept; apply dispositions to ledger
  F3  head unmoved + ≥1 disputed ─────────────► accept; disputed → queue for raiser
  F4  head unmoved + 0 disputed + retries==0 ─► strengthened re-dispatch; retries=1
  F5  head unmoved + 0 disputed + retries≥1 ──► operator hold (blocked)

raiser R runs with pending disputes:
  passes ──► disputes → dispute_accepted
  bounces ─► disputes → dispute_rejected; new round items re_raised=true

completion disputes a re_raised round with head unmoved ──► operator hold
disputed item with raiser == "ci" ──────────────────────► operator hold

documenting terminal success:
  files_modified non-empty or head delta ──► record documentation pass (125) — today's path
  zero files AND no head delta ────────────► advance + documenting_noop_completion; NO 125 record
```

## Interplay with 125

- A queued dispute for stage S vetoes S's verdict-cache skip (V2b in 125's decision table).
- U2 suppresses `_record_stage_verdict` for documenting, keeping the last-documented SHA truthful.
