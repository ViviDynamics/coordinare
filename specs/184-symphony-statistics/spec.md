# Symphony statistics

Provide a read only symphony detail page with identity, health, current board counts,
capacity and waiting reasons, throughput, flow, spend, reliability and recent activity.
Use GET /api/stats for every statistic. Support rolling 24h, 7d and 30d windows;
the list headlines use the same rolling 7d cohort as the default detail page.

Extend the existing statistics service only for missing measures: UTC daily completed
cards, recent merged cards and runs, cost per completed card, live symphony metadata,
and recorded board check reliability. Reuse bounded event reads and source caching.
New board check observations use the existing event store. Legacy periods without
observations say Not recorded; never infer a rate from consecutive failures.

Show partial coverage explicitly, including tokens and cost. Use the shared time,
stage, column and waiting vocabulary helpers. Each measure has a definition and
window tooltip. Charts are plain SVG with text alternatives in stats_charts.js.
Recent activity links to card timelines, transcripts and filtered History.

Preserve symphony scope and server redaction. Keep env cache and bootstrap status
visible without mutation controls. Both themes and a 375px viewport must work.
Leave changes uncommitted. Browser execution and the full suite are user run.

This checkout predates dependency #181. The detail route becomes read only here;
moving the former editor and migrating its older browser tests remains with #181.
Existing list management controls remain in place for that dependency to move.
