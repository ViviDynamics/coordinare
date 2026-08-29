# coordinare Helm chart

Runs the coordinare daemon in-cluster, managing performers as Pods.

**The daemon never needs `/var/run/docker.sock` here.** Mounting that socket is
root-equivalent access to the host and the largest single trust liability in the
Docker deployment. On this path coordinare holds a namespace-scoped ServiceAccount
granting `pods` and `pods/log`, and nothing else. See
[the threat model](../../../docs/security/threat-model.md).

## Install

```bash
kubectl create namespace coordinare

kubectl create secret generic coordinare-credentials \
  --namespace coordinare \
  --from-literal=GITHUB_TOKEN=...

helm install coordinare deploy/helm/coordinare \
  --namespace coordinare \
  --set existingSecret=coordinare-credentials \
  --values my-values.yaml
```

Minimum configuration — coordinare refuses to start without these, naming each
one it is missing:

```yaml
config:
  github_org: your-org
  project_name: your-project
  github_project_number: 5
  human_reviewers: [your-github-login]
```

Put your endpoints, roles and board settings in the same block. **Never put a
credential value there** — reference it as `${GITHUB_TOKEN}` and let the Secret
supply it. The chart wires `github_token` that way for you.

## One replica, and why it is not a default you should tune

Coordinare's state is a single-process JSON snapshot on one volume. A second
replica does not divide the work; it races on that file and corrupts it. So
`replicaCount` other than `1` **fails the render** rather than being obeyed, and
the failure explains itself.

`allowUnsafeMultiReplica: true` exists for anyone who has read that and still
wants it. The name is the warning.

## Reaching the dashboard, and what it costs

```bash
kubectl -n coordinare port-forward svc/coordinare 8090:8090
```

**The dashboard has no authentication.** Anyone who can reach it can rewrite
configuration, cancel work, and delete symphonies. Nothing is exposed outside the
cluster by default and it should stay that way until spec 143 adds
authentication.

### The trusted-host entry this chart adds for you

Coordinare guards the unauthenticated dashboard by refusing any request whose
`Host` header is not loopback (spec 144). The trusted-host list is empty by
default, deliberately, so the safe posture is what you get by doing nothing.

**This chart adds three entries to it**, so that the Service it ships actually
works instead of returning 403 on every request:

```
coordinare.<namespace>.svc.cluster.local
coordinare.<namespace>
coordinare
```

That is a real widening of a control that was deliberately opt-in, which is why
it is written here rather than left to be discovered in a template. It is kept as
narrow as it can be: exact hostnames only, never a wildcard, never a bind-all
address, and a test asserts none appears. `X-Forwarded-Host` is still not
honoured — it is attacker-controlled, and that decision is not revisited here.

Add your own hostnames with `dashboard.trustedHosts` if you front the dashboard
with something else. Adding a wildcard hands the guard away entirely.

### Ingress

Off by default. Turning it on publishes an unauthenticated dashboard to whatever
your ingress controller is reachable from. If you enable it, put authentication
in front of it at the ingress layer and treat that hostname as a trust boundary.

## Storage

| | Required? | Notes |
|---|---|---|
| Controller state | **Yes** | Holds the snapshot and artifacts. Name `state.storageClass` explicitly if your cluster has storage but no *default* StorageClass. |
| Performer env cache | No | `performers.cacheClaim`. Its absence makes performers cold, not broken — requiring it would fail to install on exactly the small clusters this is meant to support. |

The state claim is created from the StatefulSet's `volumeClaimTemplate`, so it is
bound to the workload's identity and an upgrade reattaches the same volume.

## Upgrades

```bash
helm upgrade coordinare deploy/helm/coordinare -n coordinare --reuse-values
```

State is retained. Coordinare's snapshot carries a schema version and newer
releases load older snapshots, applying defaults for fields that did not exist
when the snapshot was written. **Downgrades are not supported**: an older
coordinare has no way to understand a newer snapshot, and it will not pretend to.

Take a copy before a major upgrade if the work in flight matters:

```bash
kubectl -n coordinare cp coordinare-0:/var/lib/coordinare/coordinare.state.json ./coordinare.state.backup.json
```

## Uninstall

```bash
helm uninstall coordinare -n coordinare
```

**The state claim survives**, deliberately: an uninstall should not be able to
destroy work by accident. Remove it when you mean to:

```bash
kubectl -n coordinare delete pvc -l app.kubernetes.io/instance=coordinare
```

## Verifying the permissions

Ask the cluster, not the file — a Role bound under a different name keeps
granting whatever it lists, and `rbac.yaml` will not show it:

```bash
kubectl auth can-i --list --as=system:serviceaccount:coordinare:coordinare -n coordinare
```

You should see `pods` and `pods/log`, and nothing else.

## Known limits

- **No egress restriction for performers.** The Docker path restricts egress with
  in-container iptables via `NET_ADMIN`. The Kubernetes equivalent is a
  NetworkPolicy, which only applies if your CNI enforces them — kind, minikube and
  microk8s defaults often do not. Nothing here silently substitutes for it. Apply
  your own policy against `app.kubernetes.io/managed-by=coordinare` if you need it,
  having confirmed your CNI enforces it.
- **The dashboard is unauthenticated** until spec 143.
- **The daemon must run in-cluster.** Pod IPs are generally not routable from
  outside, so coordinare could not reach the performers it starts.
- **`/metrics` is not exposed** by default; it discloses card counts, model
  identifiers and error rates.
