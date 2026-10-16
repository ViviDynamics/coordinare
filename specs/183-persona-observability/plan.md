# Plan

Extend the existing stats measures and retained header projection for cost per
run, recent runs and version cohorts. Add a runtime projection to the same API
using effective symphony configuration and observed active sessions. Add a real
persona filter to History rather than approximating an agent with its stage.

Add a dedicated personas_stats.js renderer and generic stats_charts.js. Keep
the existing instructions and last edited metadata. Use group_by=symphony so
one response supplies totals, daily data, and the symphony table. Guard stale
responses on agent, window, scope and navigation changes. Refresh on SSE with
throttling and keep controls stable while loading. Reuse the existing source
cache; runtime data is read fresh for each request.

Verify with fixed record API tests, Node rendering and asynchronous selection
tests, and new browser tests for responsive themes, links and read only behavior.
