# Feature Specification: External Service Resilience

**Feature Branch**: `005-resilience`
**Created**: 2026-02-22
**Status**: Draft
**Input**: User description: "Make all external service calls (GitHub GraphQL, SSH agent, Slack webhook, SMTP email, Anthropic API) resilient to transient failures. Add retry with exponential backoff and jitter. Add circuit breakers for persistent failures. Ensure LangGraph nodes surface errors cleanly rather than crashing. Define per-service retry budgets and failure semantics. The coordinare must continue polling even when individual service calls fail, and must distinguish between transient errors (retry) and permanent errors (move card to Blocked)."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Board Polling Survives GitHub API Outages (Priority: P1)

The GitHub GraphQL API becomes temporarily unavailable — rate-limited, returning server errors, or timing out. The coordinare does not crash. It logs the failure, waits an appropriate interval, and retries. If the API recovers within the retry budget, the coordinare resumes normal polling with no human intervention. If GitHub remains unavailable beyond the retry budget, the coordinare logs a degraded-mode warning and continues its poll loop, attempting again on the next cycle.

**Why this priority**: GitHub is the coordinare's primary data source. A crash on any GitHub API error means the coordinare cannot operate reliably in any real environment. Surviving GitHub outages is table-stakes reliability.

**Independent Test**: Simulate GitHub API failures (HTTP 502, connection timeout) using a network proxy or mock. Verify the coordinare retries, recovers when the mock recovers, and does not crash if the mock stays down for multiple cycles.

**Acceptance Scenarios**:

1. **Given** the coordinare is polling the board, **When** the GitHub GraphQL API returns a 5xx error, **Then** the coordinare retries the request with exponential backoff, up to the configured retry budget, before logging a warning and skipping the cycle.
2. **Given** the GitHub API has been returning errors for multiple consecutive cycles, **When** the API recovers, **Then** the coordinare resumes normal board polling on the next cycle without restart.
3. **Given** a GitHub API rate-limit response (HTTP 429 with Retry-After header), **When** the coordinare receives it, **Then** it respects the Retry-After value and waits at least that duration before retrying.
4. **Given** the coordinare is in mid-workflow (card In Progress), **When** the GitHub API is unavailable, **Then** the coordinare does not move the card or lose its phase — it holds position and retries.

---

### User Story 2 - Notification Failures Do Not Block the Workflow (Priority: P1)

The Slack webhook or SMTP mail server is unreachable when the coordinare attempts to send a notification after a card transition. The coordinare retries the notification, but if all retries are exhausted it logs the delivery failure and continues the workflow. The card moves as expected — the notification failure does not orphan the card or halt the coordinare.

**Why this priority**: Notifications are informational. A notification failure should never be the reason a card gets stuck or the coordinare crashes. The core workflow must always take precedence over supplementary alerting.

**Independent Test**: Configure Slack to a dead endpoint and SMTP to an unreachable server. Trigger a card transition that produces a notification. Verify the card moves correctly and the coordinare logs the notification failure without crashing.

**Acceptance Scenarios**:

1. **Given** a card transition requiring a Slack notification, **When** the Slack webhook returns an error or times out, **Then** the coordinare retries up to the configured limit, then logs the failure and continues — the card transition is not rolled back.
2. **Given** a card transition requiring an email notification, **When** the SMTP server is unreachable, **Then** the coordinare retries, then logs the failure and continues — the card transition is not rolled back.
3. **Given** both Slack and email fail for the same transition, **When** all retries are exhausted, **Then** the coordinare logs both failures in structured output and continues to the next poll cycle.
4. **Given** notification retries in progress, **When** the service recovers mid-retry, **Then** the notification is delivered successfully and the coordinare resumes normally.

---

### User Story 3 - Agent Transport Failures Are Classified and Handled Per Severity (Priority: P1)

When the coordinare attempts to reach the agent via `AgentService` (spec 004-agent-protocol), it may encounter different failure types: a `TransportTimeoutError` (transient), a `TransportError` from a permanent subprocess or connection failure (permanent), or a valid `ProtocolResponse` with `status: error` or `status: session_expired` (semantic/permanent). The coordinare applies the correct response to each: transient transport errors trigger retries, permanent transport failures move the card to Blocked immediately, and semantic error responses from the agent move the card to Blocked with the agent's reason.

**Why this priority**: The agent transport is the most operationally complex interface. Misclassifying a transient error as permanent (or vice versa) either causes unnecessary card Blocking or infinite retry loops. Correct classification is essential for autonomous operation.

**Independent Test**: Simulate each failure category (timeout, permanent transport error, protocol error) using the mock agent subprocess (spec 004). Verify the coordinare responds correctly to each without crashing.

**Acceptance Scenarios**:

1. **Given** the coordinare is attempting to poll agent status, **When** `AgentService.check_status()` raises `TransportTimeoutError`, **Then** the coordinare classifies this as transient, retries with backoff, and — if retries are exhausted — continues to the next poll cycle without moving the card.
2. **Given** the coordinare is attempting to dispatch, **When** `AgentService.dispatch_card()` raises a permanent `TransportError` (non-zero exit, subprocess launch failure), **Then** the coordinare classifies this as a permanent error, logs a structured error, and moves the card to Blocked.
3. **Given** the coordinare is attempting to dispatch, **When** the agent responds with `status: error`, **Then** the coordinare classifies this as a semantic (permanent) error, moves the card to Blocked with the agent's `reason`, and does not retry.
4. **Given** repeated transient transport failures over multiple consecutive poll cycles, **When** the failure count exceeds the circuit-breaker threshold, **Then** the coordinare opens the circuit, stops calling `AgentService`, logs a structured warning, and moves the card to Blocked.

---

### User Story 4 - Circuit Breaker Prevents Cascading Failure (Priority: P2)

Any external service — GitHub, Slack, SMTP, or the agent transport (`AgentService`) — that fails consistently over a sustained window is automatically quarantined by a circuit breaker. The coordinare stops hammering the failing service and operates in a degraded mode: continuing to run the poll loop, serving health endpoint requests, and logging the open circuit. When the service recovers, the circuit closes and normal operation resumes automatically.

**Why this priority**: Without circuit breakers, a single failing dependency causes the coordinare to spend most of each poll cycle waiting for timeouts, slowing everything else down and generating excessive error log volume. Circuit breakers make degraded-mode operation graceful.

**Independent Test**: Configure a service endpoint to fail persistently. Verify that after the threshold is exceeded, the coordinare stops calling the endpoint, logs the open circuit, and resumes calls when the service is restored.

**Acceptance Scenarios**:

1. **Given** a service that has failed on every attempt for the last N calls (configurable threshold), **When** the coordinare would normally call it again, **Then** the circuit is open and the call is skipped, with a structured log event noting the open circuit.
2. **Given** an open circuit, **When** a configurable recovery window elapses, **Then** the coordinare sends a single probe request. If it succeeds, the circuit closes and normal operation resumes. If it fails, the circuit remains open.
3. **Given** an open circuit for the GitHub service, **When** the health endpoint is queried, **Then** the health response indicates degraded state with the GitHub circuit open.
4. **Given** an open circuit for a notification service (Slack or email), **When** the coordinare needs to send a notification, **Then** it skips the call (circuit is open), logs the skip, and the workflow continues unaffected.

---

### User Story 5 - Operators Can Observe Retry and Failure Behavior (Priority: P3)

Every retry attempt, backoff delay, circuit-breaker state change, and final failure outcome is logged as a structured event with enough context to diagnose the problem: which service failed, what the error was, how many retries were attempted, and what action the coordinare took as a result. Prometheus metrics reflect current circuit states and cumulative retry counts so that monitoring dashboards can alert on sustained failures.

**Why this priority**: Retry and circuit-breaker logic that operates silently is indistinguishable from a hung process. Structured observability of the resilience layer is what makes the coordinare trustworthy in production.

**Independent Test**: Trigger a known failure scenario. Verify that structured log events are emitted for each retry attempt, each backoff, and the final outcome. Verify Prometheus metrics reflect the failure count and circuit state.

**Acceptance Scenarios**:

1. **Given** a service call that requires retries, **When** each retry fires, **Then** a structured log event is emitted containing: service name, attempt number, error description, and backoff duration before next attempt.
2. **Given** a circuit breaker state change (closed → open, or open → closed), **When** the transition occurs, **Then** a structured log event is emitted with the service name, new state, and reason for the change.
3. **Given** the coordinare is running with failures in progress, **When** the Prometheus metrics endpoint is scraped, **Then** it exposes per-service retry counts and current circuit-breaker states.
4. **Given** all retries exhausted for a call, **When** the coordinare takes a fallback action (skip cycle, move card to Blocked), **Then** the log event records both the failure and the action taken.

---

### Edge Cases

- What happens when the backoff interval would exceed the poll interval? → The coordinare caps the backoff at the poll interval to ensure it doesn't delay the entire poll cycle beyond expectations.
- What happens when multiple services fail simultaneously? → Each service has its own independent retry budget and circuit breaker. Failures are independent; one open circuit does not affect others.
- What happens if the retry logic itself throws an exception? → Retry infrastructure errors are treated as non-retriable and propagate to the LangGraph node's error handler, which logs them and transitions to the appropriate fallback state.
- What happens during backoff if a shutdown signal is received? → The coordinare abandons the pending retry immediately and proceeds with graceful shutdown.
- What happens if the Anthropic API (Claude) fails during card assessment? → Claude failures during assess_card are treated as transient; the card is not dispatched until assessment succeeds or retries are exhausted, at which point the card moves to Blocked.
- What distinguishes a transient error from a permanent one? → HTTP 5xx, connection timeout, and DNS resolution failure are transient. HTTP 4xx (except 429), authentication failure, and invalid credentials are permanent. Agent semantic errors (status: error, status: session_expired) are permanent.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Every external service call (GitHub GraphQL, agent transport via `AgentService`, Slack webhook, SMTP, Anthropic API) MUST be wrapped in retry logic using the `stamina` library (`stamina.retry()` async decorator), with configurable maximum attempts, initial backoff, and backoff multiplier. `stamina`'s built-in instrumentation MUST be used to satisfy FR-014 retry counter requirements.
- **FR-002**: Retry backoff MUST use exponential growth with random jitter to prevent synchronized retries across multiple deployments.
- **FR-003**: Backoff intervals MUST be capped at the configured poll interval to prevent a single retry from delaying the entire poll cycle.
- **FR-004**: Transient errors (network timeout, 5xx, DNS failure, connection refused) MUST be retried. Permanent errors (4xx except 429, authentication failure, invalid credentials) MUST NOT be retried.
- **FR-005**: HTTP 429 (rate-limit) responses MUST be retried after respecting any server-provided Retry-After value; the Retry-After duration overrides the standard backoff calculation.
- **FR-006**: Every service MUST have an independent circuit breaker implemented as a custom `CircuitBreaker` class in `src/coordinare/resilience.py` (no additional library dependency). It MUST open after a configurable number of consecutive failures within a configurable time window.
- **FR-007**: An open circuit MUST be periodically probed with a single request after a configurable recovery window; a successful probe closes the circuit.
- **FR-008**: Notification service failures (Slack, SMTP) MUST NOT block card workflow transitions; they are logged and skipped if all retries are exhausted.
- **FR-009**: Permanent agent transport failures (`TransportError` after retries exhausted, i.e., subprocess launch failure, persistent non-zero exit) MUST move the active card to Blocked rather than leaving the coordinare in an infinite retry loop. Resilience is applied at the `AgentService` method boundary using a Decorator wrapping `AgentServiceProtocol`.
- **FR-010**: Agent semantic errors (`status: error`, `status: session_expired`) MUST be treated as permanent and not retried at the transport level.
- **FR-011**: Every retry attempt MUST emit a structured log event with: service name, attempt number, error type, error description, and time until next attempt.
- **FR-012**: Every circuit-breaker state transition MUST emit a structured log event with: service name, previous state, new state, and triggering event.
- **FR-013**: The health endpoint MUST reflect the current state (closed, open, half-open) of each service's circuit breaker.
- **FR-014**: Prometheus metrics MUST expose per-service counters for: total call attempts, successful calls, failed calls, retries, and current circuit-breaker state.
- **FR-015**: All retry and circuit-breaker parameters (max attempts, initial backoff, backoff multiplier, circuit threshold, recovery window) MUST be configurable via the configuration file with documented defaults.
- **FR-016**: The LangGraph graph MUST NOT crash due to an unhandled service exception; all service errors MUST be caught at the node level and result in a defined state transition.

### Key Entities

- **ResilientAgentService** (Decorator): Wraps `AgentServiceProtocol` (spec 004) and applies `stamina.retry()` + `CircuitBreaker` around `dispatch_card`, `check_status`, and `relay_feedback`. Implements `AgentServiceProtocol` so graph nodes require no change. `check_health` uses a fixed 10 s timeout (spec 004 FR-013) and is not retried.

- **RetryPolicy**: Configuration fed into `stamina.retry()` for each service.
  - Max attempts (total calls including the first)
  - Initial backoff duration
  - Backoff multiplier (for exponential growth)
  - Jitter strategy (random within a percentage band, provided by `stamina` natively)
  - Backoff cap (maximum backoff before capping)
  - Permanent error classifications (HTTP status codes, exception types — passed as `on` kwarg to `stamina.retry()`)

- **CircuitBreaker** (`src/coordinare/resilience.py`, custom class — no extra library): A stateful guard around a service that opens under sustained failure. Chosen over `pybreaker`/`aiobreaker` to avoid a new dependency; three-state logic is ~50 lines and integrates directly with `structlog` (FR-012) and Prometheus gauges (FR-014).
  - Current state: `closed` (normal), `open` (calls skipped), `half_open` (single probe in flight)
  - Failure threshold: consecutive failures within observation window that open the circuit
  - Observation window: sliding time window in which failures are counted
  - Recovery window: time to wait in `open` state before transitioning to `half_open`
  - On state transition: emits structlog event + updates Prometheus gauge

- **ServiceCallOutcome**: The result of a single external call attempt, used for logging and metrics.
  - Service name
  - Action attempted
  - Success / failure
  - Error type and description (on failure)
  - Attempt number
  - Retry decision (will retry / exhausted / permanent)

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A transient failure in any single external service (GitHub, Slack, SMTP, agent transport, Anthropic) does not crash the coordinare in 100% of test cases.
- **SC-002**: The coordinare resumes normal operation within two poll cycles after a service that was failing transiently recovers, with no manual intervention.
- **SC-003**: When a service fails persistently and the circuit opens, the coordinare's poll cycle duration does not increase by more than 10% compared to a healthy-service baseline (no timeout accumulation).
- **SC-004**: Every retry attempt and circuit-breaker state change produces a structured log event — zero silent failures in the resilience layer.
- **SC-005**: All retry and circuit-breaker parameters can be changed via configuration with no code changes required.
- **SC-006**: An active card is never silently stuck due to a service failure — within one poll cycle of any permanent failure, the card is either still being retried or has been moved to Blocked with a logged reason.

## Clarifications

### Session 2026-02-22

- Q: Should resilience (retry + circuit-breaker) for agent communication attach inside `SubprocessTransport.send()`, at the `AgentService` method boundary, or at the LangGraph node level? → A: Attach at the `AgentService` method boundary using a Decorator (`ResilientAgentService` implements `AgentServiceProtocol`). This keeps `SubprocessTransport` responsible only for I/O, graph nodes responsible only for state transitions, and the resilience layer responsible only for retry/circuit-breaker logic — each component has a single responsibility. New transports are automatically resilient with zero extra code.
- Q: Should retry logic use `tenacity`, `stamina`, or a bespoke async utility? → A: `stamina` library. Built on `tenacity`, ships with first-class `structlog` integration (already present in this project), and its instrumentation hooks integrate with Prometheus via OpenTelemetry — satisfying FR-014 retry counters with minimal boilerplate. Circuit-breaker state (FR-006/FR-007/FR-013) is handled by the custom `CircuitBreaker` class (see Q3 below).
- Q: Should the circuit breaker use `pybreaker`, `aiobreaker`, or a custom class? → A: Custom `CircuitBreaker` class in `src/coordinare/resilience.py`. Three-state FSM (closed → open → half_open → closed) is ~50 lines; avoids a new dependency; integrates directly with `structlog` for FR-012 transition events and Prometheus gauges for FR-014 circuit state metrics. `stamina` handles retry; this class handles circuit-break independently, composing at the `ResilientAgentService` / service-wrapper boundary.

## Assumptions

- Retry and circuit-breaker parameters have sensible defaults that work for typical cloud API SLAs without requiring operator tuning on first deployment. Defaults will be defined during planning.
- Each external service has its own independent retry policy and circuit breaker. There is no shared budget across services.
- The Anthropic API is used only during `assess_card`. Failures there are treated identically to other transient service failures.
- Backoff jitter is computed locally (random number generation); no distributed coordination is needed.
- This spec addresses runtime resilience only. Start-up failures (bad config, unreachable required services) are governed by existing startup validation behavior and are out of scope here.
- The coordinare runs as a single process. Retry state and circuit-breaker state are in-memory within a single process run. Circuit breakers reset to closed on process restart.
