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
  --from-literal=GITHUB_TOKEN=... \
  --from-literal=COORDINARE_DASHBOARD_AUTH_TOKEN="$(openssl rand -hex 32)"

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

The state claim's metadata carries only labels that do not change with a
release (chart 0.1.1 and later). Kubernetes forbids updates to
`volumeClaimTemplates` metadata, so a version-derived label there would reject
every upgrade outright; the claim renders version-stable labels instead and the
Pod template keeps the version.

Releases installed from chart 0.1.0 or earlier already carry the old labels, and
one recreation is needed to shed them. Throughout this README `coordinare` is
the example: substitute your release and namespace wherever you see it. Scope
the delete by the release label rather than the StatefulSet name, so it works
whatever the release is called:

```bash
kubectl -n coordinare delete statefulset -l app.kubernetes.io/instance=coordinare
helm upgrade coordinare deploy/helm/coordinare -n coordinare --reuse-values
```

The claim (and its data) is retained through the delete; the StatefulSet
reattaches it on the next install. Every later version bump is an ordinary
rolling update.

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

- **Egress restriction is available but off by default** (`performers.egress.enabled`). A NetworkPolicy is inert unless your CNI implements
  it, and turning it on where it is not enforced gives you no protection while
  looking as though it does. **Support is often partial, not absent.** Measured on kind: kindnet enforces *ingress* but not *egress*, so a policy applied there is silently inert in exactly the direction that matters here. That is why the question to ask is not "does my CNI support NetworkPolicy" but "does it enforce the direction I am relying on" — and `coordinare doctor --check egress` measures it rather than inferring it from the CNI's name.

  It restricts by **CIDR, not hostname**: `egress.to` accepts only `ipBlock` and
  selectors, so it cannot express "allow api.github.com". The Docker path's
  allowlist *is* hostname-based, so the two are **not equivalent**. For
  hostname-level control you need a CNI with FQDN policies, or an egress proxy.
- **The dashboard is unauthenticated** until spec 143.
- **The daemon must run in-cluster.** Pod IPs are generally not routable from
  outside, so coordinare could not reach the performers it starts.
- **`/metrics` is not exposed** by default; it discloses card counts, model
  identifiers and error rates.


The dashboard requires the `COORDINARE_DASHBOARD_AUTH_TOKEN` Secret entry because
its container binds a non-loopback address. The chart references the variable
name; never put the token into `config` or a committed values file. Connect through
port-forwarding, then use username `operator` and the token as the browser password.
The Service stays ClusterIP. For ingress/SSO and TLS, follow
[dashboard authentication](../../../docs/security/dashboard-auth.md).
