# T-A2 completion plan

Preserve Jacob's PR and integrate main without rewriting his commits. Keep JSONL
schema v1; advance workflow snapshots to v21 with optional attempt identity/path.

Use flat per-card state fields and the existing session round-trip to persist
attempts across daemon fan-out and restart. Start only after successful implementing
dispatch. End content attempts before opening a bounce child; retain the attempt
across non-content retries. Terminal nodes clear both identity fields together.
Derive verdict provenance honestly, use null for unknown restored timestamps, and
resolve the fallback log directory against the config/state location.

Validate with node/session/persistence tests, restart and midnight tests, a golden
two-bounce chain, write failures, lint, the full suite, independent review and CI.
Keep Jacob assigned and the primary squash author. Historical query/index and
orphan sweeping remain the separate T-A3/T-A4 work described in the original PR.

Review clarifications: persist the pending content-failure source with identity/path
so bounces survive restart without treating infrastructure retries as task failure.
Unknown restored start timestamps are null, as requested in the maintainer review.
No performer payload changes or dependencies are required.
