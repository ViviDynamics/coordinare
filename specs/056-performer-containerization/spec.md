# Feature Specification: Containerized Performer Execution

**Feature Branch**: `056-performer-containerization`
**Created**: 2026-04-28
**Status**: Draft
**Input**: User description: "Containerized performer execution with port-based job protocol"

## Clarifications

### Session 2026-04-28

- Q: How does a containerized performer authenticate that an incoming request is from the coordinare? → A: Per-performer shared bearer token, optional (can be disabled for local/dev deployments).
- Q: How does the coordinare consume in-flight job progress and the final result? → A: Both polling and streaming are required of every conformant performer image.
- Q: What is the default consecutive status-poll failure threshold before a performer is excluded? → A: 5 consecutive failures. Exclusion and recovery events MUST notify operators via existing alerting/observability channels.
- Q: How long does the coordinare wait for a freshly started container to report ready? → A: 120 seconds default, configurable per performer.
- Q: At what granularity does a performer advertise capabilities? → A: Backend identifiers + a fixed enumerated set of tool capability flags. Default flag set covers what current personas (write/doc/test/QA) need: `git`, `node`, `python`, `browser`, `lint`, `format`, `test_runner`, `ripgrep`, `jq`, `shell`.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Operator runs a performer inside a container instructed over a port (Priority: P1)

An operator wants to isolate a performer's execution from the coordinare host. They configure a performer to run as a container, coordinare sends the job over a network port, the performer executes the assigned work against the card's repository, reports completion, and either remains running for the next job or is torn down — depending on the operator's choice.

**Why this priority**: This is the core capability the feature exists to deliver. Without it, performers remain coupled to the coordinare host and cannot be operated independently, audited as discrete units, or moved to remote infrastructure later. Every other story builds on this baseline.

**Independent Test**: Configure a single performer with `mode: ephemeral` pointing at a published image, dispatch a card to it, observe the container start, accept the job over its port, complete the work, push results, and tear down. The card progresses through its lifecycle with no functional difference vs. native subprocess execution.

**Acceptance Scenarios**:

1. **Given** a performer configured with a container image and `mode: ephemeral`, **When** the coordinare dispatches a job, **Then** a fresh container is started, the job is delivered over the configured port, the work completes, and the container is torn down.
2. **Given** a performer configured with `mode: persistent` pointing at a long-running container, **When** the coordinare dispatches a job, **Then** the existing container accepts the job over its port without being recreated.
3. **Given** a performer configured with `mode: subprocess`, **When** the coordinare dispatches a job, **Then** the existing native subprocess execution path is used unchanged.
4. **Given** a performer container is mid-job, **When** another job is dispatched to that same performer, **Then** the performer responds that it is busy and the coordinare defers dispatch instead of overwriting the in-flight job.

---

### User Story 2 - Coordinare selects an available performer from a pool (Priority: P1)

When multiple persistent performers are registered, the coordinare needs to know which one is free before sending work. The coordinare maintains a connection pool that tracks each performer's current status, picks an idle one matching the role's requirements, and falls back to the next candidate when its first choice turns out to be busy.

**Why this priority**: Containerized performers are most useful in a small fleet (e.g. one per role, one per backend, multiple replicas of the same backend). Without availability-aware dispatch, the coordinare will repeatedly hand jobs to busy performers and stall. This is what makes the persistent mode operationally viable.

**Independent Test**: Register two persistent performers of the same role. Dispatch two jobs back-to-back. The first goes to performer A, then while A is busy the second goes to performer B. Dispatch a third while both are busy and confirm the coordinare defers to the next cycle rather than blocking or losing work.

**Acceptance Scenarios**:

1. **Given** two persistent performers of the same role both reporting idle, **When** the coordinare dispatches a job, **Then** exactly one performer receives it and the other remains idle.
2. **Given** the chosen performer reports busy at dispatch time, **When** another candidate of the same role is registered and idle, **Then** the coordinare automatically routes the job to the alternate without operator intervention.
3. **Given** all candidates of a role are busy, **When** the coordinare would dispatch a job, **Then** dispatch is deferred to the next cycle and the operator can see why through existing dashboard/observability surfaces.
4. **Given** a registered performer becomes unreachable, **When** the coordinare polls its status, **Then** the performer is marked unavailable and excluded from dispatch until it recovers.

---

### User Story 3 - Operator chooses an image variant that matches their needs (Priority: P2)

Operators care about image size and what the performer can do out of the box. A "full" image bundles every supported agent CLI plus universal validation and QA tooling so any role can run on it. Lighter images cover one backend each, with the option to opt out of heavy extras like browser binaries. A bare base image lets advanced operators bring their own CLI. Operators may also supply their own image entirely as long as it satisfies the documented contract.

**Why this priority**: The feature must not force every operator to pay the cost of every backend's tooling. Variant flexibility is what makes the feature adoptable across small and large deployments and is required before this is something an operator would pick up.

**Independent Test**: Pick a slim variant for a single backend, configure a performer to use it, run an end-to-end card through it, and confirm the role completes successfully without the missing tools (which it shouldn't have needed). Then swap to the full variant and confirm a QA-style role that needs a browser also completes.

**Acceptance Scenarios**:

1. **Given** the published full image, **When** any of the five supported backends is configured against it, **Then** the corresponding role completes its work without needing additional tooling installs at job time.
2. **Given** a published slim image scoped to one backend, **When** a performer using a different backend is pointed at it, **Then** the misconfiguration is detected and reported clearly rather than failing partway through a job.
3. **Given** an operator-provided custom image that satisfies the documented contract, **When** the coordinare dispatches a job to it, **Then** the job runs successfully without the operator changing coordinare code.
4. **Given** a performer image and a project-specific tool (e.g. an extra language toolchain), **When** the operator volume-mounts the tool into the container, **Then** the performer can invoke that tool during the job.

---

### User Story 4 - Operator provides credentials safely (Priority: P2)

Each backend needs its own credentials and may need additional secrets to interact with the project's repository or services. The operator picks how those secrets reach the container based on their environment: baked into the container as environment variables, mounted from a host directory, or sent over the wire when the coordinare initializes a specific job. When more than one source provides the same secret, the most specific source wins.

**Why this priority**: Without a sane secrets story, operators cannot use this feature in any setting more sensitive than a personal sandbox. Three options cover the operational realities (local dev, shared host, dynamically scoped per-job credentials) without forcing one pattern.

**Independent Test**: Configure the same secret three ways in a single deployment (env, mounted creds, init payload) and confirm the most specific source wins each time. Remove the over-the-wire override and confirm fallback to env. Remove env and confirm fallback to the mounted file.

**Acceptance Scenarios**:

1. **Given** a secret is required by a backend and is provided only as an environment variable on the container, **When** a job is dispatched, **Then** the backend receives the secret and the job proceeds.
2. **Given** the same secret is provided in the job-init payload and as an environment variable, **When** the job runs, **Then** the job-init value is used.
3. **Given** the same secret is provided as an environment variable and as a mounted creds file, **When** the job runs, **Then** the environment variable value is used.
4. **Given** a required secret is provided through none of the supported sources, **When** the job is dispatched, **Then** the performer reports the missing secret to the coordinare and the job is not silently retried until exhaustion.

---

### Edge Cases

- A performer container starts but never finishes initializing: the coordinare must time out the wait, mark the performer unhealthy, and stop sending it jobs until it recovers.
- A persistent performer crashes mid-job: the coordinare must observe the disconnection, mark the job failed via the existing error path, and not double-deliver the job to a different performer without operator-visible state.
- An ephemeral container completes its job but fails to tear down: the operator must be able to identify the orphaned container and the coordinare must not consider it part of the active pool.
- A custom (BYO) image is missing tooling the chosen role needs: the failure must surface as a clear capability mismatch, not as an opaque agent backend error.
- Network instability between coordinare and a persistent performer: status checks and job dispatch must tolerate transient failures without removing the performer from the pool on a single missed poll.
- An operator changes a performer's mode (subprocess → ephemeral, etc.) while jobs are queued: in-flight work must finish under the original mode; new dispatches use the new mode.
- Two operators register two performers at the same address by mistake: the coordinare must detect the conflict and refuse to dispatch ambiguously.

## Requirements *(mandatory)*

### Functional Requirements

**Lifecycle modes**

- **FR-001**: System MUST allow each configured performer to run in one of three modes: native subprocess (existing behavior), ephemeral container (started on demand and torn down on job completion), or persistent container (long-running, reused across jobs).
- **FR-002**: System MUST keep `subprocess` mode as the default so that operators who do not opt in to containers see no change in behavior.
- **FR-003**: System MUST tear down ephemeral containers after their assigned job completes, regardless of whether the job succeeded or failed.
- **FR-004**: System MUST NOT modify or restart persistent containers as part of normal job dispatch.
- **FR-004a**: System MUST wait up to a configurable readiness timeout (default: 120 seconds) for a freshly started container to report ready. If the container fails to report ready within the timeout, the coordinare MUST mark the performer unhealthy, stop dispatching to it, and emit the same exclusion notification as for status-poll failures.

**Job protocol**

- **FR-005**: A containerized performer MUST expose a status surface the coordinare can query to learn whether it is idle, busy, starting up, or draining, along with the identifier of any in-flight job and the capabilities it advertises. Capabilities MUST be expressed as (a) the set of supported backend identifiers (e.g. `claude_code`, `codex`, `junie`, `opencode`) and (b) a set of tool capability flags drawn from a fixed enumeration. The v1 enumeration MUST include at minimum: `git`, `node`, `python`, `browser` (headless browser/Playwright), `lint`, `format`, `test_runner`, `ripgrep`, `jq`, `shell`. The enumeration is extensible in later versions; performers MUST ignore unknown flags rather than fail.
- **FR-006**: A containerized performer MUST accept new job assignments over its port, return a positive acknowledgement with a job identifier when accepting, or return a busy response with a retry hint when it cannot accept.
- **FR-007**: A containerized performer MUST allow the coordinare to cancel an in-flight job and report whether the cancellation was honored.
- **FR-008**: A containerized performer MUST allow the coordinare to retrieve job progress and final result for any job it has accepted via BOTH a polling interface (request returns the latest status/result snapshot) AND a streaming interface (long-lived connection that pushes incremental progress events until the job reaches a terminal state). Both interfaces MUST be implemented by every conformant image; the coordinare chooses which to use per call.

**Coordinare-side dispatch**

- **FR-009**: System MUST maintain a registry of known performer endpoints and their last-known status so dispatch decisions are based on availability, not assumption.
- **FR-010**: System MUST poll a candidate performer's status before dispatching a job and choose only candidates whose advertised capabilities match the role's requirements.
- **FR-011**: System MUST handle a busy response by trying the next eligible candidate, and defer dispatch to the next cycle when no candidate is available, instead of blocking or queuing locally without operator visibility.
- **FR-012**: System MUST mark a performer unavailable when its endpoint is unreachable for a configurable consecutive-failure threshold (default: 5 consecutive failed status checks), and exclude it from dispatch until it recovers. Both the exclusion event and the subsequent recovery event MUST be emitted to the existing alerting/notification channels (notification service, dashboard, logs, metrics) so operators are notified without polling the dashboard themselves.
- **FR-013**: System MUST surface deferred dispatches and unavailable performers through the existing operational visibility surfaces (dashboard, logs, metrics) without requiring a new dashboard area.

**Image variants**

- **FR-014**: System MUST publish an image variant ("full") that includes every supported agent backend plus the validation tooling (lint, format, test runners, browser binaries for QA) operators need to run any role end to end.
- **FR-015**: System MUST publish lightweight image variants scoped to a single agent backend, omitting tooling that backend does not require.
- **FR-016**: System MUST publish a base image containing only universal toolchains (no agent CLIs) so operators can install their own CLI on top.
- **FR-017**: System MUST document a contract that any container image must satisfy to be used as a performer (entrypoint behavior, port discovery, status protocol, expected pre-installed tools), so operators can supply their own image.
- **FR-018**: System MUST allow operators to mount additional project-specific tools or files into the performer container at run time without modifying the image.

**Secrets handling**

- **FR-019**: System MUST support providing secrets to a containerized performer through any combination of three sources: environment variables on the container, a mounted creds file or directory, and values sent in the job-initialization payload.
- **FR-020**: System MUST resolve conflicts among the three secret sources by deterministic precedence: job-init payload overrides environment variables, environment variables override mounted creds files.
- **FR-021**: System MUST allow operators to disable any of the three sources per performer, so a deployment can opt out of (for example) over-the-wire credentials.
- **FR-022**: System MUST detect when a required secret is missing from all enabled sources and fail the job with an actionable error rather than letting the backend produce an opaque failure.
- **FR-023**: System MUST NOT log secret values, regardless of source, including when reporting errors about missing secrets.
- **FR-023a**: System MUST support a per-performer shared bearer token that the coordinare presents on every request to the performer's port; the performer MUST reject requests with a missing or mismatched token. Token authentication MUST be disable-able per performer registration to support local/dev deployments, and the disabled state MUST be visible to the operator through existing observability surfaces.

**Compatibility and rollout**

- **FR-024**: System MUST allow native subprocess performers and containerized performers to coexist in the same deployment so operators can migrate one role at a time.
- **FR-025**: System MUST treat capability mismatches (e.g. role requires browser tooling but the chosen image variant lacks it) as a configuration error reported up front, not a runtime failure mid-job.
- **FR-026**: System MUST preserve existing performer state, history, and observability semantics so containerization is invisible to consumers of those surfaces beyond the underlying execution environment.

### Key Entities

- **Performer image variant**: A published or operator-provided container image that satisfies the performer contract. Carries metadata about which backends and tools it supports.
- **Performer registration**: An operator configuration entry describing one performer: its mode (subprocess/ephemeral/persistent), its image (if containerized), the endpoint at which it can be reached (if persistent), the role(s) it serves, and its secret-source configuration.
- **Job**: A unit of work the coordinare sends to a performer. Carries the card context, role, and any secrets supplied via the job-init payload. Has a lifecycle: accepted, running, completed (success or failure), cancelled.
- **Performer status**: A snapshot of one performer's current state — whether it is idle, busy, starting, or draining; the in-flight job (if any); the capabilities it advertises; the timestamp of the last successful status check.
- **Performer pool**: The coordinare's view across all registered performers — which are eligible for which roles, which are reachable, which are currently free, and which have been excluded due to repeated failures.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can convert one role from subprocess to ephemeral-container execution by changing configuration only (no code changes) and observe the same card outcomes as before.
- **SC-002**: With two persistent performers of the same role registered, two cards dispatched back-to-back are handled in parallel — one per performer — instead of being serialized.
- **SC-003**: When all eligible performers are busy, no card is silently lost; every deferred dispatch is visible to the operator within one polling cycle of the deferral.
- **SC-004**: A performer that becomes unreachable is excluded from dispatch within the operator-configured consecutive-failure threshold, and is automatically re-included on its first successful status check after recovery.
- **SC-005**: An operator can switch from the full image to a slim image (or vice versa) per performer through configuration alone, with no rebuild of the coordinare.
- **SC-006**: Capability mismatches (image missing a required backend or tool) are reported before any card is dispatched to that performer.
- **SC-007**: A required secret missing from every enabled source produces an actionable error referencing the secret name within the same cycle the job would have run, with no secret values written to logs.
- **SC-008**: Existing native subprocess performers continue to operate unchanged in a deployment that also has containerized performers, with no regression in their card throughput.

## Assumptions

- The deployment can run Docker locally on the coordinare host (or reach a Docker-compatible runtime over the network) for the duration of v1; orchestrators like Kubernetes are explicitly deferred.
- Performer-to-coordinare communication for status and job dispatch happens over the network using HTTP-style request/response. Logs and progress streaming may use a streaming variant of the same transport.
- The coordinare is the trust anchor for issuing job-init payloads; secrets sent over the wire are protected by the same mechanisms that protect existing coordinare → performer communication.
- Operators are responsible for the security of any creds files they mount and for the secrets they place in container environments; the system enforces precedence and non-logging but does not manage external secret stores.
- Project-specific tooling that performers need beyond the universal validation set is delivered via volume mount, not by extending base images.

## Out of Scope (v1)

- Kubernetes manifests, Helm charts, and any K8s-specific compatibility work. Verification targets Docker and native performers in v1.
- A central image registry, signing, or publishing pipeline. Image build is a project artifact; distribution is the operator's responsibility in v1.
- Multi-architecture image builds.
- A dashboard area dedicated to the performer pool. Pool state surfaces through existing dashboard/log/metric channels in v1.
- Automated migration tooling for converting an existing subprocess performer to a containerized one. Migration is a config-edit task in v1.
