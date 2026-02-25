# Data Model: External Service Resilience

**Branch**: `005-resilience` | **Date**: 2026-02-22

---

## New File: `src/coordinare/resilience.py`

### `CircuitState` (Enum)

```python
class CircuitState(str, Enum):
    CLOSED   = "closed"     # Normal operation; calls allowed
    OPEN     = "open"       # Calls blocked; waiting for recovery window
    HALF_OPEN = "half_open" # Single probe request allowed
```

### `CircuitOpenError` (Exception)

```python
class CircuitOpenError(RuntimeError):
    """
    Raised when a call is blocked by an open circuit breaker.
    NOT retried by stamina — excluded from all `on=` tuples.
    Caught by daemon.py as a degraded-mode event (logs warning, continues polling).
    """
    def __init__(self, service_name: str) -> None:
        super().__init__(f"{service_name} circuit is open — call skipped")
        self.service_name = service_name
```

### `CircuitBreaker`

```python
@dataclass
class CircuitBreaker:
    """
    Asyncio-safe three-state FSM circuit breaker.

    Single-process asyncio guarantee: state transitions are synchronous
    (no awaits in FSM logic), so they are effectively atomic.
    No threading.Lock or asyncio.Lock required.
    """
    service_name: str
    failure_threshold: int          # Exhausted retry budgets to open circuit
    recovery_window: float          # Seconds in OPEN before transitioning to HALF_OPEN
    observation_window: float       # Sliding window for failure counting (seconds)
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_times: deque[float] = field(default_factory=deque, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _half_open_probe_in_flight: bool = field(default=False, init=False)

    # Public read-only access
    @property
    def state(self) -> CircuitState: ...

    # Called before issuing a request (synchronous, no await)
    def allow_request(self) -> bool: ...

    # Called after a successful request
    def record_success(self) -> None: ...

    # Called after an exhausted retry budget (one failure = one exhausted budget)
    def record_failure(self) -> None: ...

    # Async context manager: guards a stamina-retried call
    @asynccontextmanager
    async def guard(self) -> AsyncGenerator[None, None]: ...
```

**State transition table**:

| From | Event | To | Reason logged |
|------|-------|----|---------------|
| CLOSED | `failure_threshold` budgets exhausted in `observation_window` | OPEN | `"failure_threshold_exceeded"` |
| OPEN | `recovery_window` elapsed | HALF_OPEN | `"recovery_window_elapsed"` |
| HALF_OPEN | probe succeeds | CLOSED | `"probe_succeeded"` |
| HALF_OPEN | probe fails | OPEN | `"probe_failed"` |
| HALF_OPEN | second concurrent request | — (blocked) | no transition |

**`guard()` contract**:
1. Calls `allow_request()` synchronously; raises `CircuitOpenError` if blocked.
2. Awaits the guarded coroutine.
3. On success: calls `record_success()`.
4. On any exception: calls `record_failure()`, re-raises.

### `RetryConfig`

```python
@dataclass(frozen=True)
class RetryConfig:
    """Per-service stamina.retry() parameters."""
    attempts: int               # Total attempts including first call
    wait_initial: float         # Seconds before first retry
    wait_max: float             # Backoff cap (≤ poll_interval_seconds)
    wait_jitter: float          # Uniform jitter range in seconds
    wait_exp_base: float = 2.0  # Exponential multiplier

    def to_stamina_kwargs(self) -> dict[str, Any]:
        """Returns kwargs dict for stamina.retry()."""
        ...
```

### `ResilientAgentService` (Decorator — depends on spec 004)

```python
class ResilientAgentService:
    """
    Decorator implementing AgentServiceProtocol (spec 004).
    Wraps a concrete AgentService instance with stamina retry + circuit breaker.
    Graph nodes see only AgentServiceProtocol — no change required there.

    check_health() is NOT retried (fixed 10s timeout per spec 004 FR-013).
    """
    def __init__(
        self,
        inner: AgentServiceProtocol,
        retry_config: RetryConfig,
        circuit_breaker: CircuitBreaker,
    ) -> None: ...

    # Implements AgentServiceProtocol:
    async def dispatch_card(self, card: Card, ...) -> ProtocolResponse: ...
    async def check_status(self, session_id: str) -> ProtocolResponse: ...
    async def relay_feedback(self, session_id: str, payload: dict) -> ProtocolResponse: ...
    async def check_health(self) -> ProtocolResponse: ...  # no retry, no CB guard
```

---

## Modified: `src/coordinare/config.py`

### `ServiceRetryConfig` (Pydantic model, nested per-service config)

```python
class ServiceRetryConfig(BaseModel):
    attempts: int = Field(ge=1, le=10)
    wait_initial_seconds: float = Field(ge=0.1, le=60.0)
    wait_max_seconds: float = Field(ge=1.0, le=300.0)
    wait_jitter_seconds: float = Field(ge=0.0, le=30.0)

class ServiceCircuitConfig(BaseModel):
    failure_threshold: int = Field(ge=1, le=20)
    recovery_window_seconds: float = Field(ge=10.0, le=3600.0)
    observation_window_seconds: float = Field(ge=10.0, le=3600.0)
```

### `ResilienceConfig` (nested in `ProjectConfiguration`)

```python
class ResilienceConfig(BaseModel):
    github_retry: ServiceRetryConfig = ServiceRetryConfig(
        attempts=4, wait_initial_seconds=2.0, wait_max_seconds=30.0, wait_jitter_seconds=2.0
    )
    slack_retry: ServiceRetryConfig = ServiceRetryConfig(
        attempts=3, wait_initial_seconds=1.0, wait_max_seconds=15.0, wait_jitter_seconds=1.0
    )
    smtp_retry: ServiceRetryConfig = ServiceRetryConfig(
        attempts=3, wait_initial_seconds=2.0, wait_max_seconds=20.0, wait_jitter_seconds=1.5
    )
    anthropic_retry: ServiceRetryConfig = ServiceRetryConfig(
        attempts=4, wait_initial_seconds=5.0, wait_max_seconds=30.0, wait_jitter_seconds=2.0
    )
    agent_retry: ServiceRetryConfig = ServiceRetryConfig(
        attempts=3, wait_initial_seconds=2.0, wait_max_seconds=20.0, wait_jitter_seconds=1.0
    )
    github_circuit: ServiceCircuitConfig = ServiceCircuitConfig(
        failure_threshold=3, recovery_window_seconds=120.0, observation_window_seconds=300.0
    )
    slack_circuit: ServiceCircuitConfig = ServiceCircuitConfig(
        failure_threshold=5, recovery_window_seconds=120.0, observation_window_seconds=300.0
    )
    smtp_circuit: ServiceCircuitConfig = ServiceCircuitConfig(
        failure_threshold=5, recovery_window_seconds=300.0, observation_window_seconds=600.0
    )
    anthropic_circuit: ServiceCircuitConfig = ServiceCircuitConfig(
        failure_threshold=3, recovery_window_seconds=120.0, observation_window_seconds=300.0
    )
    agent_circuit: ServiceCircuitConfig = ServiceCircuitConfig(
        failure_threshold=3, recovery_window_seconds=60.0, observation_window_seconds=180.0
    )
```

All fields are configurable via YAML; defaults require no tuning on first deployment.

---

## Modified: `src/coordinare/metrics.py`

### New Prometheus Metrics

```python
# Gauge: 1.0 = current state, 0.0 = not in this state
# Labels: service (github/slack/smtp/anthropic/agent), state (closed/open/half_open)
CIRCUIT_BREAKER_STATE = Gauge(
    "coordinare_circuit_breaker_state",
    "Current circuit breaker state per service (1=active, 0=inactive)",
    labelnames=("service", "state"),
)

# Counter: incremented once per retry attempt (before each sleep)
SERVICE_RETRIES_TOTAL = Counter(
    "coordinare_service_retries_total",
    "Total retry attempts per service and action",
    labelnames=("service", "action"),
)

# Counter: incremented once per call outcome (success/failure/circuit_open)
SERVICE_CALLS_TOTAL = Counter(
    "coordinare_service_calls_total",
    "Total service call outcomes",
    labelnames=("service", "action", "outcome"),
)
```

---

## Modified: `src/coordinare/health.py`

### Health Response Extension

```python
class ServiceCircuitStatus(TypedDict):
    state: str                  # "closed" | "open" | "half_open"
    opened_at: str | None       # ISO-8601 timestamp when circuit last opened (null when closed)
    failure_count: int          # exhausted retry budgets within current observation window

class HealthResponse(TypedDict):
    status: str                 # "ok" | "degraded" | "unhealthy"
    phase: str                  # from spec 003
    snapshot_at: str | None     # from spec 003
    circuit_breakers: dict[str, ServiceCircuitStatus]  # NEW
    # ...existing fields...
```

**Status derivation logic**:
- `"ok"`: all circuits closed
- `"degraded"`: any non-notification circuit (github/anthropic/agent) is open
- Notification circuits (slack/smtp) open → logged but do not change overall status

---

## Domain Exception Taxonomy

### GitHub Exceptions

```python
class GitHubError(RuntimeError): ...
class TransientGitHubError(GitHubError): ...  # 5xx, timeout, DNS, connection refused
class PermanentGitHubError(GitHubError): ...  # 4xx (except 429), malformed response
class RateLimitedGitHubError(TransientGitHubError):
    retry_after: float                         # seconds from Retry-After header
```

### Slack Exceptions

```python
class SlackError(RuntimeError): ...
class TransientSlackError(SlackError): ...    # 5xx, timeout, network
class PermanentSlackError(SlackError): ...    # 4xx (bad webhook URL, invalid payload)
```

### SMTP Exceptions (mapped from aiosmtplib errors)

```python
class SMTPDeliveryError(RuntimeError): ...
class TransientSMTPError(SMTPDeliveryError): ...   # connect fail, server disconnect, 4xx codes
class PermanentSMTPError(SMTPDeliveryError): ...   # auth failure, no such user, relaying denied
```

### Anthropic Exceptions (mapped from anthropic SDK)

```python
class AnthropicCallError(RuntimeError): ...
class TransientAnthropicError(AnthropicCallError): ...  # APIConnectionError, APITimeoutError, 5xx
class PermanentAnthropicError(AnthropicCallError): ...  # AuthenticationError, 4xx
```

**Agent exceptions** (`TransportError`, `TransportTimeoutError`, `CircuitOpenError`) are defined in `src/coordinare/resilience.py` (for `CircuitOpenError`) and `src/coordinare/transport/base.py` (spec 004, for the transport errors).

---

## Snapshot of File Ownership

| File | Status | New Entities |
|------|--------|-------------|
| `src/coordinare/resilience.py` | **NEW** | `CircuitState`, `CircuitBreaker`, `CircuitOpenError`, `RetryConfig`, `ResilientAgentService` |
| `src/coordinare/config.py` | **MODIFY** | `ServiceRetryConfig`, `ServiceCircuitConfig`, `ResilienceConfig` nested in `ProjectConfiguration` |
| `src/coordinare/metrics.py` | **MODIFY** | `CIRCUIT_BREAKER_STATE`, `SERVICE_RETRIES_TOTAL`, `SERVICE_CALLS_TOTAL` |
| `src/coordinare/health.py` | **MODIFY** | `ServiceCircuitStatus` in health response, status derivation logic |
| `src/coordinare/services/github.py` | **MODIFY** | `TransientGitHubError`, `PermanentGitHubError`, `RateLimitedGitHubError`; stamina decorators; CB guard |
| `src/coordinare/services/slack.py` | **MODIFY** | `TransientSlackError`, `PermanentSlackError`; stamina decorator; CB guard |
| `src/coordinare/services/email.py` | **MODIFY** | `TransientSMTPError`, `PermanentSMTPError`; stamina decorator; CB guard |
| `src/coordinare/services/claude.py` | **MODIFY** | `TransientAnthropicError`, `PermanentAnthropicError`; stamina decorator; CB guard; `max_retries=0` |
| `src/coordinare/services/agent_ssh.py` | **SUPERSEDED** | No changes — replaced by spec 004 `AgentService` + `ResilientAgentService` from spec 005 |
| `src/coordinare/__main__.py` | **MODIFY** | Circuit breaker construction and injection; stamina hook registration |
| `src/coordinare/daemon.py` | **MODIFY** | `CircuitOpenError` handler → degraded-mode log, not crash |
| `src/coordinare/graph/nodes/notify.py` | **MODIFY** | Replace silent `return_exceptions=True` with structured error logging |
| `pyproject.toml` | **MODIFY** | Add `stamina>=24.2.0` |
