# Implementation plan

Use stdlib sqlite3, a single dedicated writer executor and separate threaded read
connections. Split database schema and maintenance, query construction, capture
adapters and the asynchronous store to keep complexity within existing gates.
Wire lifecycle around daemon startup and teardown, independent of dashboard use.
Add a validated config block and cached System health data with one existing-panel
line. Preserve the state snapshot schema and current activity SSE behavior.

Write unit tests first, verify failure, implement, then run targeted checks and the
requested full tree. Benchmark representative rows. Review the complete diff,
commit locally and run quality-guard. No GitHub or cluster mutations.
