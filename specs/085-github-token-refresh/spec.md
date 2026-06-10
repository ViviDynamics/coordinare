# Feature Specification: Recover from GitHub auth 401s by refreshing the token

**Feature Branch**: `085-github-token-refresh`  
**Created**: 2026-06-10  
**Status**: Draft  
**Input**: User description: "Refresh stale GitHub auth tokens on GraphQL 401 so the daemon recovers automatically instead of starving board polling"

## User Scenarios & Testing *(mandatory)*

<!--
  IMPORTANT: User stories should be PRIORITIZED as user journeys ordered by importance.
  Each user story/journey must be INDEPENDENTLY TESTABLE - meaning if you implement just ONE of them,
  you should still have a viable MVP (Minimum Viable Product) that delivers value.
-->

### User Story 1 - Board polling recovers after a credential goes stale (Priority: P1)

The coordinare daemon polls the GitHub project board every cycle to discover and
dispatch work. When the active GitHub credential is rejected mid-run (the server
returns an authorization failure), the daemon must refresh the credential and
retry the same request once, so polling recovers within the same cycle instead of
surfacing a transient error and silently starving dispatch until the next restart.

**Why this priority**: This is the production incident driving the feature. An
intermittent authorization failure currently misclassifies as a generic transient
error, no fresh credential is ever obtained, and board polling stops making
progress — work cards sit undispatched. Fixing this restores autonomous operation.

**Independent Test**: Drive board polling against a credential provider whose first
minted credential is rejected with an authorization failure and whose next minted
credential is accepted. Verify the operation succeeds without operator intervention
and the board is polled.

**Acceptance Scenarios**:

1. **Given** a refreshable credential that the server rejects with an authorization
   failure, **When** board polling issues the request, **Then** the system
   invalidates the cached credential, obtains a fresh one, retries the request once,
   and the retry succeeds.
2. **Given** the refresh produces a new credential that the server *still* rejects,
   **When** the single retry also fails with an authorization failure, **Then** the
   system raises exactly one permanent authorization error and does not loop or
   retry further.
3. **Given** a successful refresh-and-retry, **When** the next request is issued,
   **Then** it carries the refreshed credential (the stale one is not reused).

---

### User Story 2 - A genuinely invalid credential fails fast and clearly (Priority: P2)

When the configured credential is static (cannot be refreshed) and the server
rejects it, the system must fail immediately with one clear permanent authorization
error rather than attempting a pointless refresh-and-retry loop, and these auth
failures must not degrade the service-health/circuit-breaker state.

**Why this priority**: Without this, a static-credential misconfiguration could
either loop uselessly or trip the circuit breaker and mask the real cause. Operators
need a single, unambiguous signal that the credential itself is bad.

**Independent Test**: Configure a static (non-refreshable) credential the server
rejects with an authorization failure; verify a single permanent error is raised,
no retry occurs, and the circuit-breaker/service-health counters are unaffected.

**Acceptance Scenarios**:

1. **Given** a static credential rejected with an authorization failure, **When** a
   request is issued, **Then** the system raises one permanent authorization error
   and performs no retry.
2. **Given** repeated authorization failures, **When** they occur, **Then** they are
   not counted against the circuit breaker / service-health state.

---

### User Story 3 - Operators can see that refresh-and-retry happened (Priority: P3)

When the system refreshes a credential and retries, it must emit an observability
record naming the action taken and its outcome (recovered or failed), so operators
can correlate recoveries with board-polling behavior — without ever exposing secret
material.

**Why this priority**: Observability is valuable for diagnosing recurrence and
confirming the fix works in production, but the recovery behavior itself (P1/P2)
delivers the core value even without it.

**Independent Test**: Trigger a refresh-and-retry and a refresh-then-still-failing
case; verify a record is emitted naming the action and outcome, and assert no field
of any emitted record contains credential, token, header, or response-body text.

**Acceptance Scenarios**:

1. **Given** a refresh-and-retry that recovers, **When** it completes, **Then** an
   observability record names the action and a "recovered" outcome.
2. **Given** a refresh-and-retry that still fails, **When** it completes, **Then** an
   observability record names the action and a "failed" outcome.
3. **Given** any emitted record, **When** inspected, **Then** no field contains
   credential, token, authorization-header, or response-body content.

---

### Edge Cases

- **Two shapes of authorization failure**: the failure may arrive as a
  transport-level non-2xx response (no structured query payload) or as a
  structured response-level authorization error. Both MUST be recognized as
  authorization failures.
- **Concurrent callers**: when several in-flight requests hit the authorization
  failure at once, they MUST share a single refresh rather than each minting a new
  credential (no refresh stampede).
- **Refresh itself fails transiently**: if obtaining a fresh credential fails for a
  transient reason (e.g. a 5xx from the credential mint), that surfaces as a
  transient error; a 4xx from the mint surfaces as permanent.
- **Non-authorization non-2xx**: a non-authorization failure (rate limit, server
  error, not-found, validation) keeps its existing classification and does NOT
  trigger a refresh.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST recognize a GitHub authorization failure whether it
  arrives as a transport-level non-2xx response or as a structured response-level
  authorization error.
- **FR-002**: On an authorization failure with a refreshable credential, the system
  MUST invalidate the cached credential and obtain a fresh one before retrying.
- **FR-003**: System MUST retry the failed request exactly once after a successful
  refresh.
- **FR-004**: If the single post-refresh retry also fails with an authorization
  failure, the system MUST raise exactly one permanent authorization error and MUST
  NOT retry further or loop.
- **FR-005**: With a static (non-refreshable) credential, an authorization failure
  MUST surface immediately as a permanent error with no retry; the invalidate
  operation MUST be a safe no-op for static credentials.
- **FR-006**: Authorization failures MUST NOT be counted against the circuit-breaker
  / service-health state.
- **FR-007**: A transient failure while minting a fresh credential MUST surface as
  transient; a 4xx while minting MUST surface as permanent.
- **FR-008**: Concurrent callers encountering an authorization failure MUST share a
  single refresh rather than each triggering an independent refresh.
- **FR-009**: A non-authorization failure MUST retain its existing classification and
  MUST NOT trigger a credential refresh.
- **FR-010**: System MUST emit an observability record when a refresh-and-retry is
  attempted, naming the action and its outcome (recovered or failed).
- **FR-011**: No emitted record, log, or error message MAY contain credential, token,
  authorization-header, or response-body content.

### Key Entities

- **Credential provider**: supplies the GitHub credential. Either mints short-lived
  credentials (refreshable) or serves a static credential (non-refreshable). Exposes
  a force-invalidate operation that clears any cached credential (no-op when static).
- **Authorization failure**: a GitHub rejection of the credential, observable in two
  shapes — a transport-level non-2xx response, or a structured response-level
  authorization error.
- **Recovery event**: an observability record describing a refresh-and-retry attempt
  and its outcome, carrying no secret material.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: When a refreshable credential goes stale, board polling recovers within
  the same cycle 100% of the time, with no operator intervention.
- **SC-002**: A single authorization failure triggers at most one refresh and at most
  one retry — never an unbounded loop.
- **SC-003**: A genuinely invalid (static) credential produces exactly one clear
  permanent error and no service-health/circuit-breaker impact.
- **SC-004**: Zero secrets (credential, token, header, or body text) appear in any
  emitted record or log across all recovery paths.
- **SC-005**: Intermittent-authorization-failure dispatch starvation is eliminated —
  the daemon no longer stops dispatching work after a transient credential rejection.
