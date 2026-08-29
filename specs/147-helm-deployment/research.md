# Research: Helm Deployment (spec 147)

**Date**: 2026-08-29 | **Verified against**: main @ 7456429

Decisions with their rationale. Where an option was rejected, the reason it was rejected is
recorded, because the next person will otherwise re-propose it.

---

## R1 — Workload kind: StatefulSet

**Decision**: `StatefulSet`, `replicas: 1`, with a `volumeClaimTemplate` for state.

**Rationale**: The claim is bound to the workload's identity, so `helm upgrade` keeps it and a
reschedule reattaches the same volume. That is FR-004 satisfied by the object's own semantics
rather than by chart logic that could drift.

**Alternative rejected — Deployment + standalone PVC**: works, but the PVC becomes a separate
object whose lifecycle the chart must manage, and a `RollingUpdate` Deployment will briefly run two
Pods. Two coordinare processes on one state file is the exact corruption this spec exists to
prevent. `StatefulSet` defaults to `OrderedReady`, terminating the old Pod before starting the new
one, which is the behaviour we want and would otherwise have to configure.

---

## R2 — Enforcing one replica (FR-002)

**Decision**: `values.yaml` carries `replicaCount: 1`. The template calls Helm's `fail` when it is
anything else, and the message states the consequence. An escape hatch exists —
`allowUnsafeMultiReplica: true` — whose name is the warning.

**Rationale**: The issue asks that >1 require an explicit `--set`. That alone is too weak: `--set
replicaCount=3` is exactly what an operator types when they assume coordinare scales horizontally,
and nothing would stop them. Failing the render puts the explanation in front of them at the moment
they are wrong, which the README cannot do. FR-002's "at the point of override" comes from this.

**Alternative rejected — a schema `maximum: 1`**: `values.schema.json` would reject the value, but
the error is a JSON-schema violation with no room to explain *why* one replica is a correctness
constraint rather than a default someone chose.

---

## R3 — Secrets (FR-006, FR-007, FR-008)

**Decision**: the ConfigMap holds `config.yaml` containing `${VAR}` placeholders; a Secret holds
the values; the container gets them via `envFrom`. Nothing else is invented.

**Rationale**: This is not a new mechanism — `config.py:1198` runs
`os.path.expandvars(config_path.read_text())` over the whole file at load time, which is how
coordinare already separates config from credentials, and how `bin/start` already works with
`--env-file`. The chart uses the seam that exists.

`existingSecret` covers FR-008 for operators using External Secrets or Vault; when set, the chart
creates no Secret of its own.

**GitHub App private key**: `github_private_key_path` names a *file*, so it is projected as a
Secret volume mounted read-only, and the config points at the mount path. A key is not an env var
and pretending otherwise would mean writing it to disk at runtime.

---

## R4 — Dashboard reachability (FR-015, FR-016)

**Decision**: a ClusterIP Service, and `trusted_dashboard_hosts` seeded with the Service's exact
in-cluster DNS name, rendered from a values entry the operator can see.

**Rationale**: Spec 144's guard rejects any request whose `Host` is not loopback. A Service without
this would return 403 on every request — a resource that ships broken. `kubectl port-forward` alone
would have avoided the question entirely (it lands in the Pod's network namespace, so `Host` stays
`127.0.0.1`), and was the safer option, but the requester chose the Service.

**Constraints that follow from it being a real widening**: the entry is the exact
`<release>-coordinare.<namespace>.svc.cluster.local`, never a wildcard and never a bind-all address;
it lives in `values.yaml` where `helm show values` prints it; and the chart README states what was
widened and why. Spec 144 made this list opt-in deliberately, so a chart opting in on the
operator's behalf must at minimum be legible about it. `X-Forwarded-Host` remains unhonoured —
that decision is spec 144's and is not revisited here.

`dashboard_host` must also become `0.0.0.0` inside the Pod, since the loopback default is
unreachable from a Service. The guard, not the bind address, is what constrains access — which is
precisely why the guard's entry has to be right.

---

## R5 — RBAC single source (FR-009)

**Decision**: the chart templates the ServiceAccount, Role and RoleBinding; `deploy/kubernetes/rbac.yaml`
remains the standalone artefact for operators not using Helm. A test asserts the two grant the
same verbs.

**Rationale**: Two copies will diverge, and the copy that drifts is the one nobody reads. Spec 146
already ships a test tracing every granted verb to the client call that uses it; the equality test
extends that guarantee to the chart rather than restating it. Notably the chart must not
reintroduce `watch`, which spec 146 removed after finding nothing called it.

---

## R6 — The daemon image (FR-018, FR-019, FR-020)

**Decision**: refresh `Dockerfile.daemon` and add a `build-daemon` job to
`main-branch-build.yml`, mirroring `build-base`/`build-full` — same GHCR registry, same CalVer from
the `Derive Version` job, same `Tag Latest` treatment.

**Findings**: `Dockerfile.daemon` was last touched 2026-05-20 (spec 065) and is built only by
`bin/start:54`. No workflow references it, so **no daemon image has ever been published**. Its
`EXPOSE 9090 9091` contradicts the real defaults (health 8080, dashboard 8090); `EXPOSE` is
documentation-only, so this is misleading rather than broken, but it is the kind of misleading that
sends someone debugging the wrong layer.

**Not done**: the image will not mount `/var/run/docker.sock`. On Kubernetes the daemon manages
Pods through the API, and carrying the socket over would reintroduce the root-equivalent trust
liability spec 146 exists to remove.

---

## R7 — CI (FR-022, FR-023)

**Decision**: `helm lint` plus `helm template` assertions run in the existing PR test job (they are
fast and need no cluster). The kind-based install runs as a separate job that installs `kind` and
`helm` explicitly rather than assuming the self-hosted runner has them.

**Rationale**: CI runs on self-hosted runners, and `kind`/`helm` are not currently used by any
workflow, so their presence cannot be assumed. Both are single binaries. Adding jobs does not
change any existing job's triggers or required status (FR-020, SC-010).

**Risk, stated rather than discovered later**: a kind cluster inside a self-hosted runner needs a
usable Docker daemon. The runners already build and push images, so Docker is present, but nested
cluster creation is a heavier operation than a build. If it proves unreliable the job stays
non-required, and the local `make` path remains the authoritative check — an unreliable required
check is worse than none, because it trains people to ignore red.

---

## R8 — How this gets tested (Constitution II)

**Decision**: Python tests under `tests/unit/test_147_helm_deployment.py` that shell out to `helm
template`, parse the YAML, and assert on the objects — following the spec-146 precedent where
`tests/unit/test_146_kubernetes_transport.py` parses `rbac.yaml`. Cluster-dependent tests go in
`tests/integration/test_147_helm_install.py` and skip visibly, naming the commands to create a
cluster.

**Rationale**: keeps chart verification inside the suite everyone already runs, rather than in a
separate tool with its own invocation that will be forgotten. The secret-leak check (FR-006) is
exactly this shape: render with a sentinel value, assert it appears in no object other than the
Secret.

**Skip discipline**: spec 146's integration module already establishes that a skip must name the
commands that would make it run. A silently-skipped install test is how "coordinare installs on
Kubernetes" stays green while quietly becoming untrue.

---

## R9 — State path (FR-003)

**Decision**: the rendered config sets `state_file_path` to a path on the mounted volume.

**Rationale**: the default is `./coordinare.state.json` — *relative*, resolving against the
container's working directory. Left alone the daemon runs correctly, persists nothing durable, and
loses everything on the first reschedule. It is the highest-consequence line in the chart and the
one with no visible symptom until the moment it costs work, so a test asserts the rendered path is
absolute and under the mount.
