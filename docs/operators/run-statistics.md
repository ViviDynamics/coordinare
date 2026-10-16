# Run statistics

`GET /api/stats?group_by=agent&symphony=example&window=7d` uses the dashboard's
existing authentication. MCP `query_stats` proxies the same endpoint, query
validation and response through its existing read budget. There is no additional
database, migration, background indexer or dependency.

`group_by` accepts `agent`, `stage`, `symphony`, `model`, `backend`, and `outcome`.
`window` accepts `24h`, `7d`, `30d`, and `all` (all retained observations).
Optional exact filters are `symphony`, `stage`, `agent`, `model`, `backend`, and
`outcome`. `top_n` limits failure reasons to 1 through 20, default 5. An unknown
symphony is 404; invalid parameters and exhausted SQLite time budgets are 422;
an unreadable event database is 503. Responses carry `Cache-Control: no-store`.
Symphony is a display filter within configured symphonies, not a separate
authorization boundary. Free text passes through the dashboard redaction guards.

The response contains `groups`, `totals`, a sparse UTC `daily` series with both
overall measures and groups per day, `card_flow` keyed by symphony, measure
`definitions`, and coverage metadata. Agent means persona, such as `implementer`;
backend is separate, such as `codex`. Stage codes use the existing role to stage
map. Missing dimensions appear as a null group key.

| Measure | Definition |
| --- | --- |
| Runs and window | Header start in the inclusive rolling window. All usage of a selected run belongs to that cohort, even when usage crosses midnight. Future starts are excluded. Daily buckets use the UTC start day. |
| Model | Dispatch model from the header. Every actual usage model is still priced individually, including planner and executor usage. |
| Outcomes | Latest recorded verdict or terminal outcome, otherwise the transcript result. Counts include success, changes_requested, blocked, system_error, cancelled and unknown. No result or terminal observation means unknown. |
| Success rate | Successful runs divided by runs with known terminal outcomes. Null if none are known. |
| First pass rate | Successful card and stage pairs with exactly one retained run divided by resolved card and stage pairs. The latest run owns the pair's contribution and must have a known terminal outcome. Attempts before the window remain context. |
| Runs per card | Runs divided by distinct symphony and card pairs in the selected group. |
| Retries | Second and later retained runs of the same symphony, card and stage, including context before the window. |
| Bounces | A run with changes_requested or an observed CI BOUNCE. Multiple bounce observations on a run count once. |
| Duration | Header start to the last timestamp in the bounded transcript tail. Negative and missing values are excluded. Median and p90 use linear interpolation; sample counts are included. |
| Tokens | Input, output, cache read and cache write counts, with a total. Uses TokenLedger's message usage, else cumulative run steps, else unscoped usage precedence. Known subtotals carry missing_usage_runs when some runs lack complete usage. |
| Cost | Existing PriceTable rules. Unknown models or incomplete usage make usd null; partial_usd preserves known cost. usd is rounded to microdollars for display. Decimal strings usd_exact and partial_usd_exact preserve exact additive reconciliation across groupings. |
| Last run | Latest selected run start. |
| Failure reasons | Most frequent recorded markers, terminal reasons or failing result text, with a deterministic tie break and the requested top N. |
| Cards completed and PRs merged | Recorded merge observations in the window. Each recorded card lifecycle has one merge. |
| Columns now | Current board snapshot counts, independent of the historical window; null without a snapshot. |
| Cycle time | First dispatch to merge for lifecycles merged in the window. Earlier dispatch context is included. Missing starts are excluded from the percentiles. |
| Human review wait | Uses the existing monitor_pr human versus automation classifier. Recorded human_review_started intervals end at human_review_ended, a human review, redispatch or merge, clipped to the window. Open intervals extend to query time. Null without recorded intervals. |
| Revision rounds per PR | CHANGES_REQUESTED review observations in the window divided by distinct observed PRs in lifecycles active in the window, including PRs opened earlier. Null without PR observations. |

Card flow describes selected symphonies independently of run dimension filters.
It does not attribute a whole card's cycle time to one persona.

Sources are the existing History reader and safe transcript paths, retained
usage interpreted by TokenLedger, the persisted card lifecycle snapshot, and
indexed SQLite lifecycle and terminal events. The live ledger's eight day
retention cannot answer thirty day queries, so retained usage is rehydrated into
a temporary ledger without changing the daemon ledger. Nothing is copied into
a new durable store. Accepted lifecycle events also go to the existing event
store to outlive snapshot limits.

## Read bounds and freshness

One source cache per dashboard application is shared by every grouping, filter
and window for 30 seconds. Concurrent cold requests share one read. A warm page
load reads no transcripts and executes no SQLite history query. Expiry refreshes
new runs and retention changes. Snapshot lifecycle and board state are fresh.
Grouping and percentile calculation run on a worker thread over the bounded cache.

A cold refresh uses the History discovery limits: 20,000 lstat operations,
2,000 entries per directory, 1,000 newest transcripts per card, and at most 2,000
run header and tail reads. Discovery, per run reads and usage parsing share a
one second cooperative deadline. A single filesystem call cannot be interrupted.
Headers use the existing 1 MiB cap and tails the existing 64 KiB cap. Usage reads
have a 16 MiB total budget with a 1 MiB line cap; an incomplete run read contributes
no usage subtotal. SQLite uses its existing kind and time index with a read only
connection, a 250 ms deadline, 50 ms busy timeout, 10,000 inspected rows and a
16 MiB row text budget. Interrupted connections and transactions are closed.

`partial` and `partial_reasons` expose discovery limits, `run_limit`,
`history_time_budget`, `unreadable_run`, `usage_read_budget_or_unreadable`,
`event_read_budget`, `transcripts_unavailable`, and `lifecycle_events_dropped`.
`coverage` includes runs read, usage runs read, lifecycle cards and the oldest
run start read. Retention, old snapshot eviction and events never recorded before
deployment cannot be reconstructed. Historical human wait has no invented start
boundary. Consumers must show coverage and missing usage with the figures and
must not present partial results as complete retained totals.

## Deployment

Deploy the daemon and MCP sidecar from the same revision, then restart normally.
Keep the existing transcript volume, state snapshot and event database. Existing
history is immediately queryable within the bounds; explicit terminal outcomes,
human wait boundaries and per run verdict identities accumulate after deployment.
No configuration changes or data migration are required. Rollback ignores the new
event rows and the additional snapshot lifecycle kinds.
