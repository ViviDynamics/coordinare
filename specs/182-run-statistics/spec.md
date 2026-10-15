# Run statistics API

Provide authenticated, read only `GET /api/stats` and MCP `query_stats` over existing
transcripts, the token ledger accounting rules, persisted card lifecycle records,
SQLite events and the current board snapshot. Do not add a durable store.

Support agent (persona), stage, symphony, model, backend and outcome dimensions;
24h, 7d, 30d and all retained windows; filters; UTC daily series; run outcomes,
rates, retries, bounces, duration percentiles, usage, cost and failure reasons.
Expose card flow per symphony, with explicit coverage for missing observations.
Unknown data must never become a fabricated zero or successful outcome.

Runs belong to the window containing their start, with all their recorded usage.
Daily series use the same start cohort. Model means dispatch model; the ledger
still prices each actual usage model. Rates are fractions. Percentiles use linear
interpolation. Retries are subsequent runs of that card and stage.

Outcomes separate stage judgement from process completion. A recorded lifecycle
verdict of pass or fail produces `success` or `failure`. A transcript result with
no verdict produces `completed_without_verdict` when it completed, or
`system_error` when it failed. Recorded terminal events can instead produce
`blocked`, `system_error` or `cancelled`. The latest attributed verdict or terminal
event determines the outcome. No recorded result means `unknown`.

Only reviewing, security, qa, documenting and closing_review are verdict stages,
matching the monitor's canonical `VERDICT_STAGES`. Implementing completion does
not imply a passing verdict. `verdict_runs` counts runs with an attributed pass or
fail verdict in one of those stages. `success_rate` divides passing verdict runs
by `verdict_runs`, independently of process completion. A resolved card and stage
pair is one whose latest retained run has a verdict. `first_pass_rate` divides
resolved pairs with exactly one retained run and a passing verdict by all resolved
pairs. Earlier retained attempts outside the selected window still prevent first
pass. Rates are null when their verdict denominator is zero. Failure reasons
exclude ordinary completion without a verdict. A failing verdict or CI BOUNCE
increments bounces once per run.

An observation with a run identity attaches only to that run of its card and
normalized stage. A legacy verdict, CI result or terminal observation with no
run identity attaches only when exactly one run of the card and stage was open
at the observation time. Run headers and lifecycle events both normalize role
names through `ROLE_TO_STAGE`; lifecycle writers normally use canonical stages.
Transcript completion timestamps and attributed verdict or terminal events close
intervals. Missing closure leaves an interval open. Overlaps, gaps and unknown
identities increment `coverage.unattributed_observations`, including observations
for cards with no retained runs. Explicit identities can attach after transcript
completion because monitor verdicts can arrive later.

Human review intervals contribute samples and seconds only when they overlap the
window for positive time. Both closed and still open intervals are clipped to the
window. A wait ending before or exactly at the window start contributes neither
a sample nor seconds. Seconds are null when no interval overlaps.

Bound filesystem discovery, transcript bytes, run count, SQLite rows and read
time. Reuse the event indexes and query budget. Share one short lived cache across
all dimensions, filters and windows. Report incomplete coverage explicitly.

Read canonical usage from the existing event store first. Accepted HTTP transcript
usage is captured there as `run_usage`, independently of display cost text.
Structured performer usage and cost events remain readable when canonical events
are absent. SQL aggregates by run and model through `events_run_ts`, applying the
ledger rules: message sums first, otherwise daily cumulative run steps, otherwise
unscoped sums. Canonical records take precedence over display events and sequence
identities prevent duplicate counting. Pricing still uses the existing PriceTable.

The oldest retained event is the conservative store coverage boundary. Only runs
starting before that boundary fall back to transcripts. When no store is available
or it is empty, all runs use the bounded transcript fallback. A run within store
coverage with no structured usage reports `usage_events_missing`; textual cost
events never fabricate token counts. A legacy transcript's final line without a
newline is discarded alone, retaining usage from complete lines and reporting
`usage_partial_line`. Byte or time truncation still refuses partial run totals.

Read at most 2000 runs, 10000 aggregated run/model groups, and 128 runs per SQL
batch. Each SQL statement has the event reader's 250 ms deadline; all usage
batches share a one second deadline and one read snapshot. Transcript fallback
retains its 16 MiB byte budget and one second deadline. The source cache lasts
30 seconds across dimension and window queries.

Reproduce performance with `python scripts/benchmark_run_statistics.py`. The
synthetic fixture spans 30 days with 2000 runs, 40000 canonical usage events and
72438890 transcript bytes, exceeding the legacy transcript usage budget. On the
development machine, the final measured cold query took 0.904 seconds and a cached
stage query took 0.027 seconds, with all 2000 runs accounted for, 6000000 tokens,
USD 8.00, zero missing usage runs and no partial reasons. These are local timings,
not a latency guarantee.

Acceptance: fixed record tests for every measure and window; restart equality;
usage reconciliation across groupings; HTTP and MCP parity, scope and redaction;
tests proving cache reuse, bounds and indexed reads. Leave changes uncommitted.
