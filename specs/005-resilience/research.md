# Research: External Service Resilience

**Branch**: `005-resilience` | **Date**: 2026-02-22

---

## Topic 1: `stamina` Library — Async Retry

**Decision**: Use `stamina>=24.2.0` as the retry decorator library. Add to `pyproject.toml`.

**Key API surface**:

```python
@stamina.retry(
    on=TransientError,             # exception type(s) to retry
    attempts=4,                    # max total attempts (including first)
    wait_initial=2.0,              # seconds before first retry
    wait_max=30.0,                 # backoff cap (set to poll_interval_seconds)
    wait_jitter=1.0,               # uniform jitter range added to each wait
    wait_exp_base=2.0,             # doubling factor
)
async def my_async_call() -> ...:
    ...
```

`stamina.retry()` detects `async def` at decoration time and wraps with `asyncio.sleep()` for backoff — no threading, fully asyncio-safe.

`stamina.set_active(False)` in test fixtures disables all retry delays globally, keeping tests fast. Must be placed in `conftest.py` for unit/integration tests.

`stamina` is **not yet installed** in the project. It must be added: `stamina>=24.2.0`. It has `tenacity` as a dependency (tenacity is already present as a transitive dep via `langgraph`).

**Prometheus integration**: `stamina` exposes `stamina.instrumentation` hooks. Since the project uses `prometheus_client` directly (not OpenTelemetry), retry counters must be wired via a `before_sleep` callback registered with stamina's instrumentation API, or by manually incrementing counters inside a thin wrapper. The `coordinare_service_retries_total` counter (service, action labels) is incremented in a `before_sleep_hook` registered at startup.

**Rationale**: Chosen over raw `tenacity` for: (1) simpler opinionated API, (2) `set_active()` test toggle, (3) structlog-native design. Chosen over bespoke wrapper per spec clarifications.

**Alternatives considered**: raw tenacity (more verbose, no test toggle), custom async utility (rejected in clarifications).

---

## Topic 2: Custom `CircuitBreaker` Class

**Decision**: Implement a bespoke `CircuitBreaker` in `src/coordinare/resilience.py`. No additional library.

**Design principles**:
- Single-process asyncio: no `threading.Lock` or `asyncio.Lock` needed. State transitions are synchronous (no `await` in FSM logic), so they are effectively atomic within a single event loop.
- Three states: `CLOSED` → `OPEN` → `HALF_OPEN` → `CLOSED` (or back to `OPEN` on probe failure).
- Failure timestamps stored in `collections.deque` for sliding-window failure counting.
- Timing via `time.monotonic()`.
- Half-open probe guard: `_half_open_probe_in_flight` boolean prevents concurrent probes.

**Composition with stamina**: Circuit breaker wraps the entire stamina-retried call as a single unit via an `async with cb.guard():` context manager. Failure is counted **once per exhausted retry budget**, not per individual attempt. This prevents aggressive circuit opening from internal retry loops.

```
CB.allow_request()           ← synchronous guard check (no await)
  stamina retry loop (N attempts with backoff)
    if any attempt succeeds → CB.record_success()
    if all exhausted        → CB.record_failure() [once]
```

**Prometheus**: `coordinare_circuit_breaker_state{service, state}` Gauge registered to `METRICS.registry`. On every state transition, the old state label is set to 0 and the new state label is set to 1.

**structlog event**: `log.warning("circuit_breaker.state_changed", service=..., previous_state=..., new_state=..., reason=...)` on every transition.

**Rationale**: Avoids `pybreaker` (uses `threading.RLock`, philosophically mismatched for asyncio) and `aiobreaker` (unmaintained, last release 2021). Custom class is ~80 lines and integrates directly with existing structlog/prometheus_client patterns.

**Alternatives considered**: `pybreaker` (threading locks, wrong abstraction), `aiobreaker` (unmaintained), `circuitbreaker` PyPI (sync-only).

---

## Topic 3: Stamina + CircuitBreaker Composition Order

**Decision**: Circuit breaker `allow_request()` executes **first** (before stamina retry loop begins). The `guard()` async context manager wraps the entire stamina-retried call.

**Pattern**:

```python
# Internal: stamina-retried raw call
@stamina.retry(on=TransientError, attempts=N, ...)
async def _raw_call(...) -> T:
    return await service_layer_call(...)

# Public: circuit breaker wraps the whole stamina call
async def public_call(...) -> T:
    async with circuit_breaker.guard():
        return await _raw_call(...)
```

**Counting semantics**: `guard().__aexit__` calls `record_failure()` exactly once per `_raw_call` invocation that raises (i.e., after all stamina retries are exhausted). This means the circuit opens after `failure_threshold` fully-exhausted retry budgets, not after `failure_threshold` individual attempts.

**Rationale**: Placing CB outside the stamina loop preserves clean separation of concerns (spec clarification Q1) and prevents `CircuitOpenError` from accidentally being retried by stamina.

**Alternatives considered**: CB inside stamina retry body (rejected — could retry CircuitOpenError), counting per attempt (rejected — too aggressive).

---

## Topic 4: Per-Service Retry and Circuit Breaker Defaults

All `wait_max` values are capped at 30s (`poll_interval_seconds` default). `timeout` parameters are for the entire retry budget (all attempts).

| Service | `attempts` | `wait_initial` | `wait_max` | `wait_jitter` | CB threshold | CB recovery |
|---------|-----------|----------------|------------|----------------|--------------|-------------|
| GitHub  | 4 | 2.0 s | 30.0 s | 2.0 s | 3 | 120 s |
| Slack   | 3 | 1.0 s | 15.0 s | 1.0 s | 5 | 120 s |
| SMTP    | 3 | 2.0 s | 20.0 s | 1.5 s | 5 | 300 s |
| Anthropic | 4 | 5.0 s | 30.0 s | 2.0 s | 3 | 120 s |
| AgentTransport | 3 | 2.0 s | 20.0 s | 1.0 s | 3 | 60 s |

**Anthropic SDK**: The `anthropic` SDK has built-in retries (`max_retries=2` by default). Disable with `AsyncAnthropic(max_retries=0)` to prevent double-retry layering and give stamina full visibility into all attempts.

**Exception taxonomy** (permanent errors are NOT in `on=` tuple, propagate immediately):

| Service | Transient (retry) | Permanent (block) |
|---------|-------------------|-------------------|
| GitHub | `aiohttp.ClientError`, 5xx, `asyncio.TimeoutError`, DNS | 401, 403, 404, `ValueError` (malformed) |
| Slack | `httpx.TimeoutException`, `httpx.NetworkError`, 5xx | 4xx (bad webhook URL) |
| SMTP | `SMTPConnectError`, `SMTPServerDisconnected`, 4xx SMTP codes | `SMTPAuthenticationError`, `SMTPRecipientsRefused` |
| Anthropic | `APIConnectionError`, `APITimeoutError`, 5xx `APIStatusError` | `AuthenticationError`, 4xx |
| AgentTransport | `TransportTimeoutError` | `TransportError` (non-zero exit, launch failure) |

---

## Topic 5: HTTP 429 / Retry-After Handling

**Decision**: Use Pattern A — sleep inside the raw execution function before raising `TransientError`.

```python
if resp.status_code == 429:
    raw = resp.headers.get("Retry-After", "60")
    retry_after = float(raw) if raw.replace(".", "").isdigit() else 60.0
    if stamina.is_active():
        await asyncio.sleep(retry_after)
    raise TransientGitHubError(f"rate-limited; waited {retry_after}s")
```

The `stamina.is_active()` guard prevents the sleep from firing in unit tests (where `stamina.set_active(False)` is set). After sleeping, stamina's own backoff applies; keep `wait_initial` small (2.0s) so the additive effect is minor.

`Retry-After` can be a seconds integer or an HTTP-date string. Handle both: `float(raw)` for integers, `email.utils.parsedate_to_datetime()` for date strings.

**Rationale**: Keeps stamina as the sole retry manager; `set_active()` test toggle still works; simple to audit. Pattern B (raw tenacity `wait_exception`) would lose stamina instrumentation.

**Alternatives considered**: tenacity `wait_exception` (more precise but loses stamina OTel hooks), hybrid stamina internals (fragile, not public API).

---

## Topic 6: Existing Codebase Integration Points

**Key finding**: Zero retry logic exists anywhere in the current codebase. All exceptions from service methods propagate immediately to `daemon.py`, which treats ANY unhandled exception as a fatal crash (`self._running = False`, `sys.exit(1)`).

**Notify node**: Currently uses `asyncio.gather(..., return_exceptions=True)` which silently swallows all email and Slack errors. This must be changed to log errors while still not blocking the workflow transition (consistent with FR-008).

**External service call locations**:

| Node | Services Called |
|------|----------------|
| `check_board` | GitHub |
| `assess_card` | GitHub, Anthropic (Claude) |
| `dispatch_card` | GitHub, AgentService |
| `monitor_agent` | AgentService |
| `monitor_pr` | GitHub |
| `merge_pr` | GitHub |
| `handle_blocked` | GitHub |
| `relay_feedback` | AgentService |
| `notify` | Slack, Email |

**Current exception handling in daemon.py**: Catches `Exception` → sets `self._running = False` → raises `RuntimeExecutionError` → `sys.exit(1)`. This must be extended to treat `CircuitOpenError` as a degraded-mode event (log warning, continue polling) rather than a fatal crash.

---

## Topic 7: Prometheus Metrics — New Additions

New metrics for `src/coordinare/metrics.py`:

```
coordinare_service_retries_total{service, action}    # Counter — stamina before_sleep callback
coordinare_service_calls_total{service, action, outcome}  # Counter (success/failure/circuit_open)
coordinare_circuit_breaker_state{service, state}     # Gauge (1=current, 0=not)
```

The circuit breaker Gauge uses multi-label pattern `{service="github", state="open"} = 1`, `{service="github", state="closed"} = 0`, etc. This enables Prometheus alerting queries like:
```promql
coordinare_circuit_breaker_state{state="open"} == 1
```

---

## Topic 8: Health Endpoint Extension (Spec 005)

Current health.py infers GitHub connectivity from poll recency. With circuit breakers, each service's state is directly known. The health response must be extended with a `circuit_breakers` dict mapping service name to state string (`"closed"`, `"open"`, `"half_open"`).

When any non-notification service circuit is open, the overall health `status` should be `"degraded"` (not `"unhealthy"` — the coordinare is still running). When a notification circuit (Slack/SMTP) is open, status stays `"ok"` since notifications are not blocking.

---

## Summary of Key Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Retry library | `stamina>=24.2.0` | Simpler API, `set_active()` test toggle, structlog-native |
| Circuit breaker | Custom class in `resilience.py` | Avoids new dep; 80 lines; direct structlog+prometheus integration |
| CB attachment for agent | `ResilientAgentService` Decorator (spec 004) | Single responsibility; new transports automatically resilient |
| CB attachment for other services | Inline in service methods | No Protocol interface to wrap; stamina inside method + CB guard at call site |
| Retry-After handling | Sleep inside raw call, `stamina.is_active()` guard | Preserves stamina as sole retry manager and test toggle |
| Failure counting | One CB failure per exhausted stamina budget | Prevents over-aggressive circuit opening from internal retries |
| Anthropic SDK retries | Disable with `max_retries=0` | Prevents double retry layering |
| Notify node error handling | Replace silent `return_exceptions=True` with explicit log | FR-008 requires logged failures, not silent swallowing |
