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
interpolation. First pass means a resolved card and stage with one retained run,
successful on that run. Retries are subsequent runs of that card and stage.

Bound filesystem discovery, transcript bytes, run count, SQLite rows and read
time. Reuse the event indexes and query budget. Share one short lived cache across
all dimensions, filters and windows. Report incomplete coverage explicitly.

Acceptance: fixed record tests for every measure and window; restart equality;
usage reconciliation across groupings; HTTP and MCP parity, scope and redaction;
tests proving cache reuse, bounds and indexed reads. Leave changes uncommitted.
