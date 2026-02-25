# Implementation Plan: External Service Resilience

**Branch**: `005-resilience` | **Date**: 2026-02-22 | **Spec**: `specs/005-resilience/spec.md`
**Input**: Feature specification from `/specs/005-resilience/spec.md`
**Prerequisites**: spec 003 (state persistence) and spec 004 (agent protocol / AgentService) must be implemented before this spec.

---

## Summary

Wrap every external service call (GitHub GraphQL, AgentService, Slack, SMTP, Anthropic) in `stamina.retry()` with exponential backoff + jitter and a per-service custom `CircuitBreaker`. The circuit breaker attaches at the `AgentService` method boundary via a `ResilientAgentService` Decorator and inline inside the other service classes. The daemon treats `CircuitOpenError` as a degraded-mode event (log + continue) rather than a fatal crash. All retry events and circuit state changes emit structured log events and update Prometheus metrics. No graph node changes are required.

---

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `stamina>=24.2.0` (NEW — add to pyproject.toml), `tenacity` (already installed via langgraph), `prometheus-client>=0.21`, `structlog>=24.1`, `pydantic>=2.9`, `pydantic-settings>=2.6`
**Storage**: N/A — circuit breaker state is in-memory, resets on restart (per spec assumption)
**Testing**: pytest, pytest-asyncio (already present); `stamina.set_active(False)` for test isolation
**Target Platform**: Linux (single-process asyncio coordinare daemon)
**Performance Goals**: Open-circuit poll cycle ≤ 110% of healthy baseline (SC-003)
**Constraints**: `wait_max` ≤ `poll_interval_seconds` (30s default) per FR-003; circuit breaker state must not persist across restarts
**Scale/Scope**: 5 services, ~10 call sites in graph nodes, single process

---

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### I. Code Quality First ✅

- `stamina` replaces no-op error handling with a clean decorator API — readability improved.
- `CircuitBreaker` is a single-responsibility class (~80 lines); each method has one clear purpose.
- Dead code: `notify.py`'s silent `return_exceptions=True` will be replaced with explicit logging.
- All new code is type-annotated (mypy strict mode).
- New dependency `stamina` is justified (retry is genuinely complex; not stdlib-solvable).

### II. Testing Discipline ✅

- Unit tests for `CircuitBreaker` FSM (all 4 state transitions, half-open probe guard, concurrent probe blocking).
- Unit tests for `ResilientAgentService` (retry on `TransportTimeoutError`, permanent `TransportError` not retried, `CircuitOpenError` propagation).
- Integration tests for each service (GitHub, Slack, SMTP, Anthropic, AgentService) with simulated failures.
- Contract test: health response matches `contracts/health-response.schema.json`.
- Performance test: SC-003 budget (open circuit poll cycle ≤ 110% baseline).
- `stamina.set_active(False)` in conftest.py prevents retry sleeps in all tests.

### III. User Experience Consistency ✅

- All degraded-mode events use `log.warning()` consistently with existing structlog patterns.
- `CircuitOpenError` message format: `"{service_name} circuit is open — call skipped"`.
- Health endpoint `/health` extended with `circuit_breakers` object following existing response shape.

### IV. Performance by Design ✅

- **Performance budget defined**: SC-003 — open circuit poll cycle ≤ 10% increase vs. healthy baseline.
- **Measurement plan**: `tests/perf/test_poll_cycle_budget.py` times 10 consecutive poll cycles with all circuits open (mocked services return immediately) vs. healthy baseline.
- **Regression prevention**: performance test in CI; fails if poll cycle exceeds budget.
- **Caching strategy**: N/A — circuit state is pure in-memory FSM.

### V. Clarity Before Action ✅

- All three clarification questions resolved in spec.
- No `NEEDS CLARIFICATION` tags remain.
- All decisions documented in `research.md`.

---

## Project Structure

### Documentation (this feature)

```text
specs/005-resilience/
├── plan.md                          # This file
├── research.md                      # Phase 0 output ✅
├── data-model.md                    # Phase 1 output ✅
├── quickstart.md                    # Phase 1 output ✅
├── contracts/
│   └── health-response.schema.json  # Phase 1 output ✅
└── tasks.md                         # Phase 2 output (speckit.tasks)
```

### Source Code

```text
src/coordinare/
├── resilience.py                    # NEW: CircuitBreaker, CircuitOpenError,
│                                    #      RetryConfig, ResilientAgentService
├── config.py                        # MODIFY: ServiceRetryConfig, ServiceCircuitConfig,
│                                    #         ResilienceConfig nested in ProjectConfiguration
├── metrics.py                       # MODIFY: CIRCUIT_BREAKER_STATE Gauge,
│                                    #         SERVICE_RETRIES_TOTAL, SERVICE_CALLS_TOTAL counters
├── health.py                        # MODIFY: circuit_breakers dict in health response
├── __main__.py                      # MODIFY: construct circuit breakers, register stamina hook
├── daemon.py                        # MODIFY: CircuitOpenError → degraded-mode log, not crash
├── services/
│   ├── github.py                    # MODIFY: TransientGitHubError taxonomy, stamina retry, CB guard
│   ├── slack.py                     # MODIFY: TransientSlackError taxonomy, stamina retry, CB guard
│   ├── email.py                     # MODIFY: TransientSMTPError taxonomy, stamina retry, CB guard
│   └── claude.py                    # MODIFY: TransientAnthropicError taxonomy, stamina retry,
│                                    #         CB guard, AsyncAnthropic(max_retries=0)
│   # agent_ssh.py: SUPERSEDED by spec 004 AgentService; ResilientAgentService wraps AgentService
└── graph/
    └── nodes/
        └── notify.py                # MODIFY: replace silent return_exceptions=True with
                                     #         per-service error logging

pyproject.toml                       # MODIFY: add stamina>=24.2.0
```

```text
tests/
├── unit/
│   ├── test_circuit_breaker.py      # NEW: CircuitBreaker FSM unit tests
│   ├── test_resilient_agent_service.py  # NEW: ResilientAgentService unit tests
│   └── services/
│       ├── test_resilient_github.py # NEW: GitHub retry + CB unit tests
│       ├── test_resilient_slack.py  # NEW: Slack retry + CB unit tests
│       ├── test_resilient_email.py  # NEW: Email retry + CB unit tests
│       └── test_resilient_claude.py # NEW: Anthropic retry + CB unit tests
├── integration/
│   └── test_resilience_integration.py  # NEW: end-to-end failure scenarios
├── contract/
│   └── test_health_schema.py        # NEW: health response matches JSON Schema
└── perf/
    └── test_poll_cycle_budget.py    # NEW: SC-003 poll cycle budget
```

---

## Implementation Phases

### Phase A: Core Resilience Infrastructure (`src/coordinare/resilience.py`)

**New file** with everything the rest of the codebase imports.

1. `CircuitState` — `str` Enum with `CLOSED`, `OPEN`, `HALF_OPEN`
2. `CircuitOpenError(RuntimeError)` — carries `service_name`
3. `CircuitBreaker` — dataclass with asyncio-safe FSM:
   - `allow_request() -> bool` (synchronous, no await)
   - `record_success() -> None`
   - `record_failure() -> None` (counts one per exhausted retry budget)
   - `@asynccontextmanager async def guard()` — raises `CircuitOpenError` if blocked; calls `record_success/failure()` based on outcome
   - `_transition(new_state, reason)` — emits `log.warning("circuit_breaker.state_changed", ...)` + updates Prometheus Gauge
4. `RetryConfig` — frozen dataclass wrapping stamina kwargs; `to_stamina_kwargs()` method
5. `ResilientAgentService` — Decorator implementing `AgentServiceProtocol` (spec 004):
   - `__init__(inner, retry_config, circuit_breaker)`
   - `dispatch_card(...)` → `async with cb.guard(): return await stamina_retry_dispatch(...)`
   - `check_status(session_id)` → same pattern
   - `relay_feedback(session_id, payload)` → same pattern
   - `check_health()` → direct call, no retry, no CB guard

---

### Phase B: Config Extensions (`src/coordinare/config.py`)

Add nested `ResilienceConfig` with all 10 `ServiceRetryConfig` / `ServiceCircuitConfig` instances (5 services × 2 config classes each). Attach as `resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)` in `ProjectConfiguration`.

All per-service defaults defined per research.md Topic 4.

---

### Phase C: Prometheus Metrics (`src/coordinare/metrics.py`)

Add three new metrics to `CoordinareMetrics`:
- `coordinare_circuit_breaker_state` — multi-label Gauge `(service, state)` registered to `self.registry`
- `coordinare_service_retries_total` — Counter `(service, action)`
- `coordinare_service_calls_total` — Counter `(service, action, outcome)`

`CoordinareMetrics` must expose its registry instance so `CircuitBreaker` can register the gauge at construction time.

---

### Phase D: Service Exception Taxonomy + Retry Wrappers

For each service file, add:

1. **Exception taxonomy**: `TransientXError`, `PermanentXError` (and `RateLimitedGitHubError` with `retry_after` field for GitHub).
2. **Internal retried method** (prefixed `_retried_`): decorated with `@stamina.retry(on=TransientXError, **retry_config.to_stamina_kwargs())`.
3. **Public method**: wraps `_retried_` with `async with self._circuit_breaker.guard():`.
4. **Retry-After handling** (GitHub only): in the `_retried_` body, detect 429 → `await asyncio.sleep(retry_after) if stamina.is_active()` → raise `TransientGitHubError`.

**Claude-specific change**: Pass `max_retries=0` to `AsyncAnthropic()` to disable SDK internal retries.

**Affected files**: `github.py`, `slack.py`, `email.py`, `claude.py`.

---

### Phase E: `ResilientAgentService` Bootstrap (`src/coordinare/__main__.py`)

After the existing transport factory (`_build_transport`, spec 004):

```python
agent_service = AgentService(transport)
circuit_breakers = _build_circuit_breakers(config, metrics)
resilient_agent = ResilientAgentService(
    inner=agent_service,
    retry_config=RetryConfig(**config.resilience.agent_retry.to_stamina_kwargs()),
    circuit_breaker=circuit_breakers["agent"],
)
```

Register stamina retry counter hook at startup:
```python
stamina.instrumentation.set_on_backoff_hook(
    lambda attempt_info: metrics.SERVICE_RETRIES_TOTAL.labels(
        service=attempt_info.name, action="retry"
    ).inc()
)
```

---

### Phase F: Daemon Update (`src/coordinare/daemon.py`)

Extend exception handling to differentiate `CircuitOpenError`:

```python
except CircuitOpenError as exc:
    log.warning("circuit_open.call_skipped", service=exc.service_name)
    # Do NOT set self._running = False — continue the poll loop
    metrics.observe_error("circuit_open")
except Exception as exc:
    # existing fatal crash logic
    ...
```

---

### Phase G: Health Endpoint Update (`src/coordinare/health.py`)

1. Accept `circuit_breakers: dict[str, CircuitBreaker]` in the health function signature (injected at startup).
2. Build `circuit_breakers` dict in health response: `{name: {"state": cb.state.value, "opened_at": ..., "failure_count": len(cb._failure_times)}}`.
3. Derive `status`: `"degraded"` if any of `github`, `anthropic`, `agent` circuits are not `CLOSED`.
4. Remove the old GitHub connectivity inference from poll recency (replaced by circuit state).

---

### Phase H: Notify Node Fix (`src/coordinare/graph/nodes/notify.py`)

Replace silent `asyncio.gather(..., return_exceptions=True)`:

```python
# Before (silent failure)
results = await asyncio.gather(
    email_service.send_notification(...),
    slack_service.send_notification(...),
    return_exceptions=True,
)

# After (logged failures, no crash)
for coro, channel in [
    (email_service.send_notification(...), "email"),
    (slack_service.send_notification(...), "slack"),
]:
    try:
        await coro
    except Exception as exc:
        log.warning("notification.delivery_failed", channel=channel, error=str(exc))
        metrics.SERVICE_CALLS_TOTAL.labels(
            service=channel, action="send_notification", outcome="failure"
        ).inc()
```

---

### Phase I: Tests

**Unit (fast, no sleeps — stamina.set_active(False) in conftest.py)**:

- `test_circuit_breaker.py`: CLOSED→OPEN on threshold, OPEN→HALF_OPEN on recovery, HALF_OPEN→CLOSED on probe success, HALF_OPEN→OPEN on probe failure, concurrent probe blocked.
- `test_resilient_agent_service.py`: dispatch retried on `TransportTimeoutError`, not retried on `TransportError`, `CircuitOpenError` propagated, `check_health()` called directly.
- Per-service tests: transient errors retried, permanent errors propagate immediately, `CircuitOpenError` propagates on open circuit.

**Integration**:

- `test_resilience_integration.py`: GitHub 503 × N calls → circuit opens → subsequent calls skip → mock recovers → circuit closes → normal operation.
- Notification failure does not block card transition.
- Daemon continues polling after `CircuitOpenError`.

**Contract**:

- `test_health_schema.py`: `/health` response validates against `contracts/health-response.schema.json`.

**Performance**:

- `test_poll_cycle_budget.py`: 10 poll cycles with all circuits open (mocked services respond instantly). Assert mean cycle time ≤ healthy baseline × 1.10 (SC-003).

---

## Complexity Tracking

| Item | Complexity | Justification |
|---|---|---|
| Custom `CircuitBreaker` | Medium | 80 lines, asyncio-safe FSM; justified by no-suitable-async-library |
| 5 service exception taxonomies | Low-Medium | Repetitive pattern; well-defined by research |
| `stamina` Prometheus hook wiring | Low | Single startup registration |
| Daemon degraded-mode extension | Low | One exception branch added |
| Notify node refactor | Low | Replace one `gather` call |

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `stamina` API changes between minor versions | Low | Medium | Pin `stamina>=24.2.0,<25` |
| Circuit breaker opens too aggressively (false positives) | Medium | Medium | Conservative defaults (threshold=3+); `observation_window` prevents stale failures from accumulating |
| Double retry layering for Anthropic | Low | Low | Explicitly set `AsyncAnthropic(max_retries=0)` |
| Concurrent half-open probes | Low | Low | `_half_open_probe_in_flight` flag prevents second probe |
| `stamina.set_active()` not reset after test failure | Low | Low | Use `yield` fixture that resets in teardown |

---

## Dependency Graph (implementation order)

```
Phase A (resilience.py — CircuitBreaker, ResilientAgentService)
    │
    ├── Phase B (config.py — ResilienceConfig)
    │
    ├── Phase C (metrics.py — new Prometheus metrics)
    │       │
    │       └── Phase D (service files — exception taxonomy + retry wrappers)
    │               │
    │               └── Phase E (__main__.py — bootstrap + stamina hook)
    │
    ├── Phase F (daemon.py — CircuitOpenError handler)
    │
    ├── Phase G (health.py — circuit state in response)
    │
    ├── Phase H (notify.py — replace silent gather)
    │
    └── Phase I (tests)
```

Phases A–C can begin in parallel once `research.md` and `data-model.md` are complete. Phase D depends on A+B+C. Phase E depends on D. Phases F, G, H are independent of D and can proceed after A.
