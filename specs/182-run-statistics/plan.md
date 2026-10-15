# Implementation plan

Use existing History discovery and safe transcript readers. Rehydrate retained
usage through TokenLedger, preserving message versus cumulative precedence and
PriceTable unknown cost handling. Join lifecycle verdicts to the preceding run
of the same card and stage. Mirror accepted lifecycle events into the existing
SQLite store so snapshot eviction does not discard future observations.

Keep source loading, pure aggregation, card flow and transport adapters separate.
Record human wait boundaries from monitor_pr's existing human versus automation
classification and preserve the latest boundary within the lifecycle snapshot cap.
One cache per dashboard application serializes cold reads and shares loaded
facts across requests. Cache expiry refreshes new records and retention changes.
All expensive work runs in worker threads. No schema or dependency changes.
