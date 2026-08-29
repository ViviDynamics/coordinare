# Feature Specification: Helm Deployment — coordinare-controller In-Cluster

**Feature Branch**: `147-helm-deployment`
**Created**: 2026-08-29
**Status**: Draft
**Issue**: [#201](https://github.com/ViviDynamics/coordinare/issues/201) — Phase B of coordinare-on-Kubernetes
**Depends on**: spec 146 (#200, merged 7456429) — the Kubernetes runtime that starts performers as Pods

## Overview

Spec 146 taught coordinare to run performers as Kubernetes Pods. It did not answer how coordinare
itself gets into the cluster. Today an operator who wants coordinare on Kubernetes has to write
their own StatefulSet, wire their own state volume, and build their own daemon image, because no
published one exists. Every one of those is a chance to get it wrong in a way that is silent until
it costs them work.

This feature packages the daemon as a Helm chart and publishes the image that chart installs. The
deployment is deliberately unambitious: **one replica, one volume, no CRDs**. Coordinare's state is
a single-process JSON snapshot, so a second replica would not scale it — it would corrupt it.

## Clarifications

### Session 2026-08-29

- Q: The chart needs a daemon image, and CI publishes only the performer images. Build one? → A:
  Yes. Spec 147 adds a daemon image build and push to CI. A chart pointing at an image nobody
  publishes is not shippable.
- Q: How should the dashboard be exposed, given the spec-144 guard rejects any non-loopback
  `Host`? → A: Ship a ClusterIP Service and have the chart add that Service's DNS name to the
  trusted-host list so it works on install. The widening must be explicit and narrow: the exact
  Service DNS name, never a wildcard, visible in the values file rather than buried in a template,
  and stated plainly in the chart README.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Install coordinare into a cluster (Priority: P1) — MVP

An operator with a Kubernetes cluster installs coordinare with one command and gets a running
daemon that reaches Ready, holds its state on a volume that survives restarts, and can dispatch
performer Pods.

**Why this priority**: It is the feature. Everything else refines a deployment that must first
exist.

**Independent Test**: `helm install` on a clean kind cluster; the controller Pod reaches Ready and
a dispatched performer appears as a Pod in the configured namespace.

**Acceptance Scenarios**:

1. **Given** a cluster with no coordinare, **When** the operator runs `helm install` with a minimal
   values file, **Then** a single controller Pod reaches Ready and its readiness endpoint answers.
2. **Given** a running controller, **When** it dispatches work, **Then** a performer **Pod** (not a
   Job) is created in the configured performer namespace under the spec-146 RBAC.
3. **Given** a running controller with recorded state, **When** its Pod is deleted and rescheduled,
   **Then** the daemon resumes from the persisted snapshot rather than starting empty.
4. **Given** a cluster with no default StorageClass, **When** the operator installs without
   requesting a performer cache volume, **Then** the install still succeeds and performers run with
   a cold cache.

### User Story 2 - Keep secrets out of the cluster's readable surfaces (Priority: P1)

An operator supplies a GitHub token and model credentials without any of those values landing in a
ConfigMap, in `helm template` output, in the chart's values file, or in a log line.

**Why this priority**: Equal-first with US1 rather than second. A deployment that leaks the token
is worse than no deployment, because the operator believes they are secured. Coordinare's config
already stores env-var *names* and never values, so the chart must use that indirection rather than
invent a parallel path.

**Independent Test**: Render the chart with credentials supplied and grep the full output for the
secret values; assert zero occurrences outside the Secret object itself.

**Acceptance Scenarios**:

1. **Given** an operator supplies a token, **When** the chart renders, **Then** the value appears
   only in a Secret and never in a ConfigMap or any other rendered object.
2. **Given** the rendered manifests, **When** the config is inspected, **Then** it references the
   environment variable **name**, never the value.
3. **Given** an operator prefers to manage secrets themselves, **When** they point the chart at a
   pre-existing Secret, **Then** the chart consumes it and creates none of its own.

### User Story 3 - Reach the dashboard without weakening the security posture (Priority: P2)

An operator opens the dashboard, and the trust boundary they are accepting is stated where they
will actually read it.

**Why this priority**: The dashboard is unauthenticated by design until spec 143. Access matters,
but not before the deployment works or the secrets are safe.

**Independent Test**: Install the chart, reach the dashboard through the Service, and confirm the
README states the widened trusted-host entry and the trust boundary in plain terms.

**Acceptance Scenarios**:

1. **Given** an installed chart, **When** the operator reaches the dashboard by its in-cluster
   Service name, **Then** the request is served rather than refused.
2. **Given** an installed chart, **When** the operator inspects the values file, **Then** the exact
   trusted host the chart adds is visible there, not hidden in a template.
3. **Given** the chart defaults, **When** the operator installs, **Then** nothing is exposed
   outside the cluster and no Ingress is created.
4. **Given** an operator who wants external access, **When** they read the chart README, **Then**
   they find a commented Ingress example carrying an explicit warning that the dashboard has no
   authentication.

### User Story 4 - Upgrade without losing state (Priority: P2)

An operator upgrades to a newer coordinare and keeps their state.

**Why this priority**: The second thing every operator does, and the one where a packaging mistake
destroys work rather than merely inconveniencing.

**Independent Test**: Install, record state, `helm upgrade`, confirm the state volume is the same
one and the daemon resumes from it.

**Acceptance Scenarios**:

1. **Given** an installed release with state, **When** the operator upgrades, **Then** the state
   volume is retained and the daemon resumes from the existing snapshot.
2. **Given** an upgrade, **When** the operator reads the documentation, **Then** the snapshot
   schema-version compatibility promise is stated.

### Edge Cases

- **An operator sets replicas above one.** The chart must refuse rather than obey. Coordinare's
  state is a single-process snapshot; two replicas racing on one volume corrupt it. This is a
  correctness constraint, not a tuning default, so it must be impossible to reach by accident —
  and where an override exists at all, the data-loss consequence must be stated at the point of
  override, not only in a document the operator has not opened.
- **No default StorageClass.** Common on minikube and microk8s. Two different things are at
  stake and conflating them would be a design error: the controller's own state volume is
  **required** and the operator must be able to name its class explicitly when no default
  exists; the performer cache is **optional** and its absence must never block an install. A
  chart that demanded a PVC nobody can provision would fail on exactly the small clusters
  this is meant to support.
- **The state path is left at its default.** Coordinare's default is a *relative* path, which in a
  container resolves onto the container filesystem and is lost on every restart. The chart must
  point it at the mounted volume. This is the single highest-consequence detail in the chart: it
  fails silently, looks fine for an entire session, and loses everything on the first reschedule.
- **The image tag is `latest`.** The controller must be pinned to a specific version by default so
  a Pod reschedule cannot silently change the running version.
- **An operator points the chart at a namespace they lack permission in.** The failure must name
  the missing permission rather than surfacing as an unexplained dispatch failure later.
- **The metrics endpoint.** It discloses card counts, model identifiers and error rates. It must
  not be exposed by the chart's defaults.

## Requirements *(mandatory)*

### Functional Requirements

**Deployment shape**

- **FR-001**: The chart MUST deploy the daemon as a single-replica workload with stable identity
  and an attached persistent volume for state.
- **FR-002**: The chart MUST make a replica count above one unreachable without a deliberate,
  explicit override, and any such override MUST carry a data-loss warning at the point of use.
- **FR-003**: The chart MUST configure the daemon's state path to the mounted persistent volume,
  overriding the relative default.
- **FR-004**: The chart MUST retain the state volume across upgrades of the release.
- **FR-005**: The chart MUST pin the controller image to an explicit version by default, never a
  floating tag.

**Credentials**

- **FR-006**: Secret values MUST appear only in Secret objects — never in a ConfigMap, the values
  file, or any other rendered manifest.
- **FR-007**: The rendered configuration MUST reference credentials by environment-variable name,
  using coordinare's existing indirection rather than a parallel mechanism.
- **FR-008**: The chart MUST support consuming an externally-managed Secret instead of creating
  one.

**Permissions**

- **FR-009**: The chart MUST provide the daemon exactly the namespace-scoped permissions spec 146
  established, templated from a single source rather than duplicated, and MUST NOT grant any verb
  beyond them.
- **FR-010**: The performer namespace MUST be configurable, and MUST default to the release's own
  namespace rather than to an unrelated one.
- **FR-011**: The controller MUST retain its own service-account credential, which it needs to
  manage performers. The token suppression spec 146 applies to *performer* Pods MUST NOT be
  extended to the controller.

**Health and exposure**

- **FR-012**: The chart MUST wire the daemon's liveness and readiness endpoints as the workload's
  probes.
- **FR-013**: The chart MUST NOT expose the metrics endpoint by default.
- **FR-014**: The chart MUST create no external ingress by default; the dashboard MUST NOT be
  reachable from outside the cluster on a default install.
- **FR-015**: The chart MUST make the dashboard reachable by its in-cluster Service name on a
  default install, by adding **that exact hostname** — never a wildcard, never a bind-all address —
  to the daemon's trusted-host list.
- **FR-016**: The trusted-host entry FR-015 adds MUST be visible in the chart's values file and
  described in the chart README, including what it widens and why. It MUST NOT be discoverable only
  by reading a template.
- **FR-017**: The chart MUST provide a disabled Ingress example carrying an explicit warning that
  the dashboard has no authentication.

**The image**

- **FR-018**: A coordinare daemon image MUST be built and published by the project's release
  pipeline, using the same registry, versioning and tagging the existing images use.
- **FR-019**: The daemon image definition MUST be corrected so its declared ports match the ports
  the daemon actually serves.
- **FR-020**: Adding the daemon image MUST NOT alter the triggers, required status, or runtime of
  any existing workflow.

**Documentation and verification**

- **FR-021**: The chart MUST ship a README covering install, the trust boundary, the dashboard
  access path, the single-replica constraint and its reason, the optional performer cache, and the
  upgrade and state-compatibility story.
- **FR-022**: Chart validity MUST be checked automatically, and an install MUST be exercised
  against a real cluster in the project's automation.
- **FR-023**: Cluster-dependent tests MUST skip visibly, naming the commands that create a
  cluster, and MUST NOT fail when no cluster is reachable.

### Key Entities

- **Controller workload**: the single-replica daemon deployment, its identity, and its probes.
- **State volume**: the persistent claim holding the snapshot and artifacts; survives restart and
  upgrade.
- **Rendered configuration**: the non-secret configuration delivered to the daemon.
- **Credential Secret**: the object holding values; referenced by environment-variable name.
- **Performer permissions**: the namespace-scoped grant from spec 146, templated here.
- **Dashboard Service**: the in-cluster address, and the trusted-host entry that makes it usable.
- **Daemon image**: the published artifact the chart installs.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator installs coordinare into a clean cluster with a single command and a
  minimal values file, and the controller reports itself healthy without further intervention.
  *(Narrowed when this shipped, because startup resolves a GitHub Project before the health
  server starts and a test had no board to resolve. Restored in full by issue #224, which
  answers that lookup from a stub running as a sidecar — so `/ready` is now asserted against a
  live cluster. Closing that gap surfaced four defects that had made a Kubernetes deployment
  unable to start at all.)*

- **SC-002**: A performer is dispatched and runs as a Pod under the spec-146 permissions, with no
  Docker socket anywhere in the deployment.
- **SC-003**: No credential value appears anywhere in the rendered output except inside a Secret,
  verified automatically rather than by inspection.
- **SC-004**: State survives both a Pod reschedule and a release upgrade.
- **SC-005**: A default install exposes nothing outside the cluster, and the dashboard is still
  reachable in-cluster.
- **SC-006**: The security trade-off the chart makes on the operator's behalf is stated in the
  chart README, in terms an operator can act on.
- **SC-007**: More than one replica cannot be reached by accident.
- **SC-008**: An install succeeds on a cluster that has storage but no *default* StorageClass, when the operator names the class for the controller's state volume. The optional performer cache never blocks the install. (The controller's own state volume does require storage — a cluster with no storage at all cannot run the single-replica-with-state design, as recorded under Assumptions.)
- **SC-009**: Chart validation and a real-cluster install both run in the project's automation and
  pass.
- **SC-010**: No existing workflow changes its triggers, required status, or runtime.

## Assumptions

- The cluster is conformant and provides a StorageClass for the controller's own state volume.
  Without persistent storage the single-replica-with-state design does not apply.
- The operator supplies their own board and model configuration; the chart packages coordinare, it
  does not configure coordinare's work.
- The controller runs in-cluster. Spec 146 established that running the daemon outside the cluster
  is a development convenience, since Pod addresses are generally not routable from outside.
- Registry credentials, where the operator's registry needs them, are supplied through the existing
  image-pull-secret configuration spec 146 added.

## Dependencies

- **Spec 146 (#200)** — the Kubernetes runtime, its namespace-scoped permissions, and the
  performer-namespace and cache configuration. Merged.
- **Spec 144 (#198)** — the trust-boundary posture and the trusted-host mechanism the dashboard
  decision depends on. Merged.

## Out of Scope

- Custom resources, an operator framework, leader election, or any multi-replica design. Coordinare's
  single-process state makes these dishonest today; they belong to a future phase only if that
  state model is ever outgrown.
- Dashboard authentication (spec 143 / #197). This feature documents the unauthenticated posture
  and makes one narrow, stated concession to reach the dashboard; it does not add login.
- Egress restriction for performers. Spec 146 recorded that it has no Kubernetes equivalent that
  applies reliably across CNIs, and shipping a control that silently fails to apply is worse than
  shipping none.
- Stress-testing performer Pod churn under contention ([#221](https://github.com/ViviDynamics/coordinare/issues/221)).
- Publishing the chart to a chart repository. The chart ships in-tree; distribution is a separate
  question.
