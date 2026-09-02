# Feature Specification: Coordinare in the cluster cluster, delivered by ArgoCD

**Feature Branch**: `159-cluster-deployment`
**Created**: 2026-09-01
**Status**: Draft
**Input**: Run coordinare in the cluster Kubernetes cluster for real testing. Performer Pods take
their environment cache from a PVC on the storage-server. ArgoCD keeps the deployment in sync with
this repo as the Helm chart changes.

## Context

Spec 146 made performers run as Pods and spec 147 packaged the controller as a Helm chart. Both
are implemented and neither has ever run anywhere but a kind cluster on a laptop. The chart's
`Chart Install (kind)` job proves a great deal about the *controller* — it installs, binds its
claim, parses its config, holds exactly the RBAC it needs, answers `/ready`, and survives a
reschedule and an upgrade — and nothing at all about a card completing on a performer Pod. Spec
146's SC-001 rests on a hand-run spike, and `tests/integration/test_146_kind_e2e.py`, which would
re-prove it, is run by no workflow.

This spec is the first deployment where coordinare does real work in a cluster it does not own.

## Clarifications

Settled before design:

- **Scope of the first deployment**: the full loop — controller plus performer Pods dispatching
  real cards — against a scratch repository and board, not a live one.
- **Model access**: the in-cluster LiteLLM Service, not the LAN model host. LiteLLM keeps the
  route to Spark; coordinare never learns an address that DHCP can move underneath it.
- **Default model**: `spark/qwen3.8:27b`, the newest Qwen registered in the cluster's LiteLLM.
- **Cache layout**: env caches on the storage-server NFS claim; performer workspaces on ephemeral
  node disk.
- **Chart delivery**: an OCI chart published by this repo's CI, tracked by an ArgoCD Application
  by version range — the pattern other in-house projects already use in this cluster.
- **Versioning**: coordinare adopts the org's CalVer, `YYYY.M.<counter>`, the scheme in-house and
  sibling repos already mint. One version is the git tag, the GitHub release, the image tag and the
  chart version. Coordinare's current `YYYY.MM.DD[.N]` is four components with a zero-padded month,
  so it is not valid SemVer and Helm will not accept it as a chart version — the alternative was
  mapping one onto the other, which is a translation layer that exists only because two schemes
  do.

## User Scenarios & Testing

### User Story 1 — A card completes on a performer Pod in cluster (Priority: P1)

An operator moves a card into the board's dispatch column. The in-cluster coordinare picks it up,
creates a performer Pod, the performer clones the scratch repository, does the work, pushes a
branch and opens a PR. The Pod is deleted when the performer is released.

**Why this priority**: it is the thing that has never happened. Everything else here exists to
make it possible or to make its failure legible.

**Independent Test**: watch the board, then `kubectl get pods -n coordinare -w`, then the PR.

**Acceptance Scenarios**:

1. **Given** the chart is installed and the board has a card in the dispatch column, **When** the
   daemon polls, **Then** a performer Pod is created in the coordinare namespace with coordinare's
   management labels, and no Docker socket is mounted anywhere.
2. **Given** a performer has finished, **When** it is released, **Then** its Pod is deleted and
   does not outlive the run.
3. **Given** a performer Pod fails to start because its image cannot be pulled, **When** the
   daemon observes it, **Then** the failure surfaces as an existing error classification with the
   pull error as its reason, not as a new state and not as a silent hang.

---

### User Story 2 — The env cache is shared safely over NFS (Priority: P1)

A symphony's first dispatch is a bootstrap performer, which builds the environment into the cache
claim. Later performers for that symphony read it and do not write to it.

**Why this priority**: the Docker path enforces one writer per symphony by mounting the cache
read-only for consumers. The Kubernetes pod builder mounts a single claim read-write for every
performer, so on a shared NFS volume several performers can write into one tree at once. NFS is
the worst substrate for that, and the corruption would look like an environment that mysteriously
stops working.

**Independent Test**: dispatch a bootstrap performer, then two consumers concurrently, and inspect
the mounts on each Pod.

**Acceptance Scenarios**:

1. **Given** a bootstrap dispatch for symphony `S`, **When** the Pod is built, **Then** its cache
   volume is mounted read-write at the symphony's own subpath.
2. **Given** a non-bootstrap dispatch for symphony `S`, **When** the Pod is built, **Then** its
   cache volume is mounted **read-only**.
3. **Given** two symphonies, **When** both have performers running, **Then** neither can see the
   other's cache directory through its mount.
4. **Given** no cache claim is configured, **When** a Pod is built, **Then** it runs with a cold
   cache rather than failing — spec 146's FR-012 behaviour is unchanged.

---

### User Story 3 — The deployment tracks this repository (Priority: P2)

A change to the chart merges to coordinare's main. The chart publishes, ArgoCD notices the new
version, and the cluster converges without anyone running `helm upgrade`.

**Why this priority**: it is what makes the deployment testable at the pace we actually change
things. It is P2 because the first bring-up can be a manual sync.

**Acceptance Scenarios**:

1. **Given** a merge to main that changes the chart, **When** CI completes, **Then** a new chart
   version exists in `ghcr.io/vividynamics/charts` whose `appVersion` matches the daemon image
   built by the same run.
2. **Given** a published chart version, **When** ArgoCD reconciles, **Then** the Application
   reports Synced and Healthy without manual intervention.
3. **Given** an out-of-band Secret in the namespace, **When** ArgoCD prunes, **Then** the Secret
   is not deleted (`prune: false`).

---

### User Story 4 — A reasoning model that thinks too long is not reported as broken (Priority: P2)

A performer's model spends its whole token budget reasoning and returns empty content. Coordinare
retries with a larger budget rather than reporting malformed output.

**Why this priority**: measured on this cluster's LiteLLM (issue #244). `spark/qwen3.6:35b`
returns `finish_reason: length` with `content: ""` at 800 tokens and a clean, parseable contract
response at 2000. The chosen default `spark/qwen3.8:27b` is unaffected at 800, so this is not a
blocker for the first deployment — but the failure mode is invisible in the logs as anything but
"the model is bad", and this deployment is where hard prompts will first meet it.

Tracked as issue #244 and may land independently of this spec — the chosen default model does not
trigger it, so SC-001 does not depend on it. Included here because this deployment is where the
hard prompts live, and because the symptom is indistinguishable from "the model is bad" without
the fix.

**Acceptance Scenarios**:

1. **Given** a completion with `finish_reason == "length"` and empty content, **When** coordinare
   classifies it, **Then** it is distinguished from malformed output and retried with a raised
   cap.
2. **Given** a completion with empty content and populated `reasoning_content`, **When** it
   arrives over the direct LiteLLM path, **Then** the reasoning is promoted the same way the
   `SelfHostedShim` chain already promotes it.

## Requirements

- **FR-001**: Coordinare's release version MUST be `YYYY.M.<counter>` — month unpadded, counter
  incrementing within the month — matching in-house and sibling repos. It MUST be valid SemVer.
- **FR-001a**: The release version MUST be minted once and used verbatim as the git tag, the
  GitHub release, the image tag and the chart version. No component may derive a second version,
  and no mapping between versions may exist.
- **FR-001b**: CI MUST publish the chart to `ghcr.io/vividynamics/charts` on merge to main, at the
  version `derive-version` minted, with `appVersion` equal to that version. The publish MUST NOT
  compute a version of its own; it consumes `needs.derive-version.outputs.version`.
  (An earlier draft said "from the same job that mints the version", copying the in-house scheme's arrangement.
  in-house needed that because its publish lived in a *second workflow* deriving `0.1.<commit-count>`
  independently — two versions for one push. Coordinare already separates minting from stamping and
  every consumer reads the one output, so the property the scheme was buying is already held; requiring
  the same *job* would be cargo-culting the remedy rather than the reason.)
- **FR-001c**: The counter MUST ignore coordinare's existing four-component tags. Parsed loosely,
  `2026.09.01.3` yields a patch of `01` and the next mint would collide with a pushed tag.
- **FR-002**: An ArgoCD Application MUST track that chart by version range, in the `platform`
  project, syncing automatically with `prune: false` and `selfHeal: true`.
- **FR-003**: The coordinare namespace and its cache PVC MUST be owned by a separate manifests
  Application, so the chart Application never owns storage that outlives it.
- **FR-004**: The controller's state volume MUST use the `nfs-storage-server` StorageClass.
- **FR-005**: The env-cache claim MUST be ReadWriteMany on `nfs-storage-server`, and MUST be
  referenced by the chart rather than created by it.
- **FR-006**: A performer Pod's cache mount MUST be read-write only for a bootstrap dispatch, and
  read-only otherwise.
- **FR-007**: A performer Pod's cache mount MUST be scoped to its symphony's subpath, so one
  symphony's performers cannot read or write another's cache.
- **FR-008**: Performer workspaces MUST NOT be placed on the NFS claim.
- **FR-009**: Model access MUST be by in-cluster Service name. No model host IP address appears in
  any committed file.
- **FR-010**: Credentials MUST reach the deployment through Secrets created out-of-band and
  documented in the namespace README. No credential value is committed.
- **FR-011**: The dashboard MUST remain ClusterIP with no Ingress, and the namespace README MUST
  say why: it has no authentication until spec 143.
- **FR-012**: A completion with `finish_reason == "length"` and empty content MUST be classified
  distinctly from malformed output, and retried **once** with the cap doubled, bounded by the
  role's configured maximum. Retrying an identical request cannot succeed and spends a retry that
  a genuinely malformed response will need.

## Success Criteria

- **SC-001**: A card dispatched on the scratch board completes end to end on a performer Pod in
  cluster, with no Docker socket involved. This is spec 146's SC-001, finally executed somewhere
  that persists.
- **SC-002**: `tests/integration/test_146_kind_e2e.py` passes against a real cluster before the
  first cluster deploy, and is wired into CI so it keeps passing.
- **SC-003**: Two performers for the same symphony run concurrently without both holding a
  read-write cache mount, asserted on the built Pod specs.
- **SC-004**: A chart change merged to main reaches the cluster with no human running helm.
- **SC-004a**: The version minted for a release appears unchanged as the git tag, the chart
  version and `appVersion`, and Helm accepts it without a mapping step anywhere in CI.
- **SC-004b**: No CI job derives a version. Exactly one job computes one, and every other consumer
  reads its output.
- **SC-005**: Every existing Docker-path test passes unmodified.
- **SC-006**: No committed file in either repository contains a credential or a model-host IP.

## What review raised and what it came to

Recorded because two of these were real defects in the fix, and one was a plausible finding that
does not hold.

**The month boundary does not produce two versions for one commit.** `next_calver.py` reads
`datetime.now(UTC)`, so a re-run after a month rollover computes a different number — but
`derive-version` runs `git fetch --tags --force` *before* checking `git tag --points-at HEAD`, so a
tag that was successfully created is always found and reused. If `create-tag` failed, no tag and no
release exist, and the number that was never used is not a version anyone can see. The harm the
finding described requires a release to exist for a version the guard cannot find, which this
ordering rules out. Left as in-house has it; deviating would be divergence for no gain.

**A `container_devenv_root` of `/` admitted every volume.** `"/".rstrip("/")` is `""`, and the
first implementation filtered with `startswith(devenv_root)` — so every absolute path matched and a
secrets mount would have been served out of the cache claim. `relative_to` refuses an empty base,
so the filter is now written in terms of it.

**The subpath and the mount path came from different sources.** The subpath was
`Path(host_path).name` while the mount path was `container_path`. Two symphonies whose cache
directories shared a basename collided on one subpath, the second read-write, so that performer
wrote into the other symphony's cache — the exact corruption FR-006 and FR-007 exist to prevent.
Both now derive from the container path, which makes the collision unconstructable rather than
detected.

## Assumptions

- The cluster cluster's `nfs-storage-server` StorageClass is available to the coordinare namespace,
  and its NFS export permits the chart's `fsGroup` (10001) to write. **This is the most likely
  thing to be wrong on first contact**: NFS exports commonly squash root and may not honour the
  supplemental group, in which case the bootstrap performer cannot populate the cache. Verified
  before the first dispatch, not after.
- Performer images are pullable in-cluster. The daemon image is published to ghcr today; the
  performer images (`base`, `full`, and the `extra` variant QA runs on) must be reachable by the
  same means, with `ghcr-pull` in the namespace.
- Performers reach GitHub and package registries with unrestricted egress. Egress allowlisting is
  out of scope for the Kubernetes path (spec 146), and `performers.egress` stays off: the chart's
  own values warn that a NetworkPolicy which the CNI does not enforce is worse than none.
- Coordinare remains a single replica. The chart already refuses more.

## Out of Scope

- Dashboard authentication (spec 143). Until it exists, the dashboard is reached by port-forward
  and is not placed behind the tunnel that fronts the public LiteLLM alias.
- Moving coordinare state off single-host JSON.
- Running coordinare against a live repository. The scratch repo is the whole point of the first
  deployment.
- Egress allowlisting for performer Pods.
- Any change to the Docker path's behaviour.
