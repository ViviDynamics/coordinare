# Contract: Run Artifact

The single structured output of one benchmark run (FR-010). Pydantic models in
`src/coordinare/bench/artifact.py`; serialized to `runs/<ts>-<confighash>/run.json`
plus a `raw/` subdir holding per-dispatch performer summaries and per-CI pytest
output (referenced by `*_ref` fields). Enforced by
`tests/unit/test_134_artifact_schema.py`.

## Guarantees

1. **Versioned**: `schema_version: int`, bumped on any shape change. Consumers
   (135+) branch on it.
2. **Self-validating**: `RunArtifact.validate()` re-parses the written `run.json`
   (round-trip) and asserts it matches the schema **before the run is declared
   complete** (FR-011). A run that cannot produce a valid artifact fails loudly.
3. **Total coverage**: exactly one `CardOutcome` per seeded card, and every
   `final_state` ∈ `{merged, blocked, abandoned, error}` (FR-009). No card omitted,
   none left non-terminal.
4. **Cost is labeled estimate**: `cost_estimated == True` and `cost_usd` is a
   `tokens_processed × cost_per_million_tokens` estimate; both may be `None`
   (FR-012). Never presented as authoritative proxy USD.

## Shape

See `data-model.md` §A for the full field tables. Top level:

```
RunArtifact
├─ schema_version, run_id, started_at, finished_at, wall_clock_seconds
├─ config_fingerprint {hash, source_path}
├─ approver_policy, fixture_manifest
├─ cards: [CardOutcome{
│     card_id, issue_ref, title, fixture_id,
│     final_state, reached_stages,
│     dispatches:[PersonaDispatch], gate_decisions:[GateDecision], ci_results:[CIResult],
│     merge{merged, merge_commit, approved_by}, timing{…}, cost{…}
│  }]
└─ totals {tokens_processed, cost_usd, cost_estimated, cards_total, cards_merged, cards_terminal_nonmerge}
```

## Source of each field

| Artifact area | Source |
|---|---|
| board/PR/review/CI/merge events, `final_state`, `merge`, `gate_decisions` | the fake's recorded event log (research.md D5) |
| `dispatches` (`job_id`, `session_id`, `container_id`, `status`, timing, `tokens_processed`, `raw_summary_ref`) | the recording performer wrapper around `dispatch_card`/`check_status` |
| `ci_results` (`pytest_exit`, `conclusion`, `summary_ref`) | the fake's real-pytest CI run per `head_sha` |
| `cost` | `tokens_processed × cost_per_million_tokens` (`config.py:226-230`) |
| `config_fingerprint.hash` | SHA-256 of the config file that drove the run |

## Non-goals (this phase)

- No scoring / correctness judgement of the produced code (135).
- No authoritative per-request USD (deferred; research.md D8).
- No aggregation across runs / noise measurement (135/136).


## Spec 206: per-model pricing (additive fields)

Cost is priced from the daemon's `cost_tracking.prices` table, read through the same loader
(`bench/pricing.py`). The flat `cost_per_million_tokens` is not read by the bench. All fields
below are additive and default to `None` or `False`, so every earlier document still loads.

- `PersonaDispatch.cost_usd`, `cost_unknown_reason`, `cost_basis`: one dispatch's priced cost.
  `None` means unknown, and the reason says why (no token count, or no price for the model).
- `Cost.cost_unknown_reason`, `Cost.cost_basis`: the card's cost. A card is unknown when any
  of its dispatches is unknown.
- `RunTotals.cost_partial`, `RunTotals.cost_unpriced_cards`, `RunTotals.cost_unknown_reason`:
  `cost_usd` sums the priced cards only. `cost_partial` is `True` when any card is unpriced.
- `cost_basis` is `total_tokens_at_input_rate` for a bare total (the bench records one token
  count per dispatch). A cost with no basis was written before spec 206 and is reported as
  unknown, never as a price.
