# Plan

Extend stats_flow for merge cohorts and run_statistics for bounded recent runs,
reliability and live metadata. The stats router supplies existing snapshot metadata
and effective capacity, preserving redaction. Capture board poll outcomes in the
existing event store. No new store, MCP tool or aggregation in the browser.

Add generic stats_charts.js and a symphony_stats.js renderer and request coordinator.
Wire the read only detail page and list headlines through that coordinator. Reuse
the existing symphony status, waiting, vocabulary, time and bootstrap renderers.

Validate with fixed API records, executable Node checks, regression selections,
ruff, mypy and the ruff ratchet. Supply browser tests for user execution.
