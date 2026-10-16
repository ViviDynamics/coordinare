# Persona observability

The read only Personas page selects one agent and a 24h, 7d or 30d start
cohort. All statistics come from GET /api/stats with agent and symphony
filters. Extend that service for recent runs, cost per run and version
breakdowns, reusing its bounded source cache and accounting rules.

Show runs, verdict success and first pass rates, median and p90 duration,
tokens and USD cost. A missing denominator is not recorded. Partial usage
shows partial instead of a precise total. Expose source coverage and metric
and window tooltips. Empty cohorts say there are no runs.

Show all recorded outcomes, daily runs, success and cost with accessible SVG
text alternatives, current resolved stage, model, mode and performer host,
symphony overrides, current activity, ten recent transcript links, an agent
filtered History link, failure reasons, and recorded persona version measures.
Version identity includes symphony and content hash. Use recorded edit times
as chart markers without inferring versions for legacy runs. Shared history
edits after the last run are marked with their unrecorded symphony scope,
without assigning those edits to a symphony or to older runs.

Use existing theme tokens, vocabulary and timeHtml. Keep instructions read only
and editing in Settings. At 375px tables scroll within their containers. Use
one generic stats_charts.js for reuse by issue 184. No new dependencies, stores,
MCP tools or write routes. Runtime configuration must be scoped and redacted.

Budgets: at most ten recent rows, existing 2000 retained runs, one stats request
per selection and no more than one automatic refresh per 30 seconds. New chart
helpers remain below 8 KiB. Browser verification covers both themes at 375px.
