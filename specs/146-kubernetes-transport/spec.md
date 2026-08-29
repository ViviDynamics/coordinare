# Feature Specification: Kubernetes Transport

**Feature Branch**: `146-kubernetes-transport`
**Created**: 2026-08-28
**Status**: Draft
**Issue**: [#200](https://github.com/ViviDynamics/coordinare/issues/200)

## Clarifications

### Session 2026-08-28

- Q: Which clusters must this support? → A: **Any conformant cluster** — EKS, vanilla Kubernetes, microk8s, minikube, k3s. This is the governing constraint: standard core APIs only, no CRDs, and no assumption that a StorageClass exists or that the CNI enforces NetworkPolicy.
- Q: Is egress allowlisting in scope? → A: **No, not in v1.** The Docker path uses in-container iptables via `NET_ADMIN`. The Kubernetes-native answer is a NetworkPolicy, but that requires a CNI which enforces it, and minikube, microk8s and kind defaults often do not. Requiring one would break portability; shipping one that silently does not enforce would be worse than shipping nothing. Egress control is documented as Docker-only.
- Q: Does spec 147 (Helm) come with this? → A: No. The transport is independently testable and Helm packages something that must exist first.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An operator runs coordinare without handing it the host (Priority: P1)

Someone running coordinare today must mount `/var/run/docker.sock` into the daemon, which is
root-equivalent access to the host. On a cluster they should be able to run it with a
namespace-scoped ServiceAccount and nothing else.

**Why this priority**: This is the entire point. The `docker.sock` mount is the largest single
trust liability in the system, documented as such in the threat model, and it is the reason
coordinare cannot be run on infrastructure anyone else owns.

**Independent Test**: With `agent_transport: kubernetes`, a card is dispatched to a performer
Pod and completes, on a cluster where the daemon has only a namespace-scoped Role and no access
to a Docker socket.

**Acceptance Scenarios**:

1. **Given** `agent_transport: kubernetes`, **When** a card is dispatched, **Then** a performer
   Pod is created, does the work, and is removed, with no Docker socket involved anywhere.
2. **Given** the shipped RBAC manifest, **When** the daemon runs under exactly those permissions,
   **Then** every operation it needs succeeds and nothing beyond that namespace is reachable.
3. **Given** a performer Pod that fails (eviction, OOM, node loss), **When** the daemon observes
   it, **Then** the failure classifies through the existing error handling rather than
   introducing a new terminal state.

---

### User Story 2 - It runs on whatever cluster the operator already has (Priority: P1)

An operator on EKS, microk8s, minikube, k3s, or vanilla Kubernetes should be able to use this
without their cluster being special.

**Why this priority**: Equal to US1 and constrains its implementation. A transport that only
works on one distribution serves almost nobody, and the failure mode is discovered late and
expensively.

**Independent Test**: The manifests and the transport use only core APIs; nothing fails on a
cluster with no StorageClass, no NetworkPolicy enforcement, no LoadBalancer, and no vendor
extensions.

**Acceptance Scenarios**:

1. **Given** any conformant cluster, **When** the transport runs, **Then** it uses only stable
   core APIs and no custom resources.
2. **Given** a cluster with **no** usable StorageClass, **When** a performer starts, **Then** it
   runs with a cold cache rather than failing.
3. **Given** a cluster whose CNI does not enforce NetworkPolicy, **When** the transport runs,
   **Then** nothing silently claims to restrict egress.

---

### User Story 3 - A maintainer can add another runtime without surgery (Priority: P2)

The code that decides *how a performer is started* is currently the same code that starts it
with Docker. Adding Kubernetes should introduce a seam rather than a second hard-wired branch.

**Why this priority**: Below the working transport, but it determines whether this feature is a
foundation or a fork in the road. Two hard-wired runtimes is the state from which a third is
never added.

**Independent Test**: `http_performer_service` depends on a lifecycle abstraction; both the
Docker and Kubernetes implementations satisfy it; neither is referenced by name from the
orchestration path.

**Acceptance Scenarios**:

1. **Given** the orchestration path, **When** it acquires or releases a performer, **Then** it
   calls through the abstraction, not a runtime-specific module.
2. **Given** the Docker implementation, **When** this feature lands, **Then** its behaviour is
   unchanged and its existing tests still pass.

---

### Edge Cases

- **A Pod is not a Job.** `performer_lifecycle.stop()` terminates a *running server*; the
  performer never exits on its own. Under a Job the container would never complete, the
  termination would read as failure, and backoff would restart it — spawning a second performer
  for work the daemon believes it has ended.
- **Logs vanish with the Pod.** The Docker path captures `docker logs` before removal. The
  Kubernetes equivalent must read `pods/log` *before* delete, not after.
- **The performer image is 4 GB, and that is not a defect.** A performer is a developer
  workstation; Playwright and Chromium account for 1.5 GB of it, and a role-specific slim variant
  would hand an implementer a machine that cannot check rendered output. The pull is **once per
  node**, not once per performer, so it is a cold-start cost on first schedule rather than a
  recurring tax. Document it; do not engineer around it by fragmenting the image.
- **A cluster may have no default StorageClass.** Requesting a PVC unconditionally would fail on
  exactly the small clusters this is meant to support.
- **Egress allowlisting has no portable equivalent.** Silently not enforcing it would be worse
  than not offering it.
- **Pod names have stricter rules than container names.** RFC 1123 labels, 63 characters,
  lowercase alphanumeric and hyphens. Existing performer identifiers may not satisfy this.
- **The daemon may itself run outside the cluster.** During development it runs on a laptop
  against a remote cluster, so reaching a performer by Pod IP may not work; that path needs to
  be understood rather than assumed.

## Requirements *(mandatory)*

### Functional Requirements

**The lifecycle seam**

- **FR-001**: A runtime-agnostic performer lifecycle abstraction MUST exist, covering acquiring a
  performer, waiting for readiness, capturing its logs, and releasing it.
- **FR-002**: The existing Docker behaviour MUST be moved behind that abstraction with no change
  in behaviour, evidenced by its existing tests continuing to pass unmodified.
- **FR-003**: `http_performer_service` MUST depend on the abstraction rather than importing a
  runtime-specific module.
- **FR-004**: The misleading `KubernetesTransport` stub in `transport/` MUST be resolved. The
  `AgentTransport` protocol is the subprocess wire protocol and is not the seam Kubernetes needs;
  leaving a stub there that appears to be the extension point is worse than having none.

**The Kubernetes implementation**

- **FR-005**: A Kubernetes lifecycle implementation MUST create a performer as a **bare Pod**,
  not a Job, for the reasons in Edge Cases.
- **FR-006**: It MUST reuse the existing performer HTTP contract unchanged. The performer image
  requires no modification; this is verified.
- **FR-007**: It MUST capture Pod logs before deletion.
- **FR-008**: It MUST delete the Pod when the performer is released, and Pods MUST NOT outlive
  the daemon that created them.
- **FR-009**: Pod failures (eviction, OOM, node loss) MUST classify through existing error
  handling with no new terminal states.
- **FR-010**: Performer identifiers MUST be converted to valid RFC 1123 Pod names, and the
  mapping MUST remain traceable back to the performer.

**Portability**

- **FR-011**: Only stable core Kubernetes APIs MUST be used. No custom resources, no vendor
  extensions.
- **FR-012**: Persistent caching MUST be optional. With no usable StorageClass the performer MUST
  run with a cold cache rather than failing.
- **FR-013**: Image pull policy and pull secrets MUST be configurable, defaulting to values that
  work on a cluster which must pull from a registry. The default MUST be `IfNotPresent` so a node
  reuses its image cache rather than re-pulling per performer.
- **FR-013a**: The cold-start cost MUST be documented honestly rather than engineered around. The
  performer image is ~4 GB because **a performer is a developer workstation** — Playwright and
  Chromium alone are 1.5 GB, and an implementer that cannot open a browser is missing a capability
  at the moment it needs it. The pull happens **once per node**, not once per performer: the first
  performer scheduled on a node waits for it, every subsequent one starts from cache. A pre-pull
  DaemonSet MUST be documented as the mitigation for operators who care about that first dispatch.
- **FR-014**: The transport MUST NOT claim to restrict egress. Egress allowlisting is documented
  as unavailable under Kubernetes in this version.

**Permissions**

- **FR-015**: An RBAC manifest MUST ship granting only namespace-scoped `pods` (create, get,
  list, watch, delete) and `pods/log` (get).
- **FR-016**: The transport MUST work under exactly those permissions, verified rather than
  asserted.
- **FR-017**: No cluster-scoped permission and no access to secrets MUST be required.

**Documentation**

- **FR-018**: Running on Kubernetes MUST be documented, including the RBAC manifest, image
  distribution, the cold-cache fallback, and the absence of egress control.
- **FR-019**: The threat model MUST be updated: the `docker.sock` trust decision does not apply
  to the Kubernetes path, and that path's own boundaries must be stated.

## Success Criteria *(mandatory)*

- **SC-001**: A card is dispatched to a performer Pod and completes end to end on a local cluster,
  with no Docker socket involved.
- **SC-002**: The daemon operates under the shipped RBAC and nothing more, verified by test.
- **SC-003**: Every existing Docker-path test passes unmodified.
- **SC-004**: The transport starts successfully on a cluster with no StorageClass.
- **SC-005**: Pod logs are captured for a failed performer.
- **SC-006**: A Pod failure produces an existing error classification, not a new state.
- **SC-007**: No CRD, vendor extension, or cluster-scoped permission is required.

## Assumptions

- The daemon runs **in-cluster** for the supported path. Running it outside against a remote
  cluster is a development convenience whose networking is documented, not guaranteed.
- Performer images are pulled from a registry. Local image loading is a development convenience.
- The spike verified on kind v0.33.0 / Kubernetes v1.37.0 that the performer image runs unmodified
  as a Pod, serves `/status` over HTTP, is reachable across pod networking, deletes in about a
  second, and needs neither `docker.sock` nor any added capability.
- Coordinare is itself the controller. No operator or CRD is introduced.

## Out of Scope

- Helm packaging (spec 147, issue #201).
- Operators or custom resources.
- Moving coordinare state off single-host JSON.
- Egress allowlisting under Kubernetes.
- Reducing the performer image size. Considered and **rejected**: performers are developer
  workstations, and splitting the image per role trades a one-time per-node pull for a capability
  gap that surfaces at the worst possible moment.
