Refs #271 (phase 2 of 3).

Adds the Logs page and query_events MCP tool for scoped event history, cursor paging and live updates.

Raises the named SQLite read budget to 250 ms. FTS streams descending rowids and text pages use ID cursors. Full text results follow insertion order, differing from timestamp order only for late arriving performer events. LIKE uses the same bounded insertion ordering. On 600000 rows with 300000 matches, FTS returned in 0.984 ms and LIKE in 0.540 ms.

Moves v4 filter indexes out of startup into maintenance, one transaction per slice between write batches. Each step logs its duration. Committed index metadata records progress across restarts, and queries work before indexes finish. Individual builds measured 282 to 355 ms on 600000 rows.

The isolated SQLite benchmark measured 8707 rows/s with four indexes and 5354 with eight, a 38.51% drop. The stage index was removed because stage can be filtered after a run or timestamp lookup while card, source and level support core incident queries. Seven indexes measured 5971 rows/s, still 31.43% below baseline. Existing stage indexes are retired during maintenance.

Regressions were written first. The requested unit selection passed 1049 tests. Ruff, mypy, the ratchet and the isolated benchmark all exited 0. Tests cover stable text paging, interrupted and resumed migration, individual transactions and writes between index builds without drops. Changes remain uncommitted in the working tree.

The Slack xox* redaction gap is fixed in phase 3, already committed on another branch and merging immediately after this one. This PR leaves lib/redaction.py unchanged.
