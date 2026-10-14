# Plan: Token and cost accounting from usage records (#134)

## Design

One source of truth: the transcript `usage` records the daemon accepts onto disk.

- `services/token_usage.py` (new, pure): `UsageCounts`, the per-turn record, `TokenLedger` (per-run buckets keyed by (UTC day, model), the scope tier rule, rollups, the disk rebuild), the price table and cost arithmetic, and the snapshot view.
- `services/run_transcript.py`: the writer records the `stage` on `usage` records (set by the daemon from the monitor's target), collects every accepted usage record in `drain_usage()`, and exposes its sanitized `parts`. `TranscriptCollector.usage` owns the ledger.
- `services/http_performer_service.py`: `_transcript_writer` stores the stage with the target; `_absorb_transcript` feeds drained usage into the ledger after each accepted batch. Every poll and drain path goes through it, so totals rise live.
- `__main__.py`: `_build_transcript_collector` rebuilds the ledger from disk at boot.
- `config.py`: `CostTrackingConfig` gains `pricing_as_of`, `prices`, `default_price`; `cost_per_million_tokens` becomes a deprecated optional.
- `graph/nodes/monitor/body.py`: the transcript target carries the stage; the card cost comes from the ledger, not the flat rate.
- `dashboard/store.py`: `token_usage` top-level block; `usage` per active session (run and card totals).
- Front end: `dashboard_static/usage.js` (new; pure formatting and tooltip text, loaded after `helpers`); `performers.js` and `app.js` read the server's structured figures; `dashboard.html` gains the header strip and a Metrics line.

## Data flow

poll -> `_get_job_status` -> `_absorb_transcript` -> writer.append_batch (to_thread) -> writer.drain_usage() -> ledger.observe -> snapshot (`token_usage`, `sessions[].usage`) -> SSE -> usage.js.

## Non-goals and constraints

- No persisted coordinare state, no schema bump. The file gets one additive key (`stage`) on usage records only.
- Dashboard HTML stays under its 48 KiB budget: logic and styles go to `usage.js` and `shell.css`.
- ES5-style JS; every interpolated value through `esc()`.
- Snapshot additions are bounded (per-run turn cap, 8-day window, card entries only for active sessions).

## Tests

- `tests/unit/test_134_token_ledger.py`: tiers, per turn/run/stage/card/symphony/day rollups, live rise, dedupe, rebuild, truncation bound.
- `tests/unit/test_134_price_table.py`: validation, matching, default, unknown, deprecated flat-rate fallback and warning.
- `tests/unit/test_134_cost_fixture.py`: a sample transcript fixture with cache reads and writes; the cost is hand-computed and asserted exactly.
- `tests/unit/test_134_capture.py`: writer `drain_usage`, stage key, daemon-side wiring, transcripts disabled.
- `tests/unit/test_134_usage_js.py` + `tests/js/usage_checks.js`: node checks for formatting and tooltip text.
- `tests/e2e/test_134_token_cost.py`: browser checks for the header strip, card and run figures, and tooltips.
