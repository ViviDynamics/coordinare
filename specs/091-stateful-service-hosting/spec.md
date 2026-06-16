# Feature Specification: Stateful Service Hosting in the QA Env-Cache

**Feature Branch**: `091-stateful-service-hosting`  
**Created**: 2026-06-15  
**Status**: Draft  
**Input**: User description: "Stateful service hosting in the QA env-cache (Postgres, Redis, and other persistent stores)."

## Overview

DB-backed QA tests currently fail with "Connection refused" because the env-cache pipeline cannot host a stateful database inside the performer container. The performer image is deliberately **agnostic** — it bakes in no postgres/redis — and that is correct and must stay that way. The fix is to make the env-cache the carrier of whatever persistent-storage dependencies a project needs: the env-bootstrap stage installs the service binaries into the cache, and the services-start pipeline brings those services up — including the first-run initialization a stateful store requires (e.g. `initdb`, admin-role and database creation) — as their own background processes listening on the connection target the project expects.

Reliability is paramount: a durable declaration (operator-authored or deterministically derived) must be the trusted path, not flaky LLM service inference. This is consistent with the project's "durable contracts over forgetful/flaky models" principle.

**Scope boundary**: This feature is the coordinare-side capability — manifest derivation for system services, the service-declaration init step, the services-start init phase, and bootstrap-persona install instructions for service dependencies. Aligning a specific consuming repository's own config (e.g. a website's `.coordinare/score.json` or `config/database.yml`) to the provided connection target is a separate downstream change and is explicitly **out of scope** here.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Stateful service is initialized and reachable before QA runs (Priority: P1)

A QA run for a project that depends on a stateful store (e.g. Postgres) starts the service inside the performer container. On the very first run against an empty storage area the env-cache performs the required first-run initialization — creating the storage area, the expected admin account, and the expected database — then starts the service listening on the host and port the project's tests connect to. DB-backed tests connect successfully instead of failing with "Connection refused".

**Why this priority**: This is the entire point of the feature. Without idempotent first-run initialization a stateful store cannot start at all, so every DB-backed QA run fails. Delivering only this story already turns a hard failure into a passing connection.

**Independent Test**: Declare a stateful service with an init step in a durable manifest, run the services-start pipeline against an empty cache, and confirm the service accepts a connection on the expected host/port and that the expected database and admin account exist.

**Acceptance Scenarios**:

1. **Given** a project declares a stateful service with a first-run init step and an empty storage area, **When** the services-start pipeline runs, **Then** the storage area is initialized, the expected admin account and database are created, and the service accepts connections on the declared host/port.
2. **Given** a stateful service whose storage area was already initialized on a prior run, **When** the services-start pipeline runs again, **Then** initialization is skipped (no re-init, no data loss) and the service starts and accepts connections.
3. **Given** a stateful service whose initialization step fails, **When** the services-start pipeline runs, **Then** the failure is reported with the reason and attributed to the environment (not to the project's code under test).
4. **Given** a stateful service that was started for a QA run, **When** the run completes or is torn down, **Then** the service's background process is stopped.

---

### User Story 2 - Env-bootstrap installs persistent-storage dependencies into the cache (Priority: P2)

When a project needs a persistent store, the env-bootstrap stage is told to install that store's binaries into the env-cache (the same delivery mechanism already used for language runtimes and other dependencies). The performer image itself remains free of any baked-in database, so the image stays generic across every project.

**Why this priority**: Story 1 can be demonstrated on a cache that already happens to contain the binary, but for the capability to work end-to-end on a fresh project the bootstrap stage must know to fetch the service dependency. This makes the feature self-contained rather than relying on a pre-seeded cache.

**Independent Test**: Declare a project that needs a stateful service, run env-bootstrap, and confirm the service binary is present in the env-cache while the base performer image contains no such binary.

**Acceptance Scenarios**:

1. **Given** a project declares a need for a stateful service, **When** env-bootstrap runs, **Then** the service's binaries are installed into the env-cache.
2. **Given** the base performer image, **When** it is inspected for the service binary, **Then** the binary is absent (the image stays agnostic).
3. **Given** a project that needs no stateful service, **When** env-bootstrap runs, **Then** no service binaries are installed and behavior is unchanged from today.

---

### User Story 3 - Durable declaration is trusted over flaky inference (Priority: P3)

When a durable service declaration exists (operator-authored or deterministically derived), the pipeline uses it verbatim instead of relying on LLM-based service inference. A project that previously got an empty `services: []` from flaky inference now hosts its declared services reliably, every run.

**Why this priority**: Stories 1 and 2 provide the mechanism; this story guarantees the mechanism is actually reached on real projects where inference has been unreliable. It is the reliability guarantee that makes the feature trustworthy in production.

**Independent Test**: Provide a durable declaration for a project and confirm the declared services are hosted on every run regardless of what LLM inference would have produced, including when inference would have returned no services.

**Acceptance Scenarios**:

1. **Given** a durable service declaration exists for a project, **When** the services pipeline runs, **Then** the declared services are used verbatim and inference is not consulted for them.
2. **Given** flaky inference that would return no services, **When** a durable declaration is present, **Then** the declared services are still hosted.
3. **Given** a hosted service that fails to initialize or start, **When** QA reports the outcome, **Then** the failure is attributed to the environment rather than to the project's code under test.

---

### Edge Cases

- **Port already bound**: the declared host/port is already in use when the service tries to start — the pipeline must report a clear, environment-attributed error rather than hang.
- **Partial prior initialization**: a previous run created the storage area but failed before creating the admin account or database — the init step must converge to a fully-initialized state or report a clear failure, not silently start a half-initialized service.
- **Concurrent start race**: two start attempts for the same service must not both run initialization or both bind the port; one must win and the other must no-op or wait.
- **Starts but never becomes reachable**: the process launches but never accepts connections within the readiness window — the pipeline must time out with an environment-attributed error.
- **Credentials/secrets**: any admin password or connection secret used during init must not be written to logs or observability output.
- **Stateless services unchanged**: services that need no init (e.g. a store that starts cleanly on an empty area) must continue to work exactly as today, with no init step required.
- **Read-only cache mount**: when the env-cache is mounted read-only, the service's mutable storage area must live in a writable location outside the read-only mount.
- **Connection-target mismatch**: the project expects a host/port/account that differs from a service default — the declared connection target must drive what is initialized and bound, with no reliance on the binary's defaults.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST support declaring a first-run initialization step for a stateful service (e.g. storage-area creation, admin-account creation, database creation) as part of the service declaration.
- **FR-002**: The system MUST allow a service declaration to specify the connection target (port, admin account, database) the service must be reachable on, and MUST host the service on that target rather than relying on the binary's defaults. The bind host is fixed to loopback (`127.0.0.1`/`localhost`), consistent with the single-host single-process assumption, and is not a declarable field.
- **FR-003**: The initialization step MUST be idempotent: running it against an already-initialized storage area MUST NOT re-initialize, lose data, or fail.
- **FR-004**: The system MUST verify that a started stateful service is actually reachable on its connection target within a bounded readiness window before declaring it ready.
- **FR-005**: When initialization, start, or readiness verification fails, the system MUST report the failure with a reason and MUST attribute it to the environment, not to the project's code under test.
- **FR-006**: The env-bootstrap stage MUST be able to install a project's persistent-storage service binaries into the env-cache using the existing dependency-delivery mechanism.
- **FR-007**: The performer base image MUST remain free of baked-in stateful-service binaries; all such binaries MUST be delivered via the env-cache.
- **FR-008**: When a durable service declaration exists for a project, the system MUST use it verbatim and MUST NOT depend on LLM service inference for those services.
- **FR-009**: The system MUST place a service's mutable storage area in a writable location even when the env-cache itself is mounted read-only.
- **FR-010**: The system MUST guard initialization and start against concurrent attempts for the same service so that initialization runs at most once and the port is bound at most once.
- **FR-011**: The system MUST NOT write service credentials or connection secrets to logs or observability output.
- **FR-012**: The system MUST stop a hosted service's background process(es) when the QA run completes or is torn down.
- **FR-013**: Services that require no initialization MUST continue to start and run unchanged, with no init step required of them.
- **FR-014**: The service declaration MUST be expressive enough to cover multiple kinds of stateful store (at minimum Postgres and Redis) without special-casing a single product in the declaration contract.

### Key Entities *(include if feature involves data)*

- **Service declaration**: a durable, trusted description of a service the env-cache must host — its kind, the binary/dependency to install, the connection target, whether it requires initialization, and how it is started and stopped.
- **Initialization recipe**: the idempotent first-run setup for a stateful service (storage-area creation, admin account, database) keyed by service kind, parameterized by the declaration's connection target.
- **Service runtime state**: whether a service is initialized, running, and reachable on its connection target — used to make start/init idempotent and to drive readiness and teardown.
- **Connection target**: the port, admin account, and database the project's tests expect to connect to on loopback — the contract the hosted service must satisfy.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: DB-backed QA runs for a project with a declared stateful store connect successfully — zero "Connection refused" failures attributable to the store not being hosted.
- **SC-002**: Running the services-start pipeline repeatedly against the same cache yields exactly one initialized, running instance of each declared service (no duplicate init, no duplicate bind, no data loss).
- **SC-003**: 100% of init/start/readiness failures are surfaced as environment-attributed failures rather than being misattributed to the project's code under test.
- **SC-004**: The declared service binary is present in the env-cache on 100% of runs that need it, while present in the base performer image on 0% of inspections.
- **SC-005**: When a durable declaration exists, the declared services are hosted on 100% of runs regardless of what LLM inference would have produced.
- **SC-006**: A hosted stateful service becomes reachable on its connection target within the configured readiness window, or fails with a clear environment-attributed timeout.

## Out of Scope

- Aligning a specific consuming repository's own configuration (e.g. a website's `.coordinare/score.json` or `config/database.yml`) to the provided connection target — that is a separate downstream config change.
- Hosting services that are inherently external/cloud-only; those remain external and are not brought up inside the performer container.
- Performance tuning of hosted services (connection pooling, resource limits, replication); the goal here is correct, reachable, reliably-initialized hosting.

## Assumptions

- The coordinare owns the deterministic initialization logic keyed by service kind; the declaration supplies the parameters (connection target, database name, admin account), and the coordinare does not parse arbitrary project config to discover them.
- The durable declaration carries the connection target, so no arbitrary project config parsing is required to know where the service must listen.
- The existing env-cache readiness/wait budget is the bound used for readiness verification.
- The existing env-cache dependency-delivery mechanism (used for language runtimes and downloaded packages) is reused to deliver service binaries.
- The deployment remains single-host, single-process, consistent with the current state model.
