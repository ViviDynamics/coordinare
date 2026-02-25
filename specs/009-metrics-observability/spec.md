# Feature Specification: Metrics & Observability

**Feature Branch**: `009-metrics-observability`
**Created**: 2026-02-25
**Status**: Draft
**Input**: User description: "Metrics and observability improvements for the coordinare daemon — comprehensive Prometheus metrics coverage across all subsystems (board orchestrator, notification service, circuit breakers, config loading), structured log correlation (request/cycle IDs threaded through all log entries), a Grafana-compatible dashboard definition, health check endpoint enhancements (readiness vs liveness distinction, per-subsystem health), and a runbook template for each alert type so operators know exactly what to do when a Prometheus alert fires."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Comprehensive Metrics Coverage (Priority: P1)

As an operator, I want to query a single metrics endpoint and get counters and timing histograms for all coordinare subsystems (board orchestrator, notifications, circuit breakers, config loading), so that I can build dashboards, set up alerts, and spot anomalies without mining raw log files.

**Why this priority**: Without comprehensive metrics, operators have no signal that coordinare is working correctly other than checking logs manually. Metrics are the foundation on which alerting, dashboards, and runbooks all depend. No other observability feature delivers value without this.

**Independent Test**: Query the coordinare metrics endpoint. Verify all of the following metric families are present and non-zero after one poll cycle: poll cycle count, card transitions by type, notification dispatch count by channel, circuit breaker trip count, config load duration, and daemon uptime. Introduce a forced failure (e.g., simulate a notification delivery failure); verify the corresponding failure counter increments by exactly 1.

**Acceptance Scenarios**:

1. **Given** the daemon has completed at least one poll cycle, **When** an operator queries the metrics endpoint, **Then** the response includes: `cycles_completed_total`, `cards_processed_total` labelled by card status, `card_state_transitions_total` labelled by transition type, and `cycle_duration_seconds`.
2. **Given** a notification is delivered to a channel, **When** the metrics endpoint is queried, **Then** `notifications_dispatched_total` increments by 1 and is labelled with the event type and channel name.
3. **Given** a circuit breaker trips, **When** the metrics endpoint is queried, **Then** `circuit_breaker_trips_total` increments by 1, labelled with the affected service name.
4. **Given** the daemon starts successfully, **When** the metrics endpoint is queried, **Then** a `coordinare_build_info` metric is present with the coordinare version and start time as labels, and `config_load_duration_seconds` is present with a non-zero value.
5. **Given** a notification delivery fails after all retries, **When** the metrics endpoint is queried, **Then** `notifications_failed_total` increments by 1, labelled with the channel name.

---

### User Story 2 - Structured Log Correlation (Priority: P2)

As an operator debugging a production issue, I want every log entry emitted during a single poll cycle to carry the same unique cycle identifier, so that I can filter my log aggregator to show only the events from the cycle I care about and reconstruct exactly what happened.

**Why this priority**: When a poll cycle encounters an error, log entries from multiple concurrent or sequential cycles appear interleaved. A correlation ID makes it trivially easy to isolate all log entries from one cycle — the highest-impact debugging improvement after metrics coverage.

**Independent Test**: Run two sequential poll cycles. Capture all structured log output. Verify: (a) every coordinare-emitted log entry contains a `cycle_id` field; (b) all entries from cycle 1 share the same `cycle_id` value; (c) all entries from cycle 2 share a different `cycle_id` value; (d) no `cycle_id` value appears in both cycles.

**Acceptance Scenarios**:

1. **Given** a poll cycle begins, **When** any coordinare subsystem emits a log entry during that cycle, **Then** the entry contains a `cycle_id` field set to a string unique to that cycle.
2. **Given** a poll cycle completes successfully, **When** the log output is filtered by that cycle's `cycle_id`, **Then** all coordinare-emitted log entries for the cycle are returned and no entries from other cycles appear.
3. **Given** a poll cycle fails part-way through, **When** the log output is filtered by the failed cycle's `cycle_id`, **Then** the failure entry and all prior entries from that cycle share the same `cycle_id`.
4. **Given** the daemon emits non-cycle log entries (e.g., startup, shutdown, config load), **When** those entries are inspected, **Then** they do not contain a `cycle_id` field — correlation context does not leak outside cycle scope.

---

### User Story 3 - Health Check Enhancements (Priority: P3)

As an operator deploying coordinare in a container orchestration environment, I want separate liveness and readiness endpoints that individually report the health of each subsystem, so that the orchestrator can distinguish between "restart this container" and "stop routing traffic here" and take the correct action automatically.

**Why this priority**: The existing health endpoint provides a single binary status. Container orchestrators require liveness and readiness to be separate signals. Without this distinction, orchestrators make incorrect decisions: restarting a healthy container with a temporarily unavailable dependency, or failing to restart a genuinely stuck daemon.

**Independent Test**: Start coordinare with a deliberately invalid GitHub token. Verify the liveness endpoint returns 200 (daemon is alive). Verify the readiness endpoint returns a non-200 status and the response body identifies the GitHub subsystem as unavailable. Restore the valid token and verify the readiness endpoint returns 200 within one health check cycle.

**Acceptance Scenarios**:

1. **Given** the daemon is running and responsive, **When** the liveness endpoint is queried, **Then** it returns 200 regardless of whether subsystem connections are healthy.
2. **Given** a required subsystem (e.g., GitHub API) is unreachable, **When** the readiness endpoint is queried, **Then** it returns a non-200 status and the response body identifies the unavailable subsystem by name.
3. **Given** all required subsystems are healthy, **When** the readiness endpoint is queried, **Then** it returns 200 and the response body lists every subsystem with an individual status of healthy.
4. **Given** a subsystem health check exceeds the configured timeout, **When** the readiness endpoint responds, **Then** the timed-out subsystem is reported as degraded, the endpoint returns a non-200 status, and the response arrives within the configured timeout — it never hangs indefinitely.
5. **Given** an optional subsystem (operator-configured) is unavailable, **When** the readiness endpoint is queried, **Then** its degraded status is reported but the overall readiness remains 200 (non-critical subsystems do not block readiness).

---

### User Story 4 - Grafana-Compatible Dashboard (Priority: P4)

As an operator, I want a pre-built dashboard definition I can import into my monitoring system with a single action and immediately see all key coordinare metrics displayed in named, labelled panels — without building visualizations from scratch.

**Why this priority**: Comprehensive metrics without a dashboard require hours of manual panel construction. A pre-built definition closes the time-to-visibility gap from hours to minutes and ensures every operator starts from the same reliable baseline.

**Independent Test**: Import the provided dashboard definition into a Grafana instance with the coordinare metrics endpoint configured as the data source. Verify: all panels display data or a clear zero value (no "no data" errors), no manual panel editing is required beyond the data source, and the dashboard includes panels for all metric families from User Story 1.

**Acceptance Scenarios**:

1. **Given** the dashboard definition file is committed to the repository, **When** an operator imports it with only the data source URL changed, **Then** all panels display data without further configuration.
2. **Given** a coordinare event occurs (cycle completion, notification dispatch, circuit breaker trip), **When** the dashboard is viewed after the next scrape interval, **Then** the corresponding panel updates to reflect the new value.
3. **Given** a metric has never incremented (e.g., no circuit breaker trips today), **When** the dashboard is displayed, **Then** the panel shows 0 rather than "no data."
4. **Given** a new coordinare version is deployed, **When** the dashboard is viewed, **Then** a build information panel shows the current version and deploy time without any dashboard edits.

---

### User Story 5 - Alert Runbooks (Priority: P5)

As an on-call operator, I want a runbook for each alert rule defined in the dashboard, so that I know exactly what the alert means, how to diagnose the root cause, and what to do to resolve it — even without deep knowledge of coordinare internals.

**Why this priority**: Metrics and alerts without runbooks create alert fatigue. An on-call operator receiving an alert without guidance either ignores it or escalates unnecessarily. Runbooks transform alerts from noise into actionable operations, completing the observability loop.

**Independent Test**: Enumerate all alert rules defined in the dashboard. For each alert rule, verify a corresponding runbook document exists at the path referenced in the alert rule, and the runbook contains: the trigger condition, operational impact, at least three diagnostic steps, at least one resolution action, and an escalation path.

**Acceptance Scenarios**:

1. **Given** an alert rule exists in the dashboard definition, **When** the operator follows the runbook URL referenced in that alert rule, **Then** the runbook document is found at that path.
2. **Given** an operator opens a runbook, **When** they follow the diagnostic steps, **Then** each step is a specific, executable action that produces an observable outcome (no vague suggestions).
3. **Given** a runbook is followed and the issue is not resolved, **When** the operator reaches the escalation section, **Then** it specifies who to contact, what information to provide, and the expected response time.
4. **Given** a new alert rule is added to the dashboard definition, **When** the automated test suite runs, **Then** the missing runbook is detected as a test failure — the 1-alert-1-runbook invariant is enforced automatically.

---

### Edge Cases

- What if a subsystem health check itself times out? The readiness endpoint must return a degraded status for that subsystem within the configured check timeout; it must never hang indefinitely regardless of subsystem responsiveness.
- What if the metrics endpoint is scraped during a high-load poll cycle? Metric collection must be non-blocking — it must not delay the poll cycle or add meaningful processing overhead.
- What if a log library used by a third-party dependency does not propagate the cycle ID? Only coordinare-owned log entries are required to carry the correlation ID; third-party entries without a `cycle_id` field are acceptable and do not constitute a violation.
- What if a metric counter was never incremented (e.g., no circuit breaker trips since daemon start)? Zero-value metrics must still be exposed so dashboards show 0 rather than "no data."
- What if the dashboard definition references a metric that does not exist in an older coordinare version? Panels for undefined metrics must gracefully show "no data" rather than causing a dashboard import failure.
- What if two poll cycles run concurrently? Each cycle must have a distinct `cycle_id`; concurrent cycles must not share or overwrite each other's correlation context.

---

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The metrics endpoint MUST expose the following board orchestrator metrics: `cycles_completed_total` (counter), `cards_processed_total` (counter, labelled by card status), `card_state_transitions_total` (counter, labelled by transition type), `cycle_duration_seconds` (histogram), and `daemon_up` (gauge, 1 when running).
- **FR-002**: The metrics endpoint MUST expose the following notification system metrics: `notifications_dispatched_total` (counter, labelled by event type and channel name), `notifications_failed_total` (counter, labelled by channel name), `notifications_rate_limited_total` (counter, labelled by channel name), `notifications_deduplicated_total` (counter, labelled by channel name).
- **FR-003**: The metrics endpoint MUST expose circuit breaker metrics: `circuit_breaker_trips_total` (counter, labelled by service name), `circuit_breaker_state` (gauge, 0=closed/1=half-open/2=open, labelled by service name).
- **FR-004**: The metrics endpoint MUST expose `config_load_duration_seconds` (histogram) and `coordinare_build_info` (Info metric — a labelled gauge that always holds value 1 — with version, start time, and runtime version as labels; using `prometheus_client.Info` which exposes as `coordinare_build_info_info{...} 1.0`).
- **FR-005**: Every coordinare-owned structured log entry emitted during a poll cycle MUST carry a `cycle_id` field containing a string unique to that cycle; the same `cycle_id` value MUST appear on every coordinare log entry from the same cycle.
- **FR-006**: The system MUST provide a liveness endpoint that returns HTTP 200 with body `{"status": "alive"}` as long as the daemon process is running and responsive, regardless of subsystem availability.
- **FR-007**: The system MUST provide a readiness endpoint that returns HTTP 200 only when all required subsystems are operational; the response body MUST include a per-subsystem status list with the name and health status (healthy/degraded/unavailable) of each subsystem.
- **FR-008**: Each individual subsystem health check MUST complete within a configurable timeout (`health_check_timeout_seconds`, default: 2 seconds); the readiness endpoint MUST always return a response within `health_check_timeout_seconds × subsystem_count + 1 second` and MUST never hang. `subsystem_count` is the number of subsystems registered in the `HealthRegistry` at the time of the request (default: 4 subsystems — github, agent_ssh, notifications, config).
- **FR-009**: Operators MUST be able to mark individual subsystems as optional in `config.yaml`; degraded optional subsystems MUST be reported in the readiness response but MUST NOT cause the overall readiness status to return non-200.
- **FR-010**: The system MUST provide a Grafana-compatible dashboard definition file committed to the repository at `docs/dashboards/coordinare.json`; the dashboard MUST include panels for all metric families in FR-001 through FR-004 and MUST require only a data source URL change before import.
- **FR-011**: The dashboard definition MUST include exactly the following four alert rules (one per metric family from FR-001 through FR-003): `CoordinareHighCycleFailureRate` (board orchestrator), `CoordinareLongCycleDuration` (board orchestrator), `CoordinareNotificationFailures` (notifications), `CoordinareCircuitBreakerOpen` (circuit breaker); each alert rule MUST reference a runbook URL in the format `docs/runbooks/<AlertName>.md`.
- **FR-012**: A runbook Markdown document MUST exist for every alert rule defined in FR-011; each runbook MUST contain sections for: trigger condition, operational impact, diagnostic steps (minimum 3), resolution actions (minimum 1), and escalation path.
- **FR-013**: An automated test MUST verify the 1-alert-1-runbook invariant: for every alert rule in the dashboard definition, a corresponding runbook file MUST exist at the referenced path; this test MUST fail if any runbook is missing.

### Key Entities

- **MetricDefinition**: A single tracked measurement point. Attributes: name (dotted notation), metric type (counter/gauge/histogram), label keys, description string.
- **HealthProbe**: A single health check evaluation result. Attributes: subsystem name, probe type (liveness/readiness), status (healthy/degraded/unavailable), checked_at timestamp, is_required (bool), details (human-readable operator-friendly description of any failure — MUST NOT contain raw exception messages or stack traces).
- **HealthReport**: The aggregated result of all health probes for a single readiness check. Attributes: overall_status, probe results (list of HealthProbe), response_time_ms.
- **CycleContext**: The correlation context bound to a single poll cycle. Attributes: cycle_id (unique string per session), started_at timestamp. Scoped to one cycle duration; never persisted.
- **AlertRule**: A threshold condition on a metric, defined within the dashboard. Attributes: alert name, metric expression, threshold, severity (warning/critical), runbook_path.
- **Runbook**: Operational procedure for a specific alert. Attributes: alert_name, trigger_condition, operational_impact, diagnostic_steps (ordered list), resolution_actions (ordered list), escalation_path.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All metric counters from FR-001 and FR-002 increment by the correct expected amount within one poll cycle of the triggering event — verified by automated tests that fire known events and assert the exact counter delta.
- **SC-002**: 100% of coordinare-owned log entries emitted during a poll cycle carry the correct `cycle_id` value — verified by automated test: zero coordinare log entries from any completed cycle are found without the field.
- **SC-003**: The liveness endpoint responds in under 100ms on a normally-loaded system; the readiness endpoint always responds within `health_check_timeout_seconds × subsystem_count + 1 second` regardless of subsystem state.
- **SC-004**: When a required subsystem becomes unavailable, the readiness endpoint correctly reports it as degraded within one health check cycle; zero false positives occur in automated tests under normal operating conditions.
- **SC-005**: An operator can import the dashboard definition, configure the data source, and achieve full metric visibility in under 5 minutes — verified by a documented import walkthrough with no more than 3 steps.
- **SC-006**: Zero alert rules exist without a corresponding runbook; the automated runbook-coverage test (FR-013) passes on every CI run.
- **SC-007**: Metric collection adds no more than 10ms to average poll cycle duration — verified by benchmarking poll cycle duration with and without metrics instrumentation active.
- **SC-008**: The dashboard definition imports without errors into a standard monitoring system when only the data source URL is changed; all panels display data or 0 (no "no data" errors on defined metrics).

---

## Assumptions

- **A-001**: The health check HTTP server already exists (spec 001 FastAPI health app); this spec adds `/live` and `/ready` paths and per-subsystem status to the existing server — no new server process or port is introduced.
- **A-002**: The metrics scraping endpoint already exists (spec 001 prometheus-client integration); this spec adds new metric definitions within the existing registry — no new metrics library is introduced.
- **A-003**: Cycle IDs are session-scoped ephemeral identifiers; cross-session or persistent correlation is out of scope. The format (UUID4, sequential integer, etc.) is an implementation choice; only uniqueness within a session is required.
- **A-004**: Per-subsystem health checks use the last-known state updated by the daemon's poll cycle (cached), not live connectivity probes on every readiness request, to prevent health check latency from coupling to external API latency.
- **A-005**: The Grafana JSON dashboard format is the target import format; operators using other monitoring systems may need to adapt the definition file.
- **A-006**: Runbook documents are Markdown files committed to the repository under `docs/runbooks/`; they are not dynamically generated or served.
- **A-007**: The `circuit_breaker_state` and `circuit_breaker_trips_total` metrics are emitted by spec 005; this spec defines the metric names, label conventions, and dashboard panels — spec 005 is responsible for writing the values.
- **A-008**: Notification metrics (FR-002) are emitted by spec 006's `NotificationService`; this spec defines the metric names and ensures they are correctly labelled — spec 006 is responsible for incrementing the counters.
- **A-009**: The `cycle_id` is set on a context variable (e.g., a Python context variable or structlog context binding) at the start of each poll cycle and is automatically included on all log entries emitted within that execution context.
