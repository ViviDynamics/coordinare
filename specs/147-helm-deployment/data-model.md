# Data Model: Helm Deployment (spec 147)

The chart's objects and how they relate. "Entities" here are Kubernetes objects and the values
that shape them; the feature adds no persisted coordinare state and no schema change.

## Objects

| Object | Kind | Purpose | Notes |
|---|---|---|---|
| Controller | `StatefulSet` | Runs the daemon | `replicas: 1`, enforced. `volumeClaimTemplate` binds state to the workload's identity so upgrades retain it. |
| State volume | PVC (from template) | `coordinare.state.json` + artifacts | Required. `state_file_path` is rendered to a path under its mount (FR-003). |
| Config | `ConfigMap` | `config.yaml` | Contains `${VAR}` placeholders only; never a credential value. |
| Credentials | `Secret` | Token and model keys | Consumed via `envFrom`. Not created when `existingSecret` is set. |
| App private key | `Secret` (volume) | GitHub App key | Projected as a read-only file; `github_private_key_path` points at the mount. A key is a file, not an env var. |
| Identity | `ServiceAccount` | The daemon's cluster identity | Keeps its token: the controller calls the API. Distinct from performer Pods, which suppress theirs. |
| Permissions | `Role` + `RoleBinding` | Namespace-scoped performer management | `pods` create/get/list/delete, `pods/log` get. No `watch` — spec 146 removed it after finding nothing called it. |
| Dashboard address | `Service` (ClusterIP) | In-cluster reachability | Its DNS name is the single entry added to `trusted_dashboard_hosts`. |
| External access | `Ingress` | Disabled by default | Present as a warned example only. |

## Relationships

```
StatefulSet ──uses──> ServiceAccount ──bound by──> RoleBinding ──> Role
     │                                                              (performer namespace)
     ├──mounts──> State PVC            (state_file_path points here)
     ├──envFrom──> Secret              (values; never in the ConfigMap)
     ├──mounts──> ConfigMap            (config.yaml with ${VAR})
     ├──mounts──> App key Secret       (read-only file)
     └──selected by──> Service ──named in──> trusted_dashboard_hosts
                          └──optionally fronted by──> Ingress (off)
```

## Values surface (the operator-facing shape)

| Value | Default | Meaning |
|---|---|---|
| `replicaCount` | `1` | Anything else fails the render unless `allowUnsafeMultiReplica`. |
| `allowUnsafeMultiReplica` | `false` | Named so the name is the warning. |
| `image.repository` / `image.tag` | published daemon image / a pinned version | Never a floating tag (FR-005). |
| `state.size`, `state.storageClass`, `state.mountPath` | sized default, cluster default, `/var/lib/coordinare` | The state volume. |
| `performers.namespace` | the release namespace | Where performer Pods are created. |
| `performers.cacheClaim` | `""` | Optional on purpose: no StorageClass must still install. |
| `performers.imagePullSecrets` | `[]` | Passed to performer Pods. |
| `dashboard.service.port` | `8090` | Matches coordinare's default. |
| `dashboard.trustedHosts` | the release's Service DNS name | **Visible here by requirement (FR-016)**, never a wildcard. |
| `dashboard.ingress.enabled` | `false` | Example carries an authentication warning. |
| `metrics.expose` | `false` | `/metrics` discloses operational detail. |
| `existingSecret` | `""` | When set, the chart creates no Secret. |

## Validation rules

1. `replicaCount != 1` fails rendering with an explanation, unless explicitly overridden.
2. The rendered `state_file_path` is absolute and under `state.mountPath`.
3. No value from the credential set appears in any rendered object other than a `Secret`.
4. The chart's Role verbs equal `deploy/kubernetes/rbac.yaml`'s exactly.
5. `image.tag` is not `latest` and not empty.
6. No `Ingress` is rendered by default.
