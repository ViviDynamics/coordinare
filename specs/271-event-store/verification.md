# Phase 2 blocker verification

This pass fixes read performance and v4 index migration without committing changes
or starting a socket listener. Slack xox* redaction belongs to phase 3, already
committed on another branch and merging immediately after phase 2. The redaction
implementation is unchanged.

## Tests first

The initial regression run failed all five cases with exit code 1: a 600,000 row
FTS query exceeded its budget, both text search paths paged by timestamp instead of
insertion ID, startup built every filter index, and writer maintenance had no index
slice. The stage retirement regressions also failed before that change.

Coverage now includes FTS and LIKE with half of 600,000 rows matching, ID cursor
paging across late arrivals and concurrent inserts, query plans without temporary
sorting, fresh and legacy migration restart, interrupted CREATE INDEX rollback,
per index transaction and duration logging, stage index removal, queries before
index completion, and a real writer committing between maintenance slices without
dropping events. Existing retention and performer cleanup tests cover the separate
maintenance schedules.

## Required checks

All commands used the requested shared virtual environment, PYTHONPATH and MYPYPATH.

| Check | Result | Exit code |
| :--- | :--- | :--- |
| Ruff over every changed Python file | Passed | 0 |
| mypy package coordinare | 269 source files passed | 0 |
| Ruff baseline ratchet | Baseline holds, 1417 violations across 43 rules | 0 |
| Focused storage and migration tests | 67 passed | 0 |
| Requested unit selection for 271, SSE, MCP and events | 1049 passed, 13265 deselected | 0 |

The requested selection finished in 84.51 seconds with five existing warnings.
No suppression, skip, pragma or baseline adjustment was added.

## Measurement method

Search measurement includes opening and closing the read connection, SQL execution
and response decoding. Both paths filter source as well as text, with every optional
filter index present. Each page contains seven results from 300,000 matching rows.
The budget is the named QUERY_TIMEOUT_SECONDS constant set to 0.250 seconds. Lock
waits retain the separate 50 ms cap.

SQLite insertion measurements isolate database cost from producer serialization,
redaction and intentional writer sleeps. Each variant starts from an identical
100,000 row database and inserts another 20,000 varied events in transactions of
128 rows. FTS, its triggers and performer replay protection remain enabled in every
variant. Three samples per layout alternate layout order; the reported rates are
medians. Four, seven and eight count the ordinary timestamp and filter indexes,
excluding the common FTS and unique performer index.

The stage index is the least useful additional index here: card, source and error
level support the main incident filters, while stage can be filtered after a run
or timestamp lookup. Removing stage also retires it from databases that already
completed v4. Stage remains a supported filter. One index is built or dropped per
transaction, after the writer starts, with queued write batches serviced between slices. SQLite's
committed index catalog records completion, so interrupted transactions roll back
and restart retries only missing work. Each individual index build still holds the
writer lock for its duration, which can grow with database size and storage latency.

## Isolated measurements

Final local run on October 15, 2026, after the requested suite completed:

| Search path | Table rows | Matching rows | Page latency | Read budget |
| :--- | ---: | ---: | ---: | ---: |
| FTS5 streaming join | 600000 | 300000 | 0.984 ms | 250 ms |
| Literal LIKE streaming scan | 600000 | 300000 | 0.540 ms | 250 ms |

| Ordinary indexes | Median rows per second | Drop from four index baseline |
| ---: | ---: | ---: |
| 4 | 8707.2 | 0% |
| 8 | 5354.0 | 38.51% |
| 7, stage removed | 5970.8 | 31.43% |

Raw write samples in layout order:

| Ordinary indexes | Sample 1 | Sample 2 | Sample 3 |
| ---: | ---: | ---: | ---: |
| 4 | 7800.2 | 8979.1 | 8707.2 |
| 7 | 5434.6 | 6013.0 | 5970.8 |
| 8 | 5354.0 | 5206.8 | 5397.1 |

Dropping stage improves measured throughput by 11.5% relative to eight indexes.
Seven indexes still cost 31.43% relative to the four index baseline. The remaining
cost is reported explicitly; this change does not claim to bring it below 25%.
These local SQLite rates are not whole daemon throughput or an NFS guarantee.

At 600,000 rows, FTS fixture index build durations were 296.28 ms for card,
355.24 ms for source and 298.73 ms for level. The LIKE fixture measured 282.46 ms,
308.62 ms and 343.46 ms respectively. These are individual maintenance lock holds,
with queued write batches serviced between slices.

The isolated search and insertion benchmark run passed three cases in 42.59 seconds,
exit code 0. Reproduce with the requested environment and shared virtual environment:

```sh
$V/pytest tests/unit/test_271_search_migration.py::test_271_large_fts_search_streams_within_budget tests/unit/test_271_performance.py::test_271_filter_index_write_throughput -q -s -p no:cacheprovider
```
