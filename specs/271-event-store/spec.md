# 271: Persistent event store, phases 1 and 2 of 3

## Purpose and scope

Keep a redacted, queryable incident history across daemon restarts without another
service. Phase 1 adds storage, common capture funnels, retention, a query service and
one compact System health line. The existing activity feed stays in memory. Phase 2 adds
a Logs page, scoped HTTP and MCP access, transcript links and live SSE. Phase 3 adds
bounded, independent OTLP export and deployment documentation.

## Storage and delivery

The daemon owns `<state directory>/events.db`, beside the state snapshot. No opt-in or
dependency is needed. A queue bounded by count and a default 16 MiB byte budget feeds
one writer task. All SQLite work runs on a dedicated single-worker executor. Producers
snapshot raw rows with JSON serialization without waiting for disk. The writer redacts
every row before insertion. Overflow, serialization failures and write failures after
bounded retries count as dropped rows. Shutdown stops acceptance and drains with a
bounded wait. A stalled filesystem can delay the worker, never the event loop.

The events table has id, ts, symphony, card_id, run_id, stage, source, kind, level,
message and JSON data. UTC timestamps plus increasing ids give stable ordering. Indexes
cover ts, (symphony, ts), (run_id, ts), (kind, ts). PRAGMA user_version tracks
independent migrations, without changing the state snapshot schema. Version 1 defines
the base table; version 2 adds indexes and the optional full text index. Version 3 adds
a unique performer index on run_id and the integer transcript_seq in JSON data. For
existing databases, duplicate cleanup scans at most one primary-key batch per maintenance
turn, with its cursor and seen identities persisted across restarts. Normal writes run
between maintenance turns. A temporary insert trigger rejects replays until cleanup
finishes and maintenance builds the unique index. Startup never deletes legacy rows.
Version 4 adds optional card, source and level timestamp indexes. Startup does not
build them. Maintenance builds one index in its own transaction between write
batches, starting after the writer is ready, and logs the index name and duration
with event_store.index_built. The committed SQLite index catalog records progress:
a restart skips completed indexes and retries interrupted builds. The schema version
identifies the query contract, not completion of optional performance work. Queries
work while indexes are absent. Existing stage indexes are removed in a separate
maintenance slice, logged as event_store.index_dropped. Stage remains queryable;
the write throughput measurement justified keeping seven filter indexes instead of
eight. Each index build still holds the SQLite writer lock for that individual
build; slices prevent all builds from holding up queued writes consecutively.
Inserts use INSERT OR IGNORE. FTS5 indexes message when supported; otherwise literal LIKE search is used.

Use DELETE rollback journaling, synchronous NORMAL and a busy timeout. WAL is unsafe on
the reference NFS state volume. A single daemon must own the volume. Prefer local disk
storage for the state volume. Set incremental auto vacuum when creating the database.
Existing versioned databases are migrated transactionally.

## Capture and privacy

Capture accepted activity entries at the shared ActivityLog append funnel, independent
of its SSE sink. Operator actions already share that path. Attribute symphony from
logging context or the daemon's card ownership map when available. Capture performer
payloads at the common monitor activity intake even without a dashboard log. Keep
tool arguments, progress, errors and cost payloads within the text bounds below, with
run_id and transcript_seq in data. Deduplicate cumulative performer batches with persistent (run_id, transcript_seq)
identities. The performer row is the only persistent copy of the corresponding live feed
entry. Distinct sequence numbers remain distinct even when timestamps and payloads
match. This does not add performer buffering or replay. Events never received from a
disconnected performer cannot be recovered here.

One non-blocking structlog processor captures INFO and above after context merge and
exception formatting. It excludes event store records and never logs its own failures.
Bootstrap and env cache records use this common logging funnel. A bounded startup buffer
is a ring retaining the newest 128 early log records within 1 MiB and flushes them
into the store when installed. CLI processes that never install a store retain only
this bounded ring until exit; new records evict the oldest. Uninstalling clears it.
Serialization and store emission happen outside the capture lock, including replay.
A thread-local guard skips nested capture when a user conversion logs. Every column
passes through redact_state_value and sensitive-key redaction on the writer before
insertion. Unsupported objects become strings before redaction.
Oversized rows are rejected before redaction rather than retaining unbounded producer
payloads in the queue. Before redaction, every string (including message, nested data
values and keys) is clipped to 16 Ki characters plus a 1 Ki character overlap margin.
Redaction sees the margin so credential patterns crossing the retained boundary can
match, then output is clipped to 16 Ki characters. The writer yields the GIL after
each redacted row. The byte budget includes foreign thread callbacks and batches
being written, and is configurable with event_store.queue_bytes.

## Retention and health

A validated event_store block defaults to retention_days 30 and max_bytes 2147483648. No
configuration is required. Maintenance deletes oldest expired rows first, then oldest
rows when the physical file exceeds the cap. Each transaction deletes at most one batch,
followed by a bounded incremental vacuum. Subsequent turns continue reclamation,
yielding between batches. The cap is an eventual bound: queued inserts, indexes and the
minimum SQLite schema footprint can exceed it briefly. Retention cannot preserve a
single row larger than the size budget.

Cached health contains size_bytes, rows, oldest_ts, dropped, pruned, writer_state and
last_error. The writer retries failures six times with exponential backoff bounded at
half a second. Error categories are exposed without exception text. The existing System
health panel renders these in one line, without filesystem work on the loop. Drops and
pruned counts cover the current daemon process; row and size statistics are refreshed
from disk. A post-commit stats failure appears on the health line without retrying
the committed insert or counting it as dropped. Recovery recounts from disk rather
than applying deltas to stale totals. Storage failures count drops and appear on the
health line.

## Query contract

`await query_events(filters, cursor, limit)` uses its own read-only connection in a
thread, with one short read transaction per page closed before returning. Filters
support inclusive start and end UTC times, symphony, card, run, stage, source, kind,
level and literal free text. Limits are bounded to 500 rows. Without text, newest
first means ORDER BY ts DESC, id DESC with an opaque (ts, id) keyset cursor. Text
search uses descending insertion ID order with an ID cursor, including the LIKE
fallback. Both cursors avoid duplication under concurrent inserts and remain usable
after pruning. Invalid filters or cursors fail explicitly.
Authorization and symphony scope must be applied by the phase 2 callers; this internal
service is not an exposed API.

## Validation and budgets

Test schema migration, optional FTS and fallback, all filters, paging ties, redaction,
capture funnels, failure isolation, overflow, many producers, retention by age and
physical size, bounded shutdown and cached health. Measure throughput without enforcing
a performance floor. Gate the median added structlog call overhead with a named 50
microsecond bound across many calls.

## Initial verification

The test-first run exited 2 for missing implementation modules. The focused suite then
passed. A representative 2000-row benchmark measured 1392 rows per second and 669.5
microseconds per enqueue on the development machine, including redaction. The requested
full-tree command reached an existing loopback HTTP contract test and failed because the
sandbox prohibits socket binding. It reported 22 passed, 3 skipped and 1 failure. No
test or quality gate was changed to bypass that failure.

 ## Review verification

Regression tests first reproduced caller thread redaction, oversized input work, byte
budget absence, duplicate feed rows, lock loss, missing health fields, startup log loss
and repeated vacuum calls. The median added structlog INFO call cost was 373.30
microseconds before correction and 6.59 microseconds afterward across nine samples of
200 calls, below the named 50 microsecond test bound. Completion tests use conditions
with 30 second deadlines. The throughput test checks row correctness without a
performance floor. SQLite retains rollback journaling; NFS state volumes require working
locking and local disk is recommended.

## Phase 2: incident queries

The Logs page at `/logs` reads `GET /api/events`. Its shared EventQuery contract
validates timezone bearing ISO timestamps, an ordered inclusive range, exact symphony,
card, run, stage, source, kind and level filters, literal message search, and the
store's opaque cursor. Text filters are bounded to 1024 characters, cursors to 512,
and limits to 1 through 500. HTTP defaults to 100 rows; the browser requests 50.
Full text results use descending insertion ID order and ID cursors, allowing FTS5 to
stream matches and apply the limit before sorting the whole match set. Insertion
order differs from timestamp order only for late arriving performer events. The
literal LIKE fallback uses the same insertion order and cursor, without choosing a
filter index that would require sorting all matches. Queries without text keep
descending timestamp and ID order. All reads, including LIKE scans with no matches,
have a named 250 ms execution budget; lock waits remain limited to 50 ms. A query
that exceeds its budget releases its transaction and asks for narrower filters.
SQLite reads remain inside EventStore.query_events's worker thread. Storage failures
return a generic unavailable response. Responses are never cached and receive another
redact_state_value and sensitive key redaction pass, including legacy rows.

The page participates in the existing navigation, static asset registration and
symphony switcher. It reads currentScopedState. A header scope fixes the symphony
parameter on every query, including form submissions; this remains a display filter,
not an authorization boundary. No snapshot fields are added. The existing snapshot
scope declarations and unscoped read allow list remain exhaustive.

Rows show the shared timeHtml timestamp, level, source, symphony, card, kind and
message, with a native disclosure for redacted JSON. Card and run addresses use the
existing helpers. An integer transcript_seq adds the transcript's #e fragment. A run
without a card address uses the existing short run route. All interpolated values
are escaped and the page script uses ES5 syntax.

A successful writer commit calls an optional event loop callback, outside the write
retry path. It broadcasts only an events_changed notification through the existing
bounded SSE queues. Callback failures cannot retry a committed insert. Live readers
requery page one with their own filters; commit bursts coalesce and an outstanding
query retains one catchup notification. Reconnect and heartbeat also requery, so a
full SSE queue cannot permanently lose new rows. Paging disables Live. Request logs
for this read endpoint use DEBUG to prevent an INFO log, commit, query feedback loop.

The MCP registry exposes query_events with a query object using the same EventQuery
model, including cursor and limit. The existing authenticated DashboardClient applies
its read timeout budget and ToolResult error mapping. The HTML page is deferred in
the MCP page path registry; the API is covered by the tool.

Tests cover HTTP validation, scope, paging, legacy redaction, storage errors, auth,
MCP registration and invocation, commit notification ordering and failure isolation,
and the shipped JavaScript under node. The new browser file tests filtering, paging,
Live, links and JSON expansion in either Chromium or Firefox. Browser tests require
an environment that permits binding a server socket and are left for the operator.
