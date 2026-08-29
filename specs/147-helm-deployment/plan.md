# Implementation Plan: Helm Deployment — coordinare-controller In-Cluster

**Branch**: `147-helm-deployment` | **Date**: 2026-08-29 | **Spec**: [spec.md](./spec.md)
**Issue**: [#201](https://github.com/ViviDynamics/coordinare/issues/201)

## Summary

Ship a Helm chart that runs the coordinare daemon in-cluster as a single-replica StatefulSet
managing performer Pods through the spec-146 runtime, and publish the daemon image that chart
installs — which does not exist today. The deployment is deliberately small: one replica, one
volume, no CRDs, no leader election, because coordinare's state is a single-process JSON snapshot
that a second replica would corrupt rather than scale.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv) for tests; the
deliverables are Helm templates (YAML + Go templating), a Dockerfile, and workflow YAML.

**Primary Dependencies**: None added to the Python project. Build/CI tooling: `helm` (3.13.3
available locally), `kind`, `kubectl`. The chart consumes only stable core and `rbac.authorization.k8s.io/v1`
APIs — no CRDs, no vendor extensions.

**Storage**: A `volumeClaimTemplate` on the StatefulSet holds `coordinare.state.json` and artifacts.
The performer environment cache claim (spec 146 `kubernetes_cache_claim`) stays optional, so a
cluster with no default StorageClass still installs.

**Testing**: `tests/unit/test_147_helm_deployment.py` renders the chart with `helm template` and
asserts on parsed objects; `tests/integration/test_147_helm_install.py` installs against a live
cluster and skips visibly when none is reachable.

**Target Platform**: Any conformant Kubernetes cluster. No assumption that a StorageClass exists
beyond the controller's own state volume, nor that the CNI enforces NetworkPolicy.

**Project Type**: Brownfield packaging + CI. No coordinare runtime behaviour changes.

**Constraints**: Must not alter spec-146 runtime behaviour; must not change any existing workflow's
triggers, required status, or runtime; no secret value may reach a ConfigMap or rendered output.

**Scale/Scope**: Exactly one controller replica by design.

## Constitution Check

| Principle | Assessment |
|---|---|
| I. Code Quality First | Templates stay readable over clever. The one piece of template logic with real consequence — the replica guard — carries its reasoning inline. No duplicated RBAC: the chart templates it and a test pins it equal to the standalone manifest. |
| II. Testing Discipline (NON-NEGOTIABLE) | Tests precede templates. Rendering assertions are deterministic (no cluster, no network). Cluster-dependent tests skip visibly rather than failing, and the skip names the commands that would make it run. |
| III. User Experience Consistency | The chart README follows the spec-146 `deploy/kubernetes/README.md` voice: state the trade-off plainly, never imply a protection that is not there. |
| IV. Performance | Not applicable — packaging, not a runtime path. |
| V. Observability | Existing `/live` and `/ready` become the probes. `/metrics` is deliberately not exposed by default (it discloses card counts, model identifiers, error rates). |

**Gate: PASS.** No violation requiring justification.

## Project Structure

### Documentation (this feature)

```
specs/147-helm-deployment/
├── spec.md
├── plan.md              # this file
├── research.md          # R1-R9 decisions
├── data-model.md        # the chart's objects and their relationships
├── quickstart.md        # install, reach the dashboard, upgrade
├── contracts/
│   └── values-contract.md   # the values surface treated as a compatibility promise
├── checklists/
│   └── requirements.md
└── tasks.md             # generated next
```

### Source Code (repository root)

```
deploy/
├── kubernetes/
│   ├── rbac.yaml            # spec 146, unchanged — the non-Helm path
│   └── README.md            # spec 146, gains a pointer to the chart
└── helm/coordinare/          # NEW
    ├── Chart.yaml
    ├── values.yaml          # the trusted-host entry is visible HERE (FR-016)
    ├── README.md
    ├── .helmignore
    └── templates/
        ├── _helpers.tpl
        ├── NOTES.txt
        ├── statefulset.yaml     # replicas=1 + guard, probes, volume, envFrom
        ├── configmap.yaml       # config.yaml with ${VAR} placeholders
        ├── secret.yaml          # skipped when existingSecret is set
        ├── service.yaml         # ClusterIP
        ├── rbac.yaml            # SA + Role + RoleBinding (spec-146 verbs)
        └── ingress.yaml         # disabled by default, warning in values + README

Dockerfile.daemon                # refreshed: ports, base, non-root
.github/workflows/
├── main-branch-build.yml        # + build-daemon job, + daemon in Tag Latest
└── pr-ci.yml                    # + chart lint/render checks, + kind install job

tests/unit/test_147_helm_deployment.py
tests/integration/test_147_helm_install.py
```

**Structure Decision**: `deploy/helm/coordinare/` sits beside the existing `deploy/kubernetes/`, so
the two deployment paths — raw manifests and chart — are discoverable together. Tests live in the
existing suites rather than a separate chart-testing tool, so `make test` remains the single
command that checks everything.

## Implementation Phases

**Phase 1 — Tests first (Constitution II).** Write the rendering assertions against a chart that
does not exist yet, and watch them fail: replica guard, state path absolute and under the mount,
no secret outside a Secret, RBAC equals `deploy/kubernetes/rbac.yaml`, no Ingress by default,
`/metrics` not exposed, probes wired, image tag pinned.

**Phase 2 — The image.** Refresh `Dockerfile.daemon` (correct `EXPOSE`, current base, non-root
where practical, no docker.sock) and add `build-daemon` to `main-branch-build.yml` mirroring
`build-base`/`build-full`, plus its `Tag Latest` entry.

**Phase 3 — The chart.** Templates until Phase 1 is green. Order: helpers → RBAC → ConfigMap and
Secret → StatefulSet → Service → Ingress (disabled) → NOTES.txt.

**Phase 4 — Live install.** kind: `helm install`, controller reaches Ready, dispatch a performer
Pod, delete the Pod and confirm state survives, `helm upgrade` and confirm the claim is retained.

**Phase 5 — Documentation.** Chart README (trust boundary, dashboard access, the trusted-host
widening in plain words, single-replica reasoning, optional cache, upgrade/schema promise);
`quickstart.md`; a pointer from `deploy/kubernetes/README.md`.

**Phase 6 — CI and verification.** Add the PR jobs, full suite, lint, acceptance re-read against
SC-001..SC-010, scope check, commit.

## Complexity Tracking

| Item | Why it is not simpler |
|---|---|
| Chart templates RBAC that also exists in `deploy/kubernetes/rbac.yaml` | Helm cannot include a file outside the chart directory. Rather than let two copies drift, a test asserts they grant identical verbs — the duplication is real but pinned. |
| A `fail`-based replica guard rather than a schema maximum | The guard has to *explain*. A JSON-schema violation states that 3 is not allowed; it cannot say that coordinare's state is a single-process snapshot and a second replica corrupts it. |
| Adding an image build to CI inside a chart spec | A chart pointing at an image nobody publishes is not installable. Settled with the requester as D1. |

## Risks

- **kind inside a self-hosted runner** may be unreliable (R7). Mitigation: the install job stays
  non-required; the local path remains authoritative. An unreliable required check trains people to
  ignore red, which is worse than not having it.
- **The chart widens a spec-144 security control** (R4). Mitigation is legibility, not concealment:
  exact hostname only, visible in `values.yaml`, stated in the README, asserted by test.
- **`state_file_path` left relative** silently loses all state on the first reschedule (R9).
  Mitigation: a test asserts the rendered path is absolute and under the mount.
