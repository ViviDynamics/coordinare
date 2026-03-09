# Tasks: External Service Resilience

**Input**: Design documents from `/specs/005-resilience/`
**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/ ✅, quickstart.md ✅

**Organization**: Tasks are grouped by user story (US1–US5) to enable independent implementation and testing of each story. The CircuitBreaker infrastructure is foundational (Phase 2) — all user story phases depend on it.

**Dependency note**: spec 003 (state persistence) and spec 004 (agent protocol / AgentService, AgentServiceProtocol, TransportTimeoutError, TransportError) must be implemented before this spec.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2)
- Include exact file paths in descriptions

---

## Phase 1: Setup

**Purpose**: Add the new dependency and configure test isolation globally.

- [X] T001 Add `stamina>=24.2.0,<25` to pyproject.toml `[project.dependencies]` and run `uv lock` to update the lockfile
- [X] T002 [P] Add `stamina.set_active(False)` / `stamina.set_active(True)` autouse pytest fixture to `tests/conftest.py` to disable retry sleeps globally in all unit and integration tests

**Checkpoint**: `python -c "import stamina; print(stamina.__version__)"` succeeds; all existing tests still pass with stamina installed.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that ALL user stories depend on. Must be complete before any service modifications.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T003 Create `CircuitState(str, Enum)` with values `CLOSED="closed"`, `OPEN="open"`, `HALF_OPEN="half_open"` and `CircuitOpenError(RuntimeError)` with `service_name: str` attribute and message `"{service_name} circuit is open — call skipped"` in `src/coordinare/resilience.py`
- [X] T004 Implement `CircuitBreaker` dataclass in `src/coordinare/resilience.py`: fields `service_name`, `failure_threshold`, `recovery_window`, `observation_window`, `_state`, `_failure_times: deque[float]`, `_opened_at: float | None`, `_half_open_probe_in_flight: bool`; implement `allow_request() -> bool`, `record_success() -> None`, `record_failure() -> None`, `_transition(new_state, reason)` (emits `log.warning("circuit_breaker.state_changed")` + updates Prometheus gauge), and `@asynccontextmanager async def guard()` (raises `CircuitOpenError` if blocked, calls `record_success/failure` on exit)
- [X] T005 Implement `RetryConfig` frozen dataclass with fields `attempts`, `wait_initial`, `wait_max`, `wait_jitter`, `wait_exp_base=2.0` and `to_stamina_kwargs() -> dict[str, Any]` method returning kwargs for `stamina.retry()` in `src/coordinare/resilience.py`
- [X] T006 [P] Add `ServiceRetryConfig(BaseModel)`, `ServiceCircuitConfig(BaseModel)`, and `ResilienceConfig(BaseModel)` Pydantic models with all per-service defaults (github, slack, smtp, anthropic, agent — per research.md Topic 4) to `src/coordinare/config.py`; attach `resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)` to `ProjectConfiguration`; add `@model_validator(mode="after")` on `ResilienceConfig` asserting each service's `wait_max_seconds ≤ 30.0` (default `poll_interval_seconds`) to enforce FR-003 at config-load time; pass `poll_interval_seconds` into validator via `model_config` or a class-level reference
- [X] T007 [P] Add `CIRCUIT_BREAKER_STATE = Gauge("coordinare_circuit_breaker_state", ..., labelnames=("service", "state"))`, `SERVICE_RETRIES_TOTAL = Counter("coordinare_service_retries_total", ..., labelnames=("service", "action"))`, and `SERVICE_CALLS_TOTAL = Counter("coordinare_service_calls_total", ..., labelnames=("service", "action", "outcome"))` to `src/coordinare/metrics.py`

**Parallel notes**: T002, T006, T007 are parallel with each other and with T003–T005. T003 → T004 → T005 are sequential (same file).

**Checkpoint**: `CircuitBreaker` can be instantiated, `RetryConfig.to_stamina_kwargs()` returns valid kwargs, all three Prometheus metrics register without conflict, `ResilienceConfig` validates with defaults.

---

## Phase 3: User Story 1 — Board Polling Survives GitHub API Outages (Priority: P1) 🎯 MVP

**Goal**: GitHub service calls retry on transient failures with exponential backoff+jitter; respect HTTP 429 Retry-After; circuit breaker opens after `failure_threshold` exhausted retry budgets; daemon continues polling on `CircuitOpenError`.

**Independent Test**: Simulate GitHub returning HTTP 503 N times (where N = attempts × failure_threshold), then verify: (1) stamina retried `attempts` times per circuit failure, (2) circuit opened after `failure_threshold` exhausted budgets, (3) daemon did NOT crash, (4) poll loop continued.

- [X] T008 [US1] Add `GitHubError(RuntimeError)`, `TransientGitHubError(GitHubError)`, `PermanentGitHubError(GitHubError)`, and `RateLimitedGitHubError(TransientGitHubError)` with `retry_after: float` attribute to `src/coordinare/services/github.py`
- [X] T009 [US1] Implement `_retried_*` private methods in `src/coordinare/services/github.py` decorated with `@stamina.retry(on=TransientGitHubError, **RetryConfig(**config.resilience.github_retry.to_stamina_kwargs()).to_stamina_kwargs())`; classify 5xx, connection errors, DNS failures as `TransientGitHubError` and 4xx (except 429), malformed responses as `PermanentGitHubError`
- [X] T010 [US1] Add HTTP 429 Retry-After detection inside `_retried_*` bodies in `src/coordinare/services/github.py`: parse `Retry-After` header (integer seconds or HTTP-date string via `email.utils.parsedate_to_datetime()`); call `await asyncio.sleep(retry_after) if stamina.is_active()` before raising `TransientGitHubError`
- [X] T011 [US1] Wrap all public GitHub service methods with `async with self._circuit_breaker.guard():` in `src/coordinare/services/github.py`; inject `CircuitBreaker` instance via constructor; increment `SERVICE_CALLS_TOTAL` on success and failure outcomes
- [X] T012 [P] [US1] Extend `src/coordinare/daemon.py` exception handler to catch `CircuitOpenError` separately: call `log.warning("circuit_open.call_skipped", service=exc.service_name)`, increment `metrics.SERVICE_CALLS_TOTAL.labels(service=exc.service_name, action="call_blocked", outcome="circuit_open").inc()`, and do NOT set `self._running = False` — poll loop continues
- [X] T013 [P] [US1] Unit tests in `tests/unit/services/test_resilient_github.py`: (a) transient error triggers retry up to `attempts` times then raises, (b) permanent error propagates immediately without retry, (c) 429 with `Retry-After: 10` calls `asyncio.sleep(10.0)` when `stamina.is_active()`, (d) `CircuitOpenError` propagates from `guard()` when circuit is open, (e) successful call calls `record_success()`

**Checkpoint**: GitHub 503 → retries → circuit opens → `CircuitOpenError` logged → daemon continues polling on next cycle.

---

## Phase 4: User Story 2 — Notification Failures Do Not Block the Workflow (Priority: P1)

**Goal**: Slack and SMTP service calls retry on transient failures; `notify.py` catches all delivery failures with structured logging; card workflow transitions are never blocked by notification failures.

**Independent Test**: Configure Slack to a dead endpoint; trigger a card transition requiring notification; verify card transitions correctly, `notification.delivery_failed` log event appears, no `RuntimeExecutionError` raised.

- [X] T014 [US2] Add `SlackError(RuntimeError)`, `TransientSlackError(SlackError)` (5xx, timeout, network error), `PermanentSlackError(SlackError)` (4xx — bad webhook URL); implement `_retried_send` with `@stamina.retry(on=TransientSlackError, ...)` and wrap public `send_notification()` with `async with self._circuit_breaker.guard()` in `src/coordinare/services/slack.py`; increment `SERVICE_CALLS_TOTAL.labels(service="slack", action="send_notification", outcome="success"|"failure"|"circuit_open")` on each call outcome (FR-014)
- [X] T015 [P] [US2] Add `SMTPDeliveryError(RuntimeError)`, `TransientSMTPError(SMTPDeliveryError)` (`SMTPConnectError`, `SMTPServerDisconnected`, 4xx SMTP codes), `PermanentSMTPError(SMTPDeliveryError)` (`SMTPAuthenticationError`, `SMTPRecipientsRefused`); implement `_retried_send` with `@stamina.retry(on=TransientSMTPError, ...)` and wrap public method with `async with self._circuit_breaker.guard()` in `src/coordinare/services/email.py`; increment `SERVICE_CALLS_TOTAL.labels(service="smtp", action="send_notification", outcome="success"|"failure"|"circuit_open")` on each call outcome (FR-014)
- [X] T016 [US2] Replace `asyncio.gather(..., return_exceptions=True)` in `src/coordinare/graph/nodes/notify.py` with sequential `for (coro, channel) in [(email_coro, "email"), (slack_coro, "slack")]: try: await coro except Exception as exc: log.warning("notification.delivery_failed", channel=channel, error=str(exc)); metrics.SERVICE_CALLS_TOTAL.labels(...).inc()`
- [X] T017 [P] [US2] Unit tests for Slack resilience in `tests/unit/services/test_resilient_slack.py`: transient 5xx retried, permanent 4xx not retried, `CircuitOpenError` propagated on open circuit, `notify.py` continues after Slack failure
- [X] T018 [P] [US2] Unit tests for email resilience in `tests/unit/services/test_resilient_email.py`: `SMTPConnectError` retried, `SMTPAuthenticationError` not retried, `CircuitOpenError` propagated, card transition not blocked after exhausted retries

**Checkpoint**: With Slack webhook returning 500 and SMTP unreachable, `notify.py` logs two `notification.delivery_failed` events and returns normally; no exception propagates to the LangGraph node.

---

## Phase 5: User Story 3 — Agent Transport Failures Are Classified and Handled Per Severity (Priority: P1)

**Goal**: `ResilientAgentService` Decorator applies stamina retry + CircuitBreaker around `dispatch_card`, `check_status`, `relay_feedback`; `TransportTimeoutError` is retried, `TransportError` is permanent; semantic agent errors (`status: error`) are not retried; CB opens after `failure_threshold` exhausted budgets.

**Independent Test**: Using mock agent (spec 004), simulate `TransportTimeoutError` N times → verify retried → simulate permanent `TransportError` → verify not retried → verify card moves to Blocked.

- [X] T019 [US3] Implement `ResilientAgentService` class in `src/coordinare/resilience.py` implementing `AgentServiceProtocol` (spec 004): `__init__(inner, retry_config, circuit_breaker)`; `dispatch_card/check_status/relay_feedback` each decorated with `@stamina.retry(on=TransportTimeoutError, ...)` internally + wrapped with `async with self._circuit_breaker.guard()`; `check_health()` calls `self._inner.check_health()` directly (no retry, no CB guard)
- [X] T019a [US3] Extend `src/coordinare/graph/nodes/dispatch_card.py` and `src/coordinare/graph/nodes/monitor_agent.py` to catch `TransportError` (permanent agent failure) and `PermanentGitHubError` escaping the resilience layer; on catch, call `await github.move_card(state["active_card_id"], "BLOCKED")` and log `log.error("permanent_service_failure.card_blocked", ...)` before returning the updated state (FR-009, FR-016); `CircuitOpenError` from these nodes is handled separately in daemon.py (T012)
- [X] T020 [US3] Add `_build_circuit_breakers(config: ProjectConfiguration, metrics: CoordinareMetrics) -> dict[str, CircuitBreaker]` factory to `src/coordinare/__main__.py`; when constructing `ResilientAgentService`, convert `ServiceRetryConfig` to `RetryConfig` explicitly: `RetryConfig(attempts=cfg.attempts, wait_initial=cfg.wait_initial_seconds, wait_max=cfg.wait_max_seconds, wait_jitter=cfg.wait_jitter_seconds)` — **do NOT call `.to_stamina_kwargs()` on `ServiceRetryConfig`** (it has no such method); `RetryConfig.to_stamina_kwargs()` is the bridge; inject `resilient_agent` into graph node constructors
- [X] T021 [US3] Register stamina instrumentation retry hook at startup in `src/coordinare/__main__.py`: `stamina.instrumentation.set_on_retry_hooks([lambda info: metrics.SERVICE_RETRIES_TOTAL.labels(service=info.name, action="retry").inc()])` (note: `set_on_retry_hooks` is the current stamina API; earlier drafts referenced the deprecated `set_on_backoff_hook`)
- [X] T022 [P] [US3] Unit tests in `tests/unit/test_resilient_agent_service.py`: (a) `TransportTimeoutError` triggers stamina retry, (b) permanent `TransportError` propagates immediately without retry, (c) `CircuitOpenError` propagates from `guard()` when agent circuit is open, (d) `check_health()` called directly on inner without CB guard, (e) successful `dispatch_card` calls `record_success()`, (f) `dispatch_card` returning `ProtocolResponse(status="error", reason="agent failed")` is **not retried** and propagates as a permanent failure (FR-010 semantic error coverage)

**Checkpoint**: `ResilientAgentService.dispatch_card()` retries on timeout, raises immediately on permanent transport error, and raises `CircuitOpenError` when circuit open — all without modifying any LangGraph node.

---

## Phase 6: User Story 4 — Circuit Breaker Prevents Cascading Failure (Priority: P2)

**Goal**: Any service that fails consistently beyond its threshold has its circuit opened; subsequent calls skip immediately; circuit closes on successful probe after recovery window; health endpoint reflects open circuit; Anthropic SDK double-retry disabled.

**Independent Test**: Set a service to fail every call; observe circuit opens after `failure_threshold` exhausted budgets; wait `recovery_window`; observe half-open probe; restore service → circuit closes. Entire poll cycle duration ≤ 110% of healthy baseline.

- [X] T023 [US4] Add `AnthropicCallError(RuntimeError)`, `TransientAnthropicError(AnthropicCallError)` (`APIConnectionError`, `APITimeoutError`, 5xx `APIStatusError`), `PermanentAnthropicError(AnthropicCallError)` (`AuthenticationError`, 4xx); configure `AsyncAnthropic(max_retries=0)` to disable SDK internal retries; implement `_retried_*` with `@stamina.retry(on=TransientAnthropicError, ...)`; wrap public methods with `async with self._circuit_breaker.guard()` in `src/coordinare/services/claude.py`; increment `SERVICE_CALLS_TOTAL.labels(service="anthropic", action="assess_card", outcome="success"|"failure"|"circuit_open")` on each call outcome (FR-014)
- [X] T024 [P] [US4] Unit tests for `CircuitBreaker` FSM in `tests/unit/test_circuit_breaker.py`: (a) `failure_threshold` failures within `observation_window` → CLOSED→OPEN transition logged, (b) `recovery_window` elapsed → OPEN→HALF_OPEN transition, (c) probe success → HALF_OPEN→CLOSED, (d) probe failure → HALF_OPEN→OPEN, (e) second concurrent request in HALF_OPEN is blocked (`_half_open_probe_in_flight` flag), (f) failures outside `observation_window` do not count toward threshold
- [X] T025 [P] [US4] Unit tests for Anthropic resilience in `tests/unit/services/test_resilient_claude.py`: `APIConnectionError` retried, `AuthenticationError` not retried, SDK `max_retries=0` verified, `CircuitOpenError` propagated on open circuit
- [X] T026 [US4] Update `src/coordinare/health.py` to accept `circuit_breakers: dict[str, CircuitBreaker]` parameter; build `circuit_breakers` dict in response body with `{"state": cb.state.value, "opened_at": ..., "failure_count": len(cb._failure_times)}` per service (all three fields per contract schema); update `ServiceCircuitStatus` TypedDict to include `opened_at: str | None` and `failure_count: int` to match `contracts/health-response.schema.json`; derive `status="degraded"` when any of `github`, `anthropic`, or `agent` circuits are not `CLOSED`; remove old GitHub connectivity inference from poll recency
- [X] T026a [P] [US4] Unit tests for health.py status derivation in `tests/unit/test_health.py`: (a) all circuits closed → `status="ok"`, (b) only slack or smtp open → `status="ok"` (notification circuits non-blocking per FR-013), (c) github open → `status="degraded"`, (d) anthropic open → `status="degraded"`, (e) agent open → `status="degraded"`, (f) all circuits open → `status="degraded"` (not `"unhealthy"` — coordinare still running)
- [X] T027 [P] [US4] Integration test in `tests/integration/test_resilience_integration.py`: GitHub mock returns 503 on every call → after `failure_threshold` exhausted retry budgets circuit opens → subsequent poll cycles emit `service.call_skipped` log event → restore mock → circuit probe succeeds → circuit closes → normal operation confirmed

**Checkpoint**: With `failure_threshold=3` and GitHub returning 503, circuit opens after 3 exhausted retry budgets. Poll cycles after open emit `CircuitOpenError` immediately (no timeout wait). Health endpoint returns `"status": "degraded"`. After recovery, circuit closes and polling resumes.

---

## Phase 7: User Story 5 — Operators Can Observe Retry and Failure Behavior (Priority: P3)

**Goal**: Structured log events emitted for every retry, every circuit state change, and every final fallback action; Prometheus metrics reflect per-service retry counts and circuit states; health endpoint contract-validated.

**Independent Test**: Trigger retry scenario; assert `service.retry_attempt` structured log events present with `service`, `attempt`, `error`, `wait_seconds` fields; assert `coordinare_service_retries_total` counter increments; assert health response validates against JSON Schema v3.

- [X] T028 [P] [US5] Integration test in `tests/integration/test_resilience_integration.py`: configure dead Slack and SMTP endpoints; trigger card transition requiring notification; assert (a) card transitions successfully, (b) two `notification.delivery_failed` log events emitted, (c) `SERVICE_CALLS_TOTAL{service="slack",outcome="failure"}` and `{service="smtp",outcome="failure"}` both increment, (d) no `RuntimeExecutionError` raised
- [X] T029 [P] [US5] Contract test in `tests/contract/test_health_schema.py`: load `specs/005-resilience/contracts/health-response.schema.json`; call health endpoint with all circuits closed → validate response; open one core circuit → validate degraded response; both must validate against schema without errors
- [X] T030 [P] [US5] Performance test in `tests/perf/test_poll_cycle_budget.py`: time 10 consecutive poll cycles with all 5 circuits open and all mocked services responding instantly; compute mean cycle time; assert `mean_open_circuit_time ≤ healthy_baseline_time × 1.10` (SC-003)

**Checkpoint**: All structured log events contain required fields; `coordinare_circuit_breaker_state{state="open"} == 1` queryable in Prometheus; health response passes JSON Schema validation for both `ok` and `degraded` states.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Type safety, code quality, and operational validation.

- [X] T031 [P] Run `ruff check src/coordinare/resilience.py src/coordinare/services/github.py src/coordinare/services/slack.py src/coordinare/services/email.py src/coordinare/services/claude.py src/coordinare/graph/nodes/notify.py src/coordinare/daemon.py src/coordinare/health.py src/coordinare/__main__.py` and resolve all violations
- [X] T032 Run quickstart.md validation end-to-end: start coordinare with a mock GitHub service returning intermittent 503s; verify structured log events match `service.retry_attempt` and `circuit_breaker.state_changed` formats shown in `quickstart.md`; verify `curl /health` returns `circuit_breakers` dict; verify `curl /metrics` exposes `coordinare_circuit_breaker_state` and `coordinare_service_retries_total`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Setup — **BLOCKS all user stories**
- **US1 (Phase 3)**: Depends on Phase 2 complete
- **US2 (Phase 4)**: Depends on Phase 2 complete (parallel with US1)
- **US3 (Phase 5)**: Depends on Phase 2 complete (parallel with US1, US2); requires spec 004 AgentServiceProtocol
- **US4 (Phase 6)**: Depends on Phase 2–5 (Anthropic wiring in T023 needs Phase 2; tests need Phase 2–5 complete)
- **US5 (Phase 7)**: Depends on Phase 6 complete (health.py update needed for contract test)
- **Polish (Phase 8)**: Depends on all user stories complete

### User Story Dependencies

- **US1 (P1)**: Starts after Phase 2 — no dependency on other user stories
- **US2 (P1)**: Starts after Phase 2 — independent of US1; parallel possible
- **US3 (P1)**: Starts after Phase 2 — requires spec 004 artifacts; independent of US1/US2
- **US4 (P2)**: Starts after Phase 2; T023 (Anthropic) is independent of US1–3; CB unit tests (T024) can run as soon as Phase 2 done
- **US5 (P3)**: Starts after US4 (health.py T026 needed for contract test T029)

### Sequential chains within phases

- `T003 → T004 → T005` (all in `resilience.py`, same file)
- `T008 → T009 → T010 → T011` (all in `github.py`)
- `T014 → T016` (T016 notify.py fix depends on slack.py changes from T014 being injected)
- `T019` must complete before `T020` (ResilientAgentService must exist before wiring in `__main__.py`)
- `T020 → T021` (both in `__main__.py`, T021 adds hook after T020 wires startup)
- `T026` must complete before `T029` (health.py extension needed for contract test)

### Parallel Opportunities

**Phase 2**: `{T002, T006, T007}` all parallel with `{T003 → T004 → T005}`

**Phase 3**: `{T012 daemon.py, T013 test file}` parallel with `{T008 → T009 → T010 → T011}`

**Phase 4**: `{T015 email.py, T017 slack tests, T018 email tests}` parallel with `{T014 slack.py}`

**Phase 6**: `{T024 CB unit tests, T025 Anthropic tests, T027 integration test}` all parallel with `T023`

**Phase 7**: `{T028, T029, T030}` all parallel with each other

---

## Parallel Example: Phase 2 (Foundational)

```bash
# Stream 1 (sequential, resilience.py):
Task: "Create CircuitState enum and CircuitOpenError in src/coordinare/resilience.py"  # T003
Task: "Implement CircuitBreaker dataclass with FSM and guard() in src/coordinare/resilience.py"  # T004
Task: "Implement RetryConfig frozen dataclass in src/coordinare/resilience.py"  # T005

# Stream 2 (parallel with Stream 1):
Task: "Add ResilienceConfig to src/coordinare/config.py"  # T006

# Stream 3 (parallel with Streams 1-2):
Task: "Add Prometheus metrics to src/coordinare/metrics.py"  # T007

# Stream 4 (parallel with all):
Task: "Add stamina.set_active(False) fixture to tests/conftest.py"  # T002
```

---

## Implementation Strategy

### MVP First (US1 Only)

1. Complete Phase 1: Setup (T001–T002)
2. Complete Phase 2: Foundational (T003–T007) — CRITICAL, blocks all stories
3. Complete Phase 3: US1 — GitHub resilience + daemon degraded-mode
4. **STOP and VALIDATE**: GitHub 503 no longer crashes coordinare
5. Proceed to US2 when ready

### Incremental Delivery

1. Setup + Foundational → resilience infrastructure ready
2. US1 → GitHub calls resilient → **coordinare survives GitHub outages** (MVP)
3. US2 → Notifications non-blocking → **card workflows unblocked by notification failures**
4. US3 → ResilientAgentService → **agent failures handled per severity**
5. US4 → Full CB coverage (Anthropic) + health endpoint → **circuit breaker operational everywhere**
6. US5 → Full observability → **production-ready with metrics + contract tests**

### Parallel Team Strategy

With two developers after Phase 2 complete:
- **Developer A**: US1 (GitHub + daemon)
- **Developer B**: US2 (Slack + SMTP + notify.py)
- Both can start US3, US4 once their P1 story is done

---

## Notes

- `[P]` tasks = different files, no dependency on incomplete work in this phase
- `[USN]` label maps task to spec.md user story for traceability
- `stamina.set_active(False)` in `tests/conftest.py` (T002) disables all retry sleeps — CB tests should call `cb.record_failure()` directly to trigger state transitions
- Commit after each phase checkpoint
- Verify phase checkpoint passes before starting next phase
- Agent exceptions (`TransportError`, `TransportTimeoutError`) defined in spec 004 transport layer; import from there into resilience.py
- `CircuitOpenError` is deliberately excluded from all `on=` tuples passed to `stamina.retry()` — it must propagate immediately to the daemon's degraded-mode handler
