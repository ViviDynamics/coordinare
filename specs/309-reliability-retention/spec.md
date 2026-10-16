# 309: Reliability history coverage

The symphony reliability figure uses main's existing hourly board check totals
in completed UTC hours. It exposes partial and partial_reasons when
event_store.retention_days is shorter than the selected window or
event_store.max_bytes pruning removed hourly totals inside that window.
The observed count and success rate remain available. The dashboard labels
both as partial and escapes the displayed reasons. Run usage coverage and
unaffected symphonies keep their existing behavior. Default retention of
30 days leaves the 30 day figure unchanged without size loss.

Persist the active retention policy at writer startup and maintenance. Record
the latest two distinct size pruned hours per symphony atomically with deletion.
Keeping two hours lets readers exclude the current incomplete hour without
hiding loss of completed hours. Markers survive restart and an empty store.
Retention deletions and size deletions of ordinary events do not create size
prune markers for hourly totals.

Schema version remains 5. Startup idempotently creates two additive metadata
tables, stats_board_check_policy and stats_board_check_pruning, without changing
existing tables or the 184 rollup migration. Older version 5 daemons ignore
these tables and can still open the store. Historical size loss before this
change, including pruning by an older daemon after rollback, cannot be
reconstructed. Newly recorded size loss is reported until it ages out of the
requested window.

Tests cover short and default retention, shorter windows, size loss inside and
outside the window, symphony isolation, restart persistence, atomic rollback,
schema compatibility, the definition text and escaped dashboard reasons.
