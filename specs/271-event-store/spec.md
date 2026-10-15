# 271: Persistent event store, phase 1 of 3

## Purpose and scope

Keep a redacted, queryable incident history across daemon restarts without another
service. Phase 1 adds storage, common capture funnels, retention, a query service
and one compact System health line. The existing activity feed stays in memory.
Phase 2 adds a Logs page, scoped HTTP and MCP access, transcript links and live SSE.
Phase 3 adds bounded, independent OTLP export and deployment documentation.

## Storage and delivery

The daemon owns `<state directory>/events.db`, beside the state snapshot. No opt-in
or dependency is needed. A bounded asyncio queue feeds one writer task. All SQLite
work runs on a dedicated single-worker executor. Producers redact and snapshot rows
before enqueueing without waiting for disk. Overflow, serialization failures and
write failures count as dropped rows. Shutdown stops acceptance and drains with a
bounded wait. A stalled filesystem can delay the worker, never the event loop.

The events table has id, ts, symphony, card_id, run_id, stage, source, kind, level,
message and JSON data. UTC timestamps plus increasing ids give stable ordering.
Indexes cover ts, (symphony, ts), (run_id, ts), (kind, ts). PRAGMA user_version
tracks independent migrations, without changing the state snapshot schema. Version 1
defines the base table; version 2 adds indexes and the optional full text index. FTS5
indexes message when supported; otherwise literal LIKE search is used.

Use DELETE rollback journaling, synchronous NORMAL and a busy timeout. WAL is
unsafe on the reference NFS state volume. A single daemon must own the volume.
Prefer local disk storage for the state volume. Set incremental auto vacuum when
creating the database. Existing versioned databases are migrated transactionally.

## Capture and privacy

Capture accepted activity entries at the shared ActivityLog append funnel,
independent of its SSE sink. Operator actions already share that path. Attribute
symphony from logging context or the daemon's card ownership map when available.
Capture performer payloads at the common monitor activity intake even without a
dashboard log. Keep full tool arguments, progress, errors and cost payloads, with
run_id and transcript_seq in data. Deduplicate cumulative performer batches with
bounded per-run identities. This does not add performer buffering or replay.
Events never received from a disconnected performer cannot be recovered here.

One non-blocking structlog processor captures INFO and above after context merge
and exception formatting. It excludes event store records and never logs its own
failures. Bootstrap and env cache records use this common logging funnel.
Every column passes through redact_state_value and sensitive-key redaction before
enqueueing. Unsupported objects become strings before redaction. Oversized rows
are dropped rather than retaining unbounded producer payloads in the queue.

## Retention and health

A validated event_store block defaults to retention_days 30 and max_bytes 2147483648.
No configuration is required. Maintenance deletes oldest expired rows first, then
oldest rows when the physical file exceeds the cap. Each transaction deletes at
most one batch, followed by a bounded incremental vacuum. Subsequent turns
continue reclamation, yielding between batches. The cap is an eventual bound:
queued inserts, indexes and the minimum SQLite schema footprint can exceed it
briefly. Retention cannot preserve a single row larger than the size budget.

Cached health contains size_bytes, rows, oldest_ts, dropped and pruned. The existing
System health panel renders these in one line, without filesystem work on the loop.
Drops and pruned counts cover the current daemon process; row and size statistics
are refreshed from disk. Storage failures count drops and appear on the health line.

## Query contract

`await query_events(filters, cursor, limit)` uses its own read-only connection in a
thread. Filters support inclusive start and end UTC times, symphony, card, run,
stage, source, kind, level and literal free text. Limits are bounded to 500 rows.
Newest first means ORDER BY ts DESC, id DESC. An opaque (ts, id) keyset cursor
avoids duplication under concurrent inserts and remains usable after pruning.
Invalid filters or cursors fail explicitly. Authorization and symphony scope must
be applied by the phase 2 callers; this internal service is not an exposed API.

## Validation and budgets

Test schema migration, optional FTS and fallback, all filters, paging ties,
redaction, capture funnels, failure isolation, overflow, many producers, retention
by age and physical size, bounded shutdown and cached health. Target at least
1000 persisted rows per second and less than 1 ms enqueue overhead per typical
1 KiB event on a local development disk. Measure both and record the results.

## Initial verification

The test-first run exited 2 for missing implementation modules. The focused suite
then passed. A representative 2000-row benchmark measured 1392 rows per second and
669.5 microseconds per enqueue on the development machine, including redaction.
The requested full-tree command reached an existing loopback HTTP contract test
and failed because the sandbox prohibits socket binding. It reported 22 passed,
3 skipped and 1 failure. No test or quality gate was changed to bypass that failure.
