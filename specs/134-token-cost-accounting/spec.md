# Feature Specification: Token and cost accounting from usage records

**Issue**: #134 (part of epic #116). Builds on #129 (run transcripts, `usage` records), and supersedes the flat-rate estimate of #121/#122.
**Status**: Implemented on branch `134-token-cost-accounting`.

## Problem

Cost on the dashboard is `tokens_processed x one flat rate`. The token count comes from a status counter the backend reports at the end of a stage, so it is one number with no breakdown: no input/output split, no cache split, no model. The price is one rate for every model. An operator cannot see what a run cost, why, or whether the price is right.

#129 now writes every `usage` record to the run transcript as it happens. That is the source for the figures.

## Goal

Token counts and cost come from the `usage` records, kept per turn with their model and four token kinds (input, output, cache read, cache write). Totals roll up per run, stage, card, symphony and UTC day. Totals rise while a run is in progress. Cost comes from a per-model price table with a visible "pricing as of" date. A figure that cannot be computed says why, and never shows a silent zero.

## User scenarios

1. **Live spend.** A Claude Code stage is running. Each finished model turn adds its tokens to the run, card and day totals without waiting for the stage to end.
2. **Hand-checkable cost.** An operator takes a run's transcript, multiplies each turn's counts by the table's prices, and gets the figure the dashboard shows.
3. **Breakdown on hover.** Every cost figure has a tooltip with tokens by kind, the price per kind, the model and the pricing date.
4. **Unknown model.** A model with no price shows its token counts and "cost unknown: no price for <model>". The cost is never estimated from a guessed rate.
5. **Restart.** The daemon restarts mid-run. Totals are rebuilt from the transcript files and the run continues from the same cursor, so nothing is counted twice and nothing is lost.
6. **Transcripts disabled.** No usage records exist. Tokens shown are the status counter, labelled as a total only, and cost reads "not tracked (transcripts disabled)". Nothing reads as zero.

## Functional requirements

- **FR-001 Source.** Usage is counted only from transcript `usage` records the daemon has accepted onto disk. Each record is counted once (the writer's seq cursor decides).
- **FR-002 Per turn.** Each accepted usage record keeps its day (UTC, from the record `ts`), model, scope, and the four token kinds. Per-turn entries are kept for a run up to a bound (`MAX_TURNS_PER_RUN`); totals stay exact beyond it.
- **FR-003 Scope rule per run.** A run's total is taken from one tier only, so nothing is counted twice:
  1. `message`-scope records, when any exist (Claude Code emits one per model message and a run total at the end; the message records are the per-model source).
  2. otherwise the last `run`-scope record (a cumulative total: codex, junie, driver result line).
  3. otherwise `scope`-less records, summed per turn, flagged as unscoped.
- **FR-004 Rollups.** Totals are available per run, stage, card, symphony and UTC day, and as "today" and "rolling 7 UTC days including today" windows.
- **FR-005 Live.** A record reaches the totals on the poll that absorbs it. No stage end is needed.
- **FR-006 Restart.** On startup the ledger is rebuilt from transcript files whose modification time is inside the 8-day window. Stage is recovered from a `stage` key the daemon writes on each `usage` record it stores (the only addition to the file format). The rebuild reads only `usage` lines.
- **FR-007 Price table.** `cost_tracking.prices` is a list of entries. Each entry has `models` (exact ids, aliases, or prefixes ending in `*`) and four rates per million tokens: `input_per_million`, `output_per_million`, `cache_read_per_million`, `cache_write_per_million`. `cost_tracking.pricing_as_of` (a date) is required when any price is configured. `cost_tracking.default_price` is an optional operator-set entry for models no entry matches. There is no built-in price.
- **FR-008 Matching.** An exact id wins, then the longest matching prefix, then `default_price`. A `None` model (not recorded) is never matched to a prefix or the default.
- **FR-009 Validation.** Negative rates, a missing `pricing_as_of`, an empty `models` list, a wildcard that is not the final character, and a model pattern listed in two entries are load-time errors.
- **FR-010 Deprecated flat rate.** `cost_tracking.cost_per_million_tokens` still works when no `prices` and no `default_price` are configured: it prices every model at the flat rate, labelled "deprecated flat rate, no pricing date". A startup warning names the replacement. When a table is configured the flat rate is ignored, with a warning.
- **FR-011 Unknown price.** A model with no price keeps its token counts. Its cost is unknown, with the reason. A total with any unknown cost shows the unknown with the reason, and the priced part as a partial figure.
- **FR-012 Transcripts disabled.** No ledger data. Tokens come from the status counter and are labelled a total only. Cost is "not tracked (transcripts disabled)". The per-card flat-rate figure is removed.
- **FR-013 Display.** Board card (live row), card detail, run header (`/performers/<role>`), Metrics, and the header status strip ("Spend today / this week") show these figures. Each cost figure has a tooltip with the breakdown and the pricing date. Day totals are labelled UTC in the UI.
- **FR-014 Persisted state.** None added. The snapshot shape gains one top-level `token_usage` block and one `usage` object per active session. `cost_per_million_tokens` leaves the snapshot. Bounded: the ledger holds at most the 8-day window and the per-run turn bound.

## Out of scope

- A UI editor for the price table (the config file is the editor; `config.js` does not expose `cost_tracking` today).
- Bench cost estimates (`coordinare/bench`), which keep their own rate parameter.
- Pricing history beyond the single `pricing_as_of` date.
- Per-turn display in the dashboard (the per-turn entries are in the ledger and the transcript).

## Assumptions

- Day boundaries are UTC. The UI says so.
- The record `ts` is the time of the usage record, not of the model call.
- The most recent Claude Code message is counted when the next message begins or the run ends, so the live figure can lag by one model turn.
