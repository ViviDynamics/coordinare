# Changelog

All notable changes to coordinare are documented here.

## [Unreleased]

### Added: 056 — Containerized Performer Execution

- **Containerized execution modes**: Performers can now run as ephemeral containers (started on demand, torn down after job completion) or persistent containers (long-running, reused across jobs), alongside the existing subprocess mode.
- **Multi-performer dispatch pool**: Coordinare maintains a dynamic pool of registered performers, automatically selects idle candidates matching role requirements, and falls over to alternatives when the first choice is busy.
- **Image variants**: Three published image families support different operator needs: `full` (all backends + QA tooling), `slim-<backend>` (single backend, optional browser), and `base` (toolchain only, BYO CLI). Operators can also supply custom images that satisfy the documented HTTP contract.
- **Secrets handling with precedence**: Three sources (job-init payload, environment variables, mounted creds files) provide secrets with deterministic precedence. Missing secrets surface as actionable errors without logging secret values.
- **Optional bearer-token authentication**: Each performer can be assigned a shared secret token that the coordinare presents on every request; authentication is optional and disable-able for local/dev deployments.
- **Capability mismatch detection**: Configuration errors (e.g., image missing a required backend or tool) are surfaced at startup, not during job dispatch.
- **Operator visibility**: Exclusion and recovery events for unreachable performers are emitted through existing notification channels (dashboard, logs, metrics, alerts). Pool state is visible via a dashboard widget.

### Changed

- Existing subprocess performers are unaffected; containerization is entirely opt-in via configuration.
- `CoordinareState` extended with `performer_endpoints` (in-memory registry tracking container availability).

### Performance

- Typical `/status` poll latency: p95 < 250ms (100 iterations against persistent performer).
- Job dispatch ack latency: p95 < 500ms.
- Ephemeral image cold-start: ≤ 60s (slim), ≤ 120s (full).
- No regression on subprocess performer throughput (SC-008).

---

## Earlier Releases

See git history for pre-056 releases.
