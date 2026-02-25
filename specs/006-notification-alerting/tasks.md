# Tasks: Notification & Alerting System

**Input**: Design documents from `/specs/006-notification-alerting/`
**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/notification-service.md ✅, quickstart.md ✅

**Tests**: Included (integration, unit, and contract tests per constitution Principle II).

**Organization**: Tasks grouped by user story to enable independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US4)
- Paths are relative to repository root (`src/`, `tests/`)

---

## Phase 1: Setup

**Purpose**: Create new file stubs so all subsequent tasks have files to write to.

- [ ] T001 Create `src/coordinare/services/notification.py` with module docstring and top-level import scaffold (asyncio, collections, datetime, uuid, structlog, dataclasses)
- [ ] T002 [P] Create 5 test file stubs with placeholder `pass` bodies: `tests/unit/services/test_notification.py`, `tests/unit/models/test_notification_models.py`, `tests/unit/test_daemon_alerts.py`, `tests/integration/test_notification_dispatch.py`, `tests/contract/test_notification_service.py`; also create `tests/utils/fake_notification.py` implementing `FakeNotificationService` (records dispatched events in `self.dispatched: list[NotificationEvent]`, exposes a `history` property returning `NotificationHistory`; implements `NotificationServiceProtocol`; used as the standard test double across all test suites in place of `AsyncMock` where full protocol compliance is needed)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core types, config models, state protocol, and channel senders that ALL user stories depend on.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [ ] T003 Add `EventType`, `NotificationSeverity`, `NotificationStatus`, `ChannelType` enums and `NotificationEvent` dataclass (fields: event_type, severity, payload, source, dedup_key) to `src/coordinare/models/notification.py`; keep existing `Notification` class temporarily (it is retired in T014)
- [ ] T004 Add `ChannelConfig`, `RoutingEntry`, and `NotificationsConfig` Pydantic models to `src/coordinare/config.py` per `data-model.md`; add `notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)` to `ProjectConfiguration`; remove the following flat fields and their validators: `notification_email`, `smtp_host`, `smtp_port`, `smtp_username`, `smtp_password`, `slack_webhook_url`, `slack_channel`; update `config.example.yaml` to replace all removed flat fields with the full `notifications:` block documented in `specs/006-notification-alerting/quickstart.md`
- [ ] T005 Update `NotificationServiceProtocol` to `async def dispatch(self, event: NotificationEvent) -> None` and replace `email_service: NotificationServiceProtocol` + `slack_service: NotificationServiceProtocol` with `notification_service: NotificationServiceProtocol` in `src/coordinare/graph/state.py`; update `initial_state()` to remove the old keys (depends on T003)
- [ ] T006 [P] Add 4 new Prometheus counters to `CoordinareMetrics.__init__()` in `src/coordinare/metrics.py`: `notifications_dispatched_total(event_type, channel)`, `notifications_failed_total(channel)`, `notifications_rate_limited_total(channel)`, `notifications_deduplicated_total(channel)`; remove the existing `notifications_total(channel, status)` counter and its `observe_notification()` helper
- [ ] T007 [P] Remove `send_notification(notification: Notification)` from `src/coordinare/services/slack.py`; `SlackService` becomes a passive configuration holder (retains its constructor and `_webhook_url` attribute); webhook HTTP delivery is handled directly by `SlackChannelSender` (T009) — no new `send()` method is added to `SlackService` itself
- [ ] T008 [P] Remove `send_notification(recipient, notification)` from `src/coordinare/services/email.py`; `EmailService` becomes a passive configuration holder (retains its constructor and SMTP settings attributes); SMTP delivery is handled directly by `EmailChannelSender` (T009) — no new `send()` method is added to `EmailService` itself
- [ ] T009 Implement `ChannelSenderProtocol`, `SlackChannelSender`, and `EmailChannelSender` in `src/coordinare/services/notification.py` per `contracts/notification-service.md`; `SlackChannelSender.send()` calls `httpx.AsyncClient.post()` directly with the webhook URL passed to its constructor; `EmailChannelSender.send()` calls `aiosmtplib.send()` directly with SMTP settings passed to its constructor; neither adapter inherits from or calls through the legacy `SlackService`/`EmailService` classes (depends on T004)
- [ ] T010 Implement `build_notification_service(config: NotificationsConfig, metrics: CoordinareMetrics) -> NotificationService` factory in `src/coordinare/services/notification.py`; instantiates `SlackChannelSender` or `EmailChannelSender` per channel type and returns a fully wired `NotificationService` (depends on T004, T009)

**Checkpoint**: Foundation ready — user story implementation can now begin.

---

## Phase 3: User Story 1 — Configurable Notification Routing (Priority: P1) 🎯 MVP

**Goal**: All coordinare notifications (card transitions, advocate escalations) route through a single configurable layer. Adding a new event type requires only a `config.yaml` change.

**Independent Test**: Configure routing with `card_transition → [slack-ops]` only. Trigger a card transition. Verify Slack receives the notification and email does not. Add a new routing entry in config; verify the new event type is delivered without code changes.

- [ ] T011 [US1] Implement `NotificationService.__init__()`, `_build_routing()`, `dispatch()`, and `_dispatch_to_channel()` with bounded retry loop (`asyncio.sleep(retry_delay_seconds)` between attempts) and concurrent channel dispatch via `asyncio.gather` in `src/coordinare/services/notification.py`; no rate limiting or dedup yet — those are added in T024 (depends on T009, T010)
- [ ] T012 [US1] Update `_bootstrap_services()` in `src/coordinare/__main__.py`: replace `EmailService`/`SlackService` wiring with `notification_service: build_notification_service(config.notifications, METRICS)`; remove `email_service`, `slack_service`, `notification_email`, `blocked_reminder_hours` keys from returned state dict (depends on T004, T010)
- [ ] T013 [US1] Migrate `src/coordinare/graph/nodes/notify.py`: replace direct `email_service`/`slack_service` calls with `notification_service.dispatch(NotificationEvent(...))`; add `_event_type_for_phase(phase: str) -> EventType` helper mapping current graph phase to correct `EventType`; build `payload` dict from card fields (depends on T011)
- [ ] T014 [US1] Remove `Notification` class and `as_text()` method from `src/coordinare/models/notification.py`; search and fix any remaining imports of `Notification` across the codebase (depends on T013)
- [ ] T015 [P] [US1] Unit tests in `tests/unit/services/test_notification.py`: routing to matched channel calls sender; unmatched event_type records `unrouted`; retry loop calls sender up to `retry_count` times on failure; exception is swallowed after all retries; concurrent dispatch calls all matched channels; **FR-014**: dispatch `advocate_escalation` through a routing table that has no `advocate`-related enablement key — verify delivery proceeds normally (routing table is evaluated stateless with respect to any subsystem's enabled/disabled state)
- [ ] T016 [P] [US1] Contract test in `tests/contract/test_notification_service.py`: verify `NotificationService` satisfies `NotificationServiceProtocol` (has `dispatch()` coroutine and `history` property); verify `SlackChannelSender` and `EmailChannelSender` satisfy `ChannelSenderProtocol`

**Checkpoint**: User Story 1 is complete — card transitions and advocate escalations route through `NotificationService`. New event types need only a config change.

---

## Phase 4: User Story 2 — Operator System Alerts (Priority: P2)

**Goal**: Operators receive automatic alerts for `daemon_restart`, `prolonged_idle`, and (via spec 005) `circuit_breaker_trip` events without any changes to existing LangGraph node files.

**Independent Test**: Start coordinare with `daemon_restart → [slack-ops]` in routing. Verify a Slack message arrives at startup. Set `prolonged_idle_threshold_seconds: 60`, run with no actionable board work for 70 seconds, verify exactly one `prolonged_idle` message is sent (dedup prevents repeats).

- [ ] T017 [US2] Add `idle_threshold_seconds: int = 1800` constructor parameter to `CoordinareDaemon.__init__()` in `src/coordinare/daemon.py`; store as `self._idle_threshold_seconds`
- [ ] T018 [US2] Add `daemon_restart` dispatch hook to `CoordinareDaemon.start()` in `src/coordinare/daemon.py`: after the startup emit and before the first poll cycle, call `notification_service.dispatch(NotificationEvent(event_type=EventType.daemon_restart, severity=NotificationSeverity.info, source="daemon", payload={"run_mode": self._run_mode, "summary": "Coordinare daemon started"}))` if `notification_service` is present in state (depends on T017)
- [ ] T019 [US2] Add `prolonged_idle` tracking to `CoordinareDaemon.start()` loop in `src/coordinare/daemon.py`: track `last_activity_at = monotonic()`, reset when phase is not `"idle"`, dispatch `NotificationEvent(event_type=EventType.prolonged_idle, dedup_key="prolonged_idle", payload={"idle_seconds": ..., "summary": ...})` when `monotonic() - last_activity_at >= self._idle_threshold_seconds` (depends on T018)
- [ ] T020 [P] [US2] Update `_run()` in `src/coordinare/__main__.py` to pass `idle_threshold_seconds=config.notifications.prolonged_idle_threshold_seconds` to `CoordinareDaemon()` constructor (depends on T017)
- [ ] T021 [P] [US2] Unit tests in `tests/unit/test_daemon_alerts.py` (stubbed in T002): mock `notification_service` in state; verify `dispatch()` is called with `event_type=daemon_restart` on `start()`; verify `prolonged_idle` dispatch fires after threshold exceeded; verify dedup_key `"prolonged_idle"` is set; verify `last_activity_at` resets when phase changes from idle

**Checkpoint**: User Stories 1 and 2 complete — operator receives runtime health alerts without node code changes.

---

## Phase 5: User Story 3 — Rate Limiting and Deduplication (Priority: P3)

**Goal**: A burst of identical events cannot flood a Slack channel or inbox. Duplicate escalations within a window produce exactly one notification.

**Independent Test**: Configure `rate_limit: 2, rate_window_seconds: 60` on Slack. Fire 5 events within 30s. Verify 2 delivered and 3 `rate_limited` in history. Fire the same `advocate_escalation` event for issue #99 twice within the dedup window. Verify 1 delivered and 1 `deduplicated`.

- [ ] T022 [US3] Implement `SlidingWindowRateLimiter` class (deque of UTC timestamps, evict timestamps older than window, check `len(deque) >= max_messages`) in `src/coordinare/services/notification.py` per `data-model.md`; `max_messages=0` means unlimited
- [ ] T023 [US3] Implement `DeduplicationWindow` class (dict[str, datetime], `is_duplicate()` checks window, `record()` stores timestamp, `_evict()` removes entries older than 2× window) in `src/coordinare/services/notification.py` per `data-model.md`
- [ ] T024 [US3] Wire `SlidingWindowRateLimiter` and `DeduplicationWindow` into `NotificationService.__init__()` (one per channel) and add rate-limit and dedup checks to `_dispatch_to_channel()` in `src/coordinare/services/notification.py`; update metrics calls: `notifications_rate_limited_total` and `notifications_deduplicated_total` (depends on T022, T023)
- [ ] T025 [P] [US3] Unit tests in `tests/unit/services/test_notification.py`: `SlidingWindowRateLimiter` allows below limit, drops at limit, resets after window expires; `DeduplicationWindow` returns False first call, True within window, False after window expires; `NotificationService._dispatch_to_channel()` records `rate_limited` / `deduplicated` status (verifiable via mock history in T028)

**Checkpoint**: User Stories 1, 2, and 3 complete — rate limiting and dedup prevent notification storms.

---

## Phase 6: User Story 4 — Notification History (Priority: P4)

**Goal**: Every dispatch attempt is recorded in a queryable in-memory log. Records older than 24 hours are evicted automatically.

**Independent Test**: Dispatch events that produce one `delivered`, one `rate_limited`, and one `deduplicated` attempt. Call `notification_service.history.query(status=NotificationStatus.delivered)`. Verify exactly one record returned with `channel_name`, `event_type`, `timestamp`, and `elapsed_ms` populated.

- [ ] T026 [US4] Add `NotificationAttempt` dataclass (fields: attempt_id, event_type, channel_name, status, timestamp, elapsed_ms, dedup_key, retries_attempted, error_message) to `src/coordinare/models/notification.py`
- [ ] T027 [US4] Implement `NotificationHistory` class in `src/coordinare/models/notification.py`: `list[NotificationAttempt]` backing store, `append()` with lazy front-eviction, `query()` with filters for event_type/channel_name/status/since, `_evict()` removes records where `timestamp < now - max_age_seconds` (depends on T026)
- [ ] T028 [US4] Wire `NotificationHistory` into `NotificationService` in `src/coordinare/services/notification.py`: instantiate `NotificationHistory(config.history_max_age_hours)` in `__init__()`, expose `history` property, implement `_record()` helper, call `_record()` for all dispatch outcomes (delivered, failed, rate_limited, deduplicated, unrouted) with correct `retries_attempted` and `elapsed_ms` (depends on T027)
- [ ] T029 [P] [US4] Unit tests in `tests/unit/models/test_notification_models.py`: `NotificationHistory.query()` filters correctly by each field; time-based eviction removes records older than `max_age_hours`; records appended after eviction are retained; `attempt_id` is unique per record

**Checkpoint**: All four user stories complete — full notification pipeline with routing, alerts, rate limiting, dedup, retry, and history.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Integration coverage, config validation tests, type safety, and E2E validation.

- [ ] T030 [P] Integration test in `tests/integration/test_notification_dispatch.py`: wire a complete `NotificationService` (real routing, rate limiter, dedup, history, metrics) using a `FakeChannelSender` that raises on first N calls then succeeds — this test validates the **entire pipeline end-to-end in a single dispatch call**, not individual components in isolation (those are covered by T015/T025/T029); assert: routing sends to correct channels, retry produces correct `retries_attempted` count in history, rate limiter drops excess events, dedup suppresses second event within window, history records all 5 outcome statuses (`delivered`/`failed`/`rate_limited`/`deduplicated`/`unrouted`) with correct metadata, and `notifications_dispatched_total` counter increments
- [ ] T031 [P] Unit tests in `tests/unit/models/test_notification_models.py`: `ChannelConfig` validator rejects slack channel without `webhook_url`; `ChannelConfig` validator rejects email channel without `smtp_host`/`smtp_recipient`; `NotificationsConfig` validator rejects duplicate channel names; `NotificationsConfig` validator rejects routing entry referencing unknown channel name
- [ ] T032 [P] Run `ruff check src/ tests/` and `mypy --strict src/coordinare/`; fix all type annotation issues in `notification.py`, `config.py`, `state.py`, `metrics.py`, `daemon.py`, `notify.py`, `slack.py`, `email.py`; run `pytest --cov=src/coordinare --cov-report=term-missing` and verify coverage does not regress from the pre-feature baseline (constitution Quality Gate #5)
- [ ] T033 Run quickstart.md E2E validation: execute all 7 manual test scenarios from `specs/006-notification-alerting/quickstart.md` and confirm each passes; update quickstart.md if any scenario steps need adjustment

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 — BLOCKS all user stories
- **US1 (Phase 3)**: Depends on Foundational — no US dependencies
- **US2 (Phase 4)**: Depends on Foundational — no US dependencies; US1 recommended first (same NotificationService)
- **US3 (Phase 5)**: Depends on Foundational — US1 recommended first (extends same file)
- **US4 (Phase 6)**: Depends on Foundational — US1 recommended first (wires into same service)
- **Polish (Phase 7)**: Depends on all desired user stories complete

### User Story Dependencies

- **US1 (P1)**: Can start after Foundational — no dependencies on US2/US3/US4
- **US2 (P2)**: Can start after Foundational — no node code changes; touches only `daemon.py`
- **US3 (P3)**: Can start after Foundational — extends `NotificationService._dispatch_to_channel()` (best after US1)
- **US4 (P4)**: Can start after Foundational — adds `_record()` to `NotificationService` (best after US1 and US3)

### Within Each User Story

- Models/enums before service implementation
- Service implementation before node migration
- Node migration before retiring old model classes
- Implementation before tests (tests validate against implementation)

### Parallel Opportunities

**Phase 2 (Foundational)**:
- T003, T004, T006, T007, T008 can run in parallel (different files: models, config, metrics, slack, email)
- T005 depends on T003; T009 depends on T004 only (adapters call transport directly — no longer depends on T007/T008); T010 depends on T004+T009

**Phase 3 (US1)**:
- T011 → T012 → T013 → T014 are sequential (same service/node chain)
- T015 and T016 can run in parallel with each other (different test files)

**Phase 4 (US2)**:
- T017 → T018 → T019 are sequential (same daemon.py method)
- T020 [P] can run parallel to T018/T019 (different file: `__main__.py`)
- T021 [P] can run parallel to implementation tasks (different test file)

**Phase 5 (US3)**:
- T022 and T023 are sequential (same file; add different classes)
- T025 [P] can run parallel to T022/T023 (different: tests)

**Phase 6 (US4)**:
- T026 → T027 are sequential (same models file)
- T028 depends on T027
- T029 [P] can run parallel to T028 (tests/unit/models vs services)

**Phase 7 (Polish)**:
- T030, T031, T032 can all run in parallel (different files)
- T033 is sequential (needs everything complete)

---

## Parallel Examples

```bash
# Phase 2 — launch foundation tasks in parallel:
Task: "Add enums + NotificationEvent to src/coordinare/models/notification.py"        # T003
Task: "Add ChannelConfig/NotificationsConfig to src/coordinare/config.py"              # T004
Task: "Add 4 Prometheus counters to src/coordinare/metrics.py"                         # T006
Task: "Retire send_notification() from SlackService in src/coordinare/services/slack.py" # T007
Task: "Retire send_notification() from EmailService in src/coordinare/services/email.py" # T008

# Phase 3 (US1) — launch tests in parallel after implementation:
Task: "Unit tests for NotificationService in tests/unit/services/test_notification.py" # T015
Task: "Contract tests in tests/contract/test_notification_service.py"                  # T016

# Phase 7 — launch polish tasks in parallel:
Task: "Integration test in tests/integration/test_notification_dispatch.py"            # T030
Task: "Config validator tests in tests/unit/models/test_notification_models.py"        # T031
Task: "ruff + mypy checks across modified files"                                        # T032
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: Trigger a card transition, confirm Slack receives message, confirm no exception raised on channel failure
5. Deploy/demo if ready

### Incremental Delivery

1. Setup + Foundational → Foundation ready
2. US1 → Routing + migration live → Deploy/Demo (MVP: all existing notifications work via new layer)
3. US2 → System alerts live → Deploy/Demo (operators notified of daemon events)
4. US3 → Rate limiting + dedup live → Deploy/Demo (notification storms prevented)
5. US4 → History live → Deploy/Demo (full audit trail available)

### Parallel Team Strategy

With multiple developers, after Foundational is complete:

- Developer A: US1 (notify.py migration + core NotificationService)
- Developer B: US2 (daemon.py alert hooks — fully independent)
- Developer C: US3 (rate limiter + dedup — can extend US1's NotificationService)

---

## Notes

- [P] tasks = different files, no in-phase dependencies
- [US*] label maps each task to its user story for traceability
- T014 (retire `Notification` model) must follow T013 (migration complete) — do not retire before all callers are migrated
- T004 removes flat config fields — `config.example.yaml` must be updated alongside this task to document the new structure
- T020 (daemon idle_threshold_seconds in `__main__.py`) can be done in parallel with T018/T019 since it touches a different file
- The `circuit_breaker_trip` event type is in `EventType` enum (T003) but its dispatch call is implemented by spec 005 — no task required here beyond the enum entry
- Commit after each completed story (T016, T021, T025, T029) at minimum; use Conventional Commits format (e.g., `feat: 006 notification routing — US1 complete`, `feat: 006 system alerts — US2 complete`)
