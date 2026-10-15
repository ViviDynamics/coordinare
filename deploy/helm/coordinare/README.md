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
# With mcp.enabled=true, the Secret must also carry the sidecar's two tokens:
#   --from-literal=COORDINARE_MCP_TOKEN="$(openssl rand -hex 32)" \
#   --from-literal=COORDINARE_MCP_DASHBOARD_TOKEN="<same value as COORDINARE_DASHBOARD_AUTH_TOKEN>"

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

## Persistent state

Two claims hold everything that must survive a restart. Know both before you
install.

| Claim | Created by | Namespace | Access | Holds | Lost if it is missing or empty |
|---|---|---|---|---|---|
| `state-<release>-0` (controller state) | The StatefulSet's `volumeClaimTemplate`, always | Release namespace | `ReadWriteOnce`, one writer | The snapshot `coordinare.state.json` (sessions, cards, PR metadata, retry counters, decision history), `env-caches/` (the daemon's side of the environment cache), `transcripts/` and `run-logs/` | Sessions and card history. Every symphony re-bootstraps its environment. |
| `coordinare-devenv-cache` (performer environment cache) | The chart when `devenvCache.create` is unset or true, otherwise you | Performer namespace | `ReadWriteMany`: the bootstrap writes it, consumers mount it read-only | One subdirectory per symphony, named with the sanitised symphony name and mounted at `/devenv/<sanitised-name>`: rbenv and gems, node modules, debs | Nothing you have written. Every symphony re-bootstraps on its next dispatch. |

**Persistence is not optional for the controller.** The state claim has no
switch to turn it off, and there is no `emptyDir` option. The snapshot is the
only record of where each card stands, so an `emptyDir` would look like a working
install and lose everything at the first reschedule.

**The performer cache is on by default, and the default needs a
ReadWriteMany StorageClass.** Without one, the claim stays `Pending`. The
bootstrap performer and every consumer that mounts the claim then wait, and no
symphony gets its environment until it binds. See the next section.

**Opting out** with `devenvCache.enabled: false` runs every performer with a cold
cache. Each dispatch then repeats the full environment bootstrap for its symphony.
That is slower, not broken. The chart prints a warning in `NOTES.txt` when it is
off, so the choice is visible.

### RWO and RWX, and why the cache needs RWX

`ReadWriteOnce` binds a claim to one node. The controller is one Pod on one node,
so its state claim is RWO and that is correct.

The performer cache is different. The bootstrap performer writes it while consumer
performers mount it read-only, and those can run on different nodes at the same
time. That needs `ReadWriteMany`. The chart defaults to it, and a RWO class will
leave the claim Pending or strand consumers on other nodes.

Kind's default StorageClass (`local-path`) is RWO-only. On kind, turn the cache off
or pick an RWX class. The chart's own kind CI job turns it off.

### The default needs an RWX StorageClass

Before you install with the default, check that the cluster has a class that
supports `ReadWriteMany`:

```bash
kubectl get storageclass
```

Look for a class whose provisioner documents RWX (NFS, CephFS, EFS, Azure Files,
and Longhorn with RWX are common). Set `devenvCache.storageClassName` to it.

After the install, check the claim binds:

```bash
kubectl get pvc -n coordinare-performers coordinare-devenv-cache
```

| STATUS | Meaning |
|---|---|
| `Bound` | The claim is ready. |
| `Pending` | No RWX-capable class is serving it. The bootstrap performer and every consumer wait, and no symphony gets its environment. Fix the class, or opt out. |

Opting out with `devenvCache.enabled: false` is the other choice. Every dispatch
then repeats the full environment bootstrap for its symphony.

### Symphony subdirectories

Each symphony gets one subdirectory on the claim. Its name is the **sanitised
symphony name**, produced by `sanitise_symphony_name` in coordinare's env-cache
service:

1. Lowercase the name.
2. Replace every character outside `a-z`, `0-9`, `_` and `-` with `-`.
3. Strip leading and trailing dashes. An empty result becomes `symphony`.
4. Append `-` and the first six hex characters of the SHA-1 of the original name.

The suffix keeps two symphonies whose names sanitise to the same slug apart.
Each performer Pod mounts only its own symphony's subdirectory, read-only for
consumers:

| Symphony name | Subdirectory on the claim (subPath) | Mount inside the Pod |
|---|---|---|
| `dettmore-platform` | `dettmore-platform-615891` | `/devenv/dettmore-platform-615891` |
| `Dettmore Platform` | `dettmore-platform-4b278e` | `/devenv/dettmore-platform-4b278e` |

To see a symphony's directory on the claim, use the sanitised name as the path,
from a performer Pod or on the NFS export. Measure it with
`du -sh /devenv/<sanitised-name>`.

### Sizing

- **State (8Gi default).** The snapshot is small. Transcripts and run logs fill
  the claim, and they are capped by config retention (`transcripts.retention_days`,
  `run_logs`). Check usage with `du -sh /var/lib/coordinare/*` from inside the Pod.
- **Performer cache (50Gi default).** It holds every symphony's toolchain. Measure
  one symphony with `du -sh /devenv/<sanitised-name>` after a successful bootstrap,
  multiply by the symphonies you run, and add headroom. A full claim fails
  bootstraps partway through, which is harder to diagnose than a small claim.

Both claims can be grown only if their StorageClass allows volume expansion. The
access mode of an existing claim cannot be changed.

### Backup

- **State:** copy the snapshot before a major upgrade or a risky change:

  ```bash
  kubectl -n coordinare cp coordinare-0:/var/lib/coordinare/coordinare.state.json ./coordinare.state.backup.json
  ```

- **Performer cache:** a backup is optional, because a bootstrap rebuilds it. A
  rebuild costs time, not data. If your storage can snapshot the export (NFS
  server snapshots, or your CSI driver's snapshot class), that is faster to restore.

### Examples

An NFS-backed cluster, using an NFS subdirectory provisioner's class:

```yaml
state:
  storageClass: nfs-client
devenvCache:
  storageClassName: nfs-client
  size: 100Gi
  accessModes: [ReadWriteMany]
```

A generic RWX class, when the cluster has no default and you name one:

```yaml
state:
  storageClass: standard-rwo
devenvCache:
  storageClassName: rwx-fast
  size: 50Gi
  accessModes: [ReadWriteMany]
```

### Using a claim that already exists

Set `create: false` and name the claim. The chart wires it in and renders no
PersistentVolumeClaim:

```yaml
devenvCache:
  create: false
  existingClaim: coordinare-devenv-cache
```

The claim must live in the performer namespace. `devenvCache.create` is a
tri-state: leave it unset and naming an `existingClaim` is enough to wire it in,
so `create: false` is only needed to be explicit. The chart fails with a clear
message if `create` is explicitly `true` and `existingClaim` is set, if
`create: false` names no claim, or if `devenvCache.namespace` disagrees with
`performers.namespace`.

### Upgrading from `performers.cacheClaim`

An install that sets only `performers.cacheClaim` keeps working unchanged. The
chart wires that claim, renders no PVC, and `NOTES.txt` prints a deprecation
notice with the migration. To migrate when you are ready:

```yaml
devenvCache:
  create: false
  existingClaim: coordinare-devenv-cache   # the same name as performers.cacheClaim
performers:
  cacheClaim: ""
```

### The performer namespace

The chart does not create the performer namespace, just as it does not create the
release namespace. When `performers.namespace` is not the release namespace, create
it first:

```bash
kubectl create namespace coordinare-performers
```

The chart renders the claim there, and the installer needs rights in that namespace.
The daemon's own ServiceAccount does not, and this chart does not grant it any.

### A hand-set `config.kubernetes_cache_claim`

While the chart wires a claim, it sets `kubernetes_cache_claim` from `devenvCache`,
and a hand-set value in `config` could disagree with it. So the render fails when
both are present. The chart accepts a hand-set value only when the cache is off
(`devenvCache.enabled: false`), because then the chart wires nothing.

To migrate, remove the key from `config` and name the claim in `devenvCache`:

```yaml
config:
  # kubernetes_cache_claim: removed. The chart sets it from devenvCache.
devenvCache:
  create: false
  existingClaim: <the claim your config named>
```

### Argo CD

Helm's `helm.sh/resource-policy: keep` protects the claim only on `helm uninstall`.
Argo CD renders the chart itself and never runs `helm uninstall`, so that
annotation does nothing under Argo. Two Argo behaviours matter more:

- **Pruning.** If the claim leaves the rendered set, a sync can delete it.
- **Deleting the Application.** With Argo's resources finalizer
  (`resources-finalizer.argocd.argoproj.io`), deleting the Application deletes
  every resource it manages, including this claim.

The chart adds `argocd.argoproj.io/sync-options: Delete=false` to the claim. That
stops pruning during a sync. It is **not** documented to stop the finalizer
deletion above. Check your Argo CD version before relying on it: delete an
Application in a test namespace and confirm the claim survives.

If losing the cache costs more than a rebuild, do not let the Application own the
claim. Create it outside the chart (for example in your infrastructure manifests),
then install with `devenvCache.create: false` and `devenvCache.existingClaim` naming
it. The chart then wires it in and never manages it.

### Reinstalling after a kept claim

A `helm uninstall` leaves the claim behind, and the claim still carries the Helm
ownership annotations of the release that created it.

- **Same release name and namespace.** Helm sees the annotations match this
  release and reuses the claim. Check with `kubectl get pvc` before you rely on it.
- **A different release name, or a different namespace.** Helm refuses with
  `invalid ownership metadata`, because the annotations name the other release.
  Do not remove the annotations by hand. Name the claim instead:

  ```yaml
  devenvCache:
    create: false
    existingClaim: coordinare-devenv-cache
  ```

  Helm then never touches the claim. Its old annotations are harmless.
- **You want the cache gone.** Delete it, then install. This discards the
  toolchains, and the next install creates an empty claim:

  ```bash
  kubectl -n coordinare-performers delete pvc -l app.kubernetes.io/instance=coordinare
  ```

### Resetting the performer cache

The cache has two halves, and both must be reset together.

1. The **devenv claim** holds the toolchains.
2. The **daemon's copy** on the state volume, `env-caches/<sanitised-name>/`, holds
   the rendered `activate.sh`, `verify.sh` and `manifest.json`.

If you delete or recreate only the devenv claim, the daemon's copy stays. The
daemon can then treat the cache as ready and consumers mount an empty subPath.
Reset both.

Set `NS` to the release namespace and `PNS` to the performer namespace. The
examples use `coordinare` as the release name, so the state claim is
`state-coordinare-0` and the StatefulSet is `coordinare`.

```bash
# 1. Stop the controller, so nothing rewrites the state volume.
kubectl -n NS scale statefulset coordinare --replicas=0
kubectl -n NS wait --for=delete pod/coordinare-0 --timeout=120s

# 2. Remove the daemon's env-cache copy from the state volume, using a short-lived Pod.
kubectl -n NS run env-reset --restart=Never --image=busybox:1.36 \
  --overrides='{"spec":{"containers":[{"name":"env-reset","image":"busybox:1.36","command":["rm","-rf","/var/lib/coordinare/env-caches"],"volumeMounts":[{"name":"state","mountPath":"/var/lib/coordinare"}]}],"volumes":[{"name":"state","persistentVolumeClaim":{"claimName":"state-coordinare-0"}}]}}'
kubectl -n NS wait --for=jsonpath='{.status.phase}'=Succeeded pod/env-reset --timeout=120s
kubectl -n NS delete pod env-reset

# 3. Delete the devenv claim. No performer Pod may still mount it, or it stays Terminating.
kubectl -n PNS get pods
kubectl -n PNS delete pvc -l app.kubernetes.io/instance=coordinare

# 4. Start the controller again. Helm or Argo re-creates the claim empty.
helm upgrade coordinare deploy/helm/coordinare -n NS --reuse-values
```

Step 3 selects by the release label, so it reaches only a claim the chart created.
If you wired the claim with `existingClaim`, the claim is yours, not the chart's:
remove it by its name in place of the label selector in step 3. Step 4
recreates a claim only if the chart creates it. For an `existingClaim`, re-create
the claim yourself before step 4.

The daemon's state snapshot still records, per symphony, the last verified bootstrap
and its frozen cache fingerprint (`env_cache` in `coordinare.state.json`). These steps
do not clear it, and they do not need to: on Kubernetes every consumer checks the
mounted cache against that fingerprint before using it (#265), so the first dispatch
after a reset is refused and a fresh bootstrap is requested. To rebuild straight away
instead of waiting for that first dispatch, trigger the bootstrap with
`POST /api/symphonies/<name>/env-bootstrap`, which also resets the retry budget (#270).

The persistent event store uses SQLite with a rollback journal. If the state
volume is NFS, it relies on working NFS locking. Local disk is recommended for
the state volume.

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

**The performer cache claim also survives**, when the chart created it. It is
annotated `helm.sh/resource-policy: keep`, because a rebuild costs a full
bootstrap per symphony. Remove it in the performer namespace, and only when you
mean to:

```bash
kubectl -n coordinare-performers delete pvc -l app.kubernetes.io/instance=coordinare
```

A claim you supplied with `existingClaim` is not labelled by this release, so
Helm never touches it. Delete it yourself.

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
name; never put the token into `config` or a committed values file.

**If the variable is not set, the daemon refuses to start.** The chart cannot
catch this at install time: the chart never reads the value (the Secret reaches the
pod through `envFrom`, not through `values.yaml`), so Helm has nothing to check. The
daemon checks instead. An unset `${COORDINARE_DASHBOARD_AUTH_TOKEN}` stays literal
after expansion, and the daemon refuses it at boot, naming the variable
(`dashboard_auth_token still holds ${COORDINARE_DASHBOARD_AUTH_TOKEN}: the environment
variable COORDINARE_DASHBOARD_AUTH_TOKEN is not set`). The same holds for
`${GITHUB_TOKEN}` and an OIDC client secret. Before installing, check the Secret has
the entry: `kubectl get secret <name> -o jsonpath='{.data.COORDINARE_DASHBOARD_AUTH_TOKEN}'`
must print a value. The pod's log then shows the refusal if it does not. Connect through
port-forwarding, then use username `operator` and the token as the browser password.
The Service stays ClusterIP. For ingress/SSO and TLS, follow
[dashboard authentication](../../../docs/security/dashboard-auth.md).

### Performer cleanup ownership

The chart sets `kubernetes_owner` to a stable hash of the Helm release namespace
and name unless explicitly configured. Keep this stable across restarts and unique among deployments sharing
one performer namespace. For daemons launched outside Helm, set
`kubernetes_owner` in `config.yaml` to a unique Kubernetes label value.

Startup cleanup and session sweeps delete only pods carrying the deployment's
owner label. Without an owner, these sweeps skip deletion. Existing pods without
an owner label are preserved on upgrade; inspect them before removing them
manually. Newly dispatched performers receive the owner label automatically and use owner-specific
pod names. A conflicting pod is replaced only when its owner label can be verified. Without an owner, name collisions fail without
deleting the existing pod.

## Bootstrap verifier image

Set `config.kubernetes_verifier_image` to the trusted performer image used for clean verification and provenance
rechecks. The chart defaults it to `config.performer_image`, or `coordinare-performer:full` when that is omitted.
Symphony endpoint images cannot override it. `performers.imagePullSecrets` also applies to verifier pods.
Roll daemon, performer and verifier images together. See the [environment cache deployment notes](../../../docs/onboarding/06-env-cache.md#kubernetes-bootstrap-verifier)
for schema v38, claim permissions for UID/GID 10001, pod security settings and first bootstrap behavior.
