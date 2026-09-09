# Website symphony QA corrections — 2026-09-09

The first live QA pass on `166682f5` exposed four defects, tracked in #310–#313.

- With the config assistant disabled, its missing initialization function aborted the dashboard's `DOMContentLoaded` callback before the quiet timer started. Optional initialization is now guarded.
- The symphony overview read bootstrap fields absent from SSE snapshots. Snapshots now include ready, success/failure, in-flight, and error fields from the same environment cache as the HTTP API.
- Third-party HTTP logs bypassed structured logging and exposed Slack webhook URLs. Standard-library handlers now redact their final formatted output, including exceptions and stack traces. Structured string values also use value-based redaction. Tests use synthetic credentials; never attach raw deployment logs.
- A long historical comment backlog held the entire session fanout open. Classification now processes an ordered prefix of at most five new comments within twenty seconds per tick. A model call has at most fifteen seconds, then uses the existing keyword fallback. Deferred comments stay beyond the watermark and are fetched on the next tick; processed IDs still suppress duplicates. Shutdown cancellation propagates normally.

The comment budget covers classification, not board retrieval or the rest of a card tick. This bounds the observed backlog delay without changing the fanout merge or discarding comments. Health and dashboard snapshots still update at cycle boundaries.
