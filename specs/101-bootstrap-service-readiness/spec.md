# Feature Specification: Env-Bootstrap Service-Readiness Completion Gate

**Feature Branch**: `101-bootstrap-service-readiness`  
**Created**: 2026-06-22  
**Status**: Draft  
**Input**: User description: "The bootstrap should make sure the declared services are running successfully and connectable before considering itself complete."

## Overview

A symphony's environment cache is built once by an **env-bootstrap** step and then reused to run every card (implement / review / qa / documenting). Some symphonies declare **stateful services** the tests depend on — most importantly a **database**. Today the bootstrap considers itself **complete once the toolchain is installed**; it does **not** verify that the declared services are actually running and reachable. So a cache can be marked "ready" while a required service is entirely absent.

This bit hard (2026-06-22, website cards): the website needs PostgreSQL for its Rails feature/QA tests, but on the live cache the database was never usable — the server package wasn't installed (only the client + a contents-free meta-package), the services setup step was **rejected** (it couldn't resolve the server binary, and another service wasn't on PATH), so **no database cluster was created and nothing was started**. The bootstrap **still reported success**, the cache was marked current, and cards were dispatched into it. Every database-backed test then failed with "connection refused" — across *all* features, not just the card's change — so the agent misread a broken environment as its own bug, thrashed for hours, and even weakened the shared test setup trying to make an unreachable database "pass." The coordinare had no signal anything was wrong because the bootstrap claimed it was done.

This feature makes **service readiness part of "bootstrap complete."** Before a cache is considered ready, the bootstrap must confirm each declared service is installed, initialized, started, and **actually connectable**. If a required service can't be brought up and connected to, the bootstrap is **not complete** — the cache is marked not-ready and the failure is surfaced as an operator-actionable environment condition, so cards are never dispatched into a structurally broken environment.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A bootstrap with an unreachable required service does not report success (Priority: P1)

When a symphony declares a required service (e.g. a database) and the bootstrap cannot make it running-and-connectable — the server software isn't installed, the service setup was rejected, initialization or start failed, or a connection attempt fails — the bootstrap must **fail/incomplete**, not report a ready cache.

**Why this priority**: This is the exact live failure — a cache was marked ready with no usable database, and dozens of downstream test runs (and hours of agent thrashing) followed. Catching it at the bootstrap is the whole point and the MVP.

**Independent Test**: Run a bootstrap for a symphony whose required database cannot be stood up (missing server / rejected setup); verify the bootstrap reports **not complete** with the specific failing service + reason, and the cache is **not** marked ready.

**Acceptance Scenarios**:

1. **Given** a symphony declares a required service, **When** the bootstrap cannot make it connectable, **Then** the bootstrap reports incomplete/failed, names the failing service and the reason, and the cache is not marked ready.
2. **Given** the same, **When** the coordinare evaluates the cache, **Then** it treats it as an environment/infra condition (operator-actionable) and does **not** dispatch cards into it.
3. **Given** a rejected/empty service-setup result for a symphony that requires services, **When** the bootstrap finishes its install phase, **Then** that rejection is treated as a service-readiness failure (not silently ignored), with the specific validation reason surfaced.

---

### User Story 2 - A bootstrap with all required services connectable completes normally (Priority: P1)

When every declared required service is installed, initialized, started, and a real connection succeeds, the bootstrap completes and the cache is marked ready — and a symphony that declares **no** services behaves exactly as before (no new gate, no added failure surface).

**Why this priority**: The gate must not regress the working path. A green bootstrap must still be green; service-less symphonies must be unaffected. Same priority as US1.

**Independent Test**: Bootstrap a symphony with a connectable database → completes ready. Bootstrap a service-less symphony → completes ready exactly as today.

**Acceptance Scenarios**:

1. **Given** all required services connect successfully, **When** the bootstrap finishes, **Then** it reports complete and the cache is marked ready/current.
2. **Given** a symphony declaring no services, **When** it bootstraps, **Then** behavior is identical to before this feature.

---

### User Story 3 - The service-readiness outcome is observable and operator-actionable (Priority: P2)

The bootstrap records, per declared service, whether it was installed / initialized / started / connectable, and on failure surfaces a clear, secret-free reason an operator can act on (e.g. "postgres: server package missing", "service setup rejected: binary unresolvable") — without ever logging secret values.

**Why this priority**: Distinguishes "the environment is broken, operator must act" from "the card is bad," and makes the failure debuggable instead of opaque. Rides on US1.

**Independent Test**: Trigger a required-service failure; verify a per-service readiness record with status + reason exists, names the service, and contains no secret values (no DB passwords) or raw output.

**Acceptance Scenarios**:

1. **Given** a required service fails readiness, **When** the bootstrap surfaces it, **Then** the record/notification names the service and a concrete cause + suggested action, with no secret values.
2. **Given** any readiness check, **When** it records its outcome, **Then** the record carries only service name / status / reason / shape — never tokens, passwords, or raw output.

---

### Edge Cases

- **No declared services**: no readiness gate; bootstrap unchanged (opt-in by declaration).
- **Optional vs required service**: a required service failing readiness blocks the bootstrap; an explicitly optional/best-effort service may warn-and-continue.
- **Rejected/empty service setup** for a required-services symphony: treated as a readiness failure with the validation reason surfaced (not silently completed).
- **Service installed but not started, or started but not connectable**: both are failures — "installed" alone is not "ready"; readiness requires a successful connection/health check.
- **Transient connection flake**: the connect check should be bounded/retried briefly before declaring failure, so a slow-to-accept service isn't falsely failed.
- **Re-bootstrap**: a fresh bootstrap that now makes services connectable produces a ready cache; existing broken caches are not retroactively repaired (re-bootstrap fixes them).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: For a symphony that declares services, the env-bootstrap MUST, before reporting the cache complete/ready, verify each declared service is installed (binary resolvable), initialized (e.g. a data cluster exists), started/running, and **connectable** via a real connect/health check — not merely that install packages were downloaded.
- **FR-002**: If any **required** declared service cannot be made running-and-connectable, the bootstrap MUST report incomplete/failed and MUST NOT mark the cache ready; the specific failing service and reason MUST be captured.
- **FR-003**: A **rejected or empty** service-setup result for a symphony that requires services MUST be treated as a service-readiness failure (bootstrap incomplete), with the underlying validation reason surfaced — not silently ignored while the cache is marked complete.
- **FR-004**: Required vs optional services MUST be distinguishable: a required service failing readiness blocks the bootstrap; an optional/best-effort service that fails MAY warn-and-continue without blocking.
- **FR-005**: A cache whose required services are not connectable MUST be treated by the coordinare as **not current/dispatchable** (integrating with the existing env-cache readiness/dispatch gate and persisted bootstrap-success state), and surfaced as an operator-actionable environment/infra condition — so cards are not dispatched into it.
- **FR-006**: The connect/health check MUST be bounded (timeout + brief bounded retry) so a slow-to-accept service is not falsely failed, and a genuinely-down service surfaces within a bounded time rather than hanging the bootstrap.
- **FR-007**: A symphony that declares **no** services MUST be unaffected (no new gate, identical behavior); the gate is driven entirely by the service declarations.
- **FR-008**: Service-readiness records, logs, and notifications MUST carry only service names / status / reasons / shapes — never secret values (e.g. database passwords) or raw output (carried invariant from 088/090/091/095).
- **FR-009**: No new external dependency; the single-host single-process snapshot state model is unchanged (any per-service readiness added to the existing persisted bootstrap state is backward-compatible). Service declarations / manifest remain config surfaces.

### Key Entities *(include if feature involves data)*

- **Declared service**: a service a symphony's environment requires (name, required-vs-optional, how to verify it's connectable) — the unit the readiness gate checks.
- **Service-readiness result**: per declared service, the outcome of install / initialize / start / connect, plus a secret-free reason on failure — drives bootstrap completeness and the operator signal.
- **Bootstrap completeness / cache-ready state**: the existing persisted bootstrap-success / cache-current signal, now also gated on required-service readiness.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A bootstrap whose required service is not connectable reports **not ready** in 100% of cases (0% of such caches are marked current) — the live failure (a ready cache with an unusable database) cannot recur.
- **SC-002**: 0 cards are dispatched into a cache whose required services are not connectable; instead the condition is surfaced as operator-actionable.
- **SC-003**: A bootstrap with all required services connectable, and any service-less symphony, completes ready exactly as before — no regression on the working path.
- **SC-004**: 100% of required-service readiness failures (including a rejected/empty service setup) produce a named, reasoned, operator-actionable record; 0% of those records contain secret values or raw output.
- **SC-005**: The readiness check is bounded — a down service is surfaced within a bounded time and never hangs the bootstrap; a slow-but-healthy service is not falsely failed.

## Assumptions

- The symphony already has a way to declare its services (the stateful-service manifest / service set from prior env-cache work); this feature adds the readiness verification + gate, not the declaration mechanism.
- The coordinare already has an env-cache readiness/dispatch gate and persisted bootstrap-success state (prior specs); this feature feeds required-service readiness into them.
- The coordinare already distinguishes environment/infra conditions from card failures (the ENV_BLOCKED line); the "required service not connectable" condition reuses that operator surface.
- A meaningful connect check exists per service kind (e.g. a database accepts a connection / readiness probe); the exact probe per service kind is an implementation detail of the readiness step.
- "Required" is the default for a declared service unless explicitly marked optional.

## Dependencies

- Spec 091 (stateful-service hosting: the service manifest + start/stop/health scripts) — the declarations + start/health surface this verifies.
- Spec 093 (env-cache toolchain-readiness / dispatch gate) — the gate this extends from "toolchain ready" to "toolchain **and required services** ready".
- Spec 088 (bootstrap-success integrity / persisted bootstrap state) — the completeness signal this gates on service readiness.
- Spec 095 (ENV_BLOCKED) — the operator-actionable env/infra surfacing reused for "required service not connectable".

## Out of Scope

- The specific website package selection (fetching the database **server** package vs a contents-free meta-package) — a manifest/install-recipe data fix this gate will *expose* by failing the bootstrap, but the gate itself is the deliverable, not the website's package list.
- Choosing which services a given symphony needs (operator/manifest config).
- Installing or provisioning specific service software, or the performer base image contents.
- Changing the service-manifest schema beyond what's needed to treat a rejected manifest as a readiness failure.
- The coordinare-side normalizer/shim work (specs 098/100).
- Retroactively repairing existing broken caches — a re-bootstrap produces a correctly-verified cache.
