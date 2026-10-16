# Implementation plan

Use existing History discovery and safe transcript readers. Aggregate canonical
event store usage by run and model in bounded SQL batches through the run and
time index, preserving ledger scope precedence and PriceTable unknown cost
handling. Capture accepted transcript usage in the existing store. Rehydrate
usage through TokenLedger only for runs predating store coverage. Join lifecycle
observations by explicit run identity, or require exactly one open run of the
same card and normalized stage for legacy observations. Count ambiguous or
unmatched observations. Keep completion separate from verdict outcomes and use
only verdict stages and recorded verdicts for success and first pass rates.
Mirror accepted lifecycle events into the existing SQLite store so snapshot
eviction does not discard future observations.

Keep source loading, pure aggregation, card flow and transport adapters separate.
Record human wait boundaries from monitor_pr's existing human versus automation
classification and preserve the latest boundary within the lifecycle snapshot cap.
One cache per dashboard application serializes cold reads and shares loaded
facts across requests. Cache expiry refreshes new records and retention changes.
All expensive work runs in worker threads. No schema or dependency changes.
