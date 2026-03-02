---

description: "Task list for Metrics & Observability feature implementation"
---

# Tasks: Metrics & Observability

**Input**: Design documents from `/specs/009-metrics-observability/`
**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/ ✅

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create directory scaffolding needed by multiple user stories.

- [X] T001 Create docs/dashboards/ directory for Grafana dashboard file
- [X] T002 [P] Create docs/runbooks/ directory for runbook Markdown files

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core `observability.py` module used by US1 (health registry), US2 (cycle_id), and US3 (health endpoints). Must be complete before user story work begins.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T003 Create src/coordinare/observability.py with `HealthStatus` enum (healthy/degraded/unavailable), `HealthProbe` dataclass (subsystem_name, status, is_required, checked_at, details), `HealthReport` dataclass (overall_status, probes, response_time_ms), `HealthRegistry` class with `register()`, `update()`, `snapshot()`, stale-detection logic in `snapshot()` using `health_check_timeout_seconds`
- [X] T004 [P] Add `bind_cycle_id(cycle_id: str) -> None` and `clear_cycle_id() -> None` to src/coordinare/observability.py — thin wrappers around `structlog.contextvars.bind_contextvars` / `clear_contextvars`; add `CycleContext` dataclass (cycle_id, started_at)
- [X] T005 [P] Add module-level `HEALTH = HealthRegistry()` singleton to src/coordinare/observability.py; export `HEALTH`, `HealthRegistry`, `HealthProbe`, `HealthReport`, `HealthStatus`, `bind_cycle_id`, `clear_cycle_id`, `CycleContext` in `__all__`

**Checkpoint**: `observability.py` complete — US1, US2, and US3 can now begin.

---

## Phase 3: User Story 1 — Comprehensive Metrics Coverage (Priority: P1) 🎯 MVP

**Goal**: Expose 7 new Prometheus metric families for board orchestrator and config subsystems; register cross-spec metric names (notifications, circuit breakers) with zero-value initialization so dashboards never show "no data."

**Independent Test**: Query `/metrics` after one poll cycle. Verify all of: `cycles_completed_total`, `cards_processed_total`, `card_state_transitions_total`, `cycle_duration_seconds`, `daemon_up`, `config_load_duration_seconds`, `coordinare_build_info`, `notifications_dispatched_total`, `circuit_breaker_trips_total` are present with non-negative values.

- [X] T006 [P] [US1] Add `cycles_completed_total` (Counter), `card_state_transitions_total` (Counter, label: transition_type with values: idle_to_dispatch, dispatch_to_monitor, monitor_to_merge, monitor_to_blocked, blocked_to_idle), `cycle_duration_seconds` (Histogram, buckets: 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0), `daemon_up` (Gauge, replaces existing `up` attribute — rename `up` → `daemon_up` in `CoordinareMetrics` and update all existing call sites) to `CoordinareMetrics` in src/coordinare/metrics.py; also confirm `cards_processed_total` already exists from spec-001 integration and preserve it unchanged
- [X] T007 [P] [US1] Add `config_load_duration_seconds` (Histogram), `coordinare_build_info` (`prometheus_client.Info`, labels: version, python_version, started_at — exposes as `coordinare_build_info_info{...} 1.0`) to `CoordinareMetrics` in src/coordinare/metrics.py
- [X] T008 [P] [US1] Register cross-spec metric names (counters and gauges) in `CoordinareMetrics` for zero-value exposure: `notifications_dispatched_total` (Counter, labels: event_type, channel_name), `notifications_failed_total` (Counter, label: channel_name), `notifications_rate_limited_total` (Counter, label: channel_name), `notifications_deduplicated_total` (Counter, label: channel_name), `circuit_breaker_trips_total` (Counter, label: service_name), `circuit_breaker_state` (Gauge, 0=closed/1=half-open/2=open, label: service_name) — all in src/coordinare/metrics.py
- [X] T009 [US1] Add `_initialize_zero_values()` method to `CoordinareMetrics` in src/coordinare/metrics.py — called from `__init__()` to pre-populate all label combinations so `/metrics` exposes zero values immediately (before any events fire); enumerate: all `transition_type` values for `card_state_transitions_total` (idle_to_dispatch, dispatch_to_monitor, monitor_to_merge, monitor_to_blocked, blocked_to_idle), all `card_status` values for `cards_processed_total` (dispatched, monitoring_agent, monitoring_pr, blocked, merged, idle), representative `channel_name` values for notification counters (slack, email), and `service_name` values for circuit breaker (github, agent_ssh, slack, smtp)
- [X] T010 [US1] Add `health_check_timeout_seconds: int = Field(default=2, ge=1, le=30)` and `optional_subsystems: list[str] = Field(default_factory=list)` to `ProjectConfiguration` in src/coordinare/config.py
- [X] T011 [US1] Modify src/coordinare/daemon.py: after successful `graph.ainvoke()`, call `METRICS.cycles_completed_total.inc()`, `METRICS.cycle_duration_seconds.observe(elapsed)`, and record `card_state_transitions_total` based on `state["phase"]` transitions; set `METRICS.daemon_up.set(1)` at startup and `METRICS.daemon_up.set(0)` in shutdown
- [X] T012 [US1] Modify src/coordinare/__main__.py: record `config_load_duration_seconds` timing around config loading; set `coordinare_build_info` labels (version from importlib.metadata or `__version__`, python_version from platform.python_version(), started_at as ISO timestamp)
- [X] T013 [P] [US1] Write tests/unit/test_metrics_coverage.py with: `test_cycles_completed_total_increments_on_cycle`, `test_card_state_transitions_total_uses_correct_labels`, `test_cycle_duration_seconds_observes_positive_value`, `test_config_load_duration_recorded_at_startup`, `test_coordinare_build_info_always_1`, `test_zero_value_metrics_present_before_any_events`, `test_notification_counters_registered_in_registry`, `test_circuit_breaker_counters_registered_in_registry`

**Checkpoint**: US1 independently testable — query `/metrics` and verify all metric families present.

---

## Phase 4: User Story 2 — Structured Log Correlation (Priority: P2)

**Goal**: Every coordinare log entry emitted during a poll cycle carries the same `cycle_id` UUID; non-cycle entries (startup, heartbeat, shutdown) have no `cycle_id`.

**Independent Test**: Capture structured log output over 2 sequential cycles. Assert: (a) all cycle-1 entries share one `cycle_id`; (b) all cycle-2 entries share a different `cycle_id`; (c) startup and heartbeat entries have no `cycle_id` field.

- [X] T014 [US2] Modify src/coordinare/daemon.py `start()` method: at the top of each cycle iteration, generate `cycle_id = str(uuid4())` and call `bind_cycle_id(cycle_id)` (imported from observability.py); in the `finally` block of the cycle iteration, call `clear_cycle_id()` — ensuring cycle_id is cleared regardless of success or exception
- [X] T015 [P] [US2] Write tests/unit/test_cycle_correlation.py with: `test_cycle_id_present_on_log_entries_during_cycle`, `test_cycle_id_absent_on_log_entries_outside_cycle` (startup/heartbeat/shutdown logs), `test_cycle_id_unique_across_sequential_cycles`, `test_clear_cycle_id_removes_field_from_subsequent_logs`, `test_exception_in_cycle_clears_cycle_id_in_finally`

**Checkpoint**: US2 independently testable — run daemon for 2 cycles, inspect log output for cycle_id field presence and uniqueness.

---

## Phase 5: User Story 3 — Health Check Enhancements (Priority: P3)

**Goal**: `/live` always returns 200 (process alive); `/ready` returns 503 with per-subsystem status when any required subsystem is degraded; `/ready` returns 200 when all required subsystems are healthy; optional subsystems don't block readiness.

**Independent Test**: Start daemon with deliberate GitHub degradation (stale last_poll_at). Verify `/live` returns 200. Verify `/ready` returns 503 with GitHub shown as degraded and required=true. Resolve degradation. Verify `/ready` returns 200 within one poll cycle.

- [X] T016 [US3] Modify src/coordinare/health.py: add `GET /live` route returning `{"status": "alive"}` with 200 unconditionally; enhance `GET /ready` to call `HEALTH.snapshot()`, serialize `HealthReport` per contracts/health-endpoints.md schema (status, subsystems[], response_time_ms), return 200 when `overall_status == healthy` else 503; accept `HealthRegistry` via dependency injection (FastAPI `Depends` or constructor param) for testability; keep existing `/health` route unchanged
- [X] T017 [US3] Modify src/coordinare/daemon.py: update `HEALTH.update("github", HealthStatus.HEALTHY)` after successful board poll; update `HEALTH.update("github", HealthStatus.DEGRADED, details=str(exc))` on GitHub-related exception; update `HEALTH.update("agent_ssh", ...)` based on agent dispatch result in state
- [X] T018 [US3] Modify src/coordinare/__main__.py: initialize `HEALTH` registry — call `HEALTH.register("github", required=True)`, `HEALTH.register("agent_ssh", required=True)`, `HEALTH.register("notifications", required=False)`, `HEALTH.register("config", required=True)`; override `required` for subsystems listed in `config.optional_subsystems`; pass `HEALTH` and `config.health_check_timeout_seconds` to `HealthRegistry` / `create_health_app()`
- [X] T019 [P] [US3] Write tests/unit/test_health_endpoints.py with: `test_live_always_200`, `test_live_returns_alive_body`, `test_ready_200_when_all_required_healthy`, `test_ready_503_when_required_subsystem_degraded`, `test_ready_200_when_only_optional_subsystem_degraded`, `test_ready_includes_all_registered_subsystems`, `test_ready_marks_stale_probe_as_degraded`, `test_ready_response_time_ms_is_present_and_positive`, `test_optional_subsystems_config_registers_subsystem_as_not_required` (verify that a subsystem name in `config.optional_subsystems` is registered with `is_required=False` and its degraded status does not affect overall readiness — satisfies FR-009)
- [X] T020 [US3] Update tests/unit/test_health_api.py: update `/ready` response assertions to match new schema (subsystems list, response_time_ms); add `/live` assertions; keep existing `/health` tests unchanged

**Checkpoint**: US3 independently testable — both /live and /ready respond correctly under healthy and degraded conditions.

---

## Phase 6: User Story 4 — Grafana-Compatible Dashboard (Priority: P4)

**Goal**: Operators can import `docs/dashboards/coordinare.json` into Grafana with only the data source URL changed and immediately see all metric families from US1 visualized.

**Independent Test**: Import the JSON file into a Grafana instance pointed at a Prometheus data source scraping coordinare. Verify all panels display data or 0 (no "no data" errors). Verify all panels for all metric families from FR-001 through FR-004 are present.

- [X] T021 [US4] Create docs/dashboards/coordinare.json as a valid Grafana JSON dashboard containing: panels for all 7 metric families (cycles_completed_total, cards_processed_total, card_state_transitions_total, cycle_duration_seconds, notifications_dispatched_total/failed_total, circuit_breaker_state, config_load_duration_seconds, coordinare_build_info); dashboard title "Coordinare Daemon"; 4 alert rules embedded in relevant panels (CoordinareHighCycleFailureRate, CoordinareLongCycleDuration, CoordinareNotificationFailures, CoordinareCircuitBreakerOpen); each alert rule includes `"runbookURL"` annotation pointing to `docs/runbooks/<AlertName>.md`; `"__inputs"` block for data source variable so operators change only one field on import

**Checkpoint**: US4 independently testable — JSON file is valid Grafana format, imports without errors.

---

## Phase 7: User Story 5 — Alert Runbooks (Priority: P5)

**Goal**: A runbook Markdown file exists for every alert rule in the dashboard; each runbook has all 5 required sections; an automated test enforces the 1-alert-1-runbook invariant.

**Independent Test**: Enumerate all alert rules in coordinare.json. For each, verify the runbook file exists at the referenced path and contains all required section headings.

- [X] T022 [P] [US5] Create docs/runbooks/_template.md with the 5 required section headings: `## Trigger Condition`, `## Operational Impact`, `## Diagnostic Steps`, `## Resolution Actions`, `## Escalation Path`; add a note that Diagnostic Steps requires ≥3 numbered items and Resolution Actions requires ≥1
- [X] T023 [P] [US5] Create docs/runbooks/CoordinareHighCycleFailureRate.md: trigger (errors_total rate > 10% over 5 min), impact (cards not being processed), 3 diagnostic steps, 1 resolution, escalation path
- [X] T024 [P] [US5] Create docs/runbooks/CoordinareLongCycleDuration.md: trigger (p95 cycle_duration_seconds > 30s over 10 min), impact (cards processed slowly), 3 diagnostic steps, 1 resolution, escalation path
- [X] T025 [P] [US5] Create docs/runbooks/CoordinareNotificationFailures.md: trigger (notifications_failed_total > 0), impact (operators not receiving alerts), 3 diagnostic steps, 1 resolution, escalation path
- [X] T026 [P] [US5] Create docs/runbooks/CoordinareCircuitBreakerOpen.md: trigger (circuit_breaker_state > 0 for any service), impact (coordinare isolating a degraded service), 3 diagnostic steps, 1 resolution, escalation path
- [X] T027 [US5] Write tests/unit/test_runbook_coverage.py with: `test_dashboard_is_valid_json` (parse docs/dashboards/coordinare.json without error), `test_every_alert_has_runbook` (for each runbookURL in dashboard alert rules, assert file exists at that path), `test_runbooks_have_required_sections` (for each runbook, assert all 5 required section headings are present: `## Trigger Condition`, `## Operational Impact`, `## Diagnostic Steps`, `## Resolution Actions`, `## Escalation Path`), `test_runbooks_have_minimum_content_depth` (for each runbook, assert `## Diagnostic Steps` section contains ≥3 numbered list items and `## Resolution Actions` section contains ≥1 numbered list item — per FR-012)

**Checkpoint**: US5 independently testable — test_runbook_coverage.py passes; all runbook links resolve.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Final cleanup and integration validation across all user stories.

- [X] T028 [P] Add SC-007 performance benchmark to tests/unit/test_metrics_coverage.py: `test_metric_collection_overhead_under_10ms` — measure the wall-clock cost of calling `.inc()`, `.observe()`, and `.set()` for all new metrics in a single cycle's worth of operations using `time.perf_counter()`; assert total overhead ≤ 10ms (satisfies SC-007 and Constitution Principle IV)
- [X] T029 [P] Update docs/quickstart.md to add a dashboard import walkthrough section: document the exact 3-step import procedure (upload coordinare.json → select data source → click Import) so SC-005 (import in < 5 minutes with ≤ 3 steps) is verifiably satisfied
- [X] T030 [P] Update CLAUDE.md (project agent context) via `.specify/scripts/bash/update-agent-context.sh claude` to reflect 009 tech additions
- [X] T031 Run full test suite (pytest tests/unit/) and confirm all existing tests pass alongside new tests; fix any regressions from daemon.py or health.py modifications
- [X] T032 [P] Verify ruff linting passes on all new and modified files (observability.py, metrics.py, health.py, daemon.py, config.py, __main__.py)
- [X] T033 Validate quickstart.md procedures — confirm `/live`, `/ready`, `/metrics` all respond correctly after a fresh daemon start with a minimal config

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 completion — BLOCKS US2 (cycle_id) and US3 (HealthRegistry)
- **US1 (Phase 3)**: Depends on Phase 2 (observability.py for HealthRegistry used by config); starts after Foundation
- **US2 (Phase 4)**: Depends on Phase 2 (observability.py bind/clear); T014 modifies daemon.py — coordinate with T011 (also modifies daemon.py); recommend sequencing US1 daemon changes (T011) before US2 daemon changes (T014) if single developer
- **US3 (Phase 5)**: Depends on Phase 2 (HealthRegistry); T017–T018 modify daemon.py/__main__ — coordinate with T011–T012 (US1 changes to same files); T016 modifies health.py — no conflict with US1/US2
- **US4 (Phase 6)**: No code dependencies — can start in parallel with US1, US2, US3
- **US5 (Phase 7)**: Depends on US4 (T021 creates coordinare.json which T027 reads); T022–T026 can start after T021
- **Polish (Phase 8)**: Depends on all user stories complete; T028 (SC-007 benchmark) depends on T006–T012; T029 (quickstart) depends on T021; T031 (full test suite) depends on all prior phases

### User Story Dependencies (within daemon.py)

T011 (US1 metrics), T014 (US2 cycle_id), and T017 (US3 health updates) all modify `CoordinareDaemon.start()`. If working as a single developer, complete these in order: T011 → T014 → T017. If parallel team, coordinate on the `start()` method edit.

### Parallel Opportunities

```bash
# All Phase 1 + Phase 2 tasks: run together
T001, T002 (Setup), then T003, T004, T005 (Foundation)

# After Foundation complete, US1 tests can run in parallel with US1 implementation:
T006, T007, T008 (metrics additions) + T013 (metrics tests) — all in parallel

# US4 dashboard + US5 template can start immediately after Phase 1:
T021 (dashboard JSON) → T022, T023, T024, T025, T026 (runbooks) → T027 (invariant test)

# US2 and US3 tests can be written in parallel with implementation:
T015 (correlation tests) alongside T014 (daemon cycle_id change)
T019 (health endpoint tests) alongside T016 (health.py change)
```

### Within Each User Story

- Metrics registration (T006–T009) before daemon usage (T011–T012)
- `observability.py` complete before daemon and health modifications (T014, T016, T017)
- Dashboard JSON complete (T021) before runbook coverage test (T027)
- All implementation complete before final regressions run (T029)

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001–T002)
2. Complete Phase 2: Foundational (T003–T005)
3. Complete Phase 3: User Story 1 — metrics (T006–T013)
4. **STOP and VALIDATE**: Query `/metrics` after one poll cycle; verify all metric families present
5. Deploy/demo if ready

### Incremental Delivery

1. Setup + Foundation → `observability.py` module ready
2. US1 → Full metrics coverage → Deploy/Demo (MVP!)
3. US2 → Cycle correlation → `/metrics` + correlated logs
4. US3 → Health endpoints → Container orchestration ready
5. US4 → Dashboard → Instant Grafana visibility
6. US5 → Runbooks + invariant test → Alert-to-runbook loop closed

### Parallel Team Strategy

With multiple developers (after Foundation complete):
- Developer A: US1 (metrics.py + daemon metrics recording)
- Developer B: US2 (daemon cycle_id) + US3 (health.py)
- Developer C: US4 (dashboard JSON) + US5 (runbooks)

Coordinate: B's US2/US3 changes and A's US1 changes all touch daemon.py — either merge carefully or designate one owner for daemon.py.

---

## Notes

- [P] tasks = different files, no dependencies within the same phase
- [Story] label maps task to specific user story for traceability
- T003–T005 all write to observability.py — run sequentially (T003 → T004 → T005 if single developer; or T004 and T005 can be written in parallel since they add distinct functions)
- T006, T007, T008 all write to metrics.py — run sequentially
- T011, T014, T017 all write to daemon.py — coordinate carefully
- T012, T018 both write to __main__.py — run sequentially
- Dashboard JSON (T021) must be authored as valid Grafana JSON — use the Grafana dashboard schema; validate with `python3 -c "import json; json.load(open('docs/dashboards/coordinare.json'))"`
- Runbook files are referenced by exact path in dashboard JSON — filenames are case-sensitive
- T031 (full test suite) replaces the former T029; T033 (quickstart validation) replaces the former T031

### Plan.md Phase Cross-Reference

| Tasks.md Phase | Plan.md Phase | Description |
|---|---|---|
| Phase 1: Setup | — | Directory scaffolding |
| Phase 2: Foundation | Phase A (A1, A5) | observability.py |
| Phase 3: US1 | Phase A (A2, A4), Phase C (C1, C2, C3) | Metrics coverage |
| Phase 4: US2 | Phase A (A3), Phase C (C1) | Cycle correlation |
| Phase 5: US3 | Phase B (B1, B2), Phase C (C1, C3), Phase E (E1) | Health endpoints |
| Phase 6: US4 | Phase D (D1) | Dashboard |
| Phase 7: US5 | Phase D (D2, D3) | Runbooks |
| Phase 8: Polish | Phase C (C1 validation), Phase E | Final validation |
