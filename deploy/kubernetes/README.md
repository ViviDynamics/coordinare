# Running coordinare on Kubernetes

Runs performers as Pods instead of local Docker containers, so the daemon never
needs `/var/run/docker.sock`.

**That is the point.** Mounting the Docker socket is root-equivalent access to the
host, and it is the largest single trust liability in the Docker deployment (see
[the threat model](../../docs/security/threat-model.md)). On Kubernetes the daemon
needs a namespace-scoped ServiceAccount and nothing else.

## Two ways in

This directory is the **raw-manifest** path: apply `rbac.yaml`, run the daemon
however you like, and point it at the cluster.

If you want coordinare itself running in-cluster, use the **Helm chart** at
[`deploy/helm/coordinare`](../helm/coordinare/README.md) instead. It templates this
same RBAC, adds the StatefulSet, state volume, config and credential wiring, and
is the shorter path to a working deployment.

## Requirements

Any conformant cluster: EKS, GKE, vanilla Kubernetes, k3s, microk8s, minikube,
kind. Only stable core APIs are used — no custom resources, no vendor extensions,
no assumption that a StorageClass exists or that your CNI enforces NetworkPolicy.

## Setup

```bash
kubectl create namespace coordinare
kubectl apply -n coordinare -f deploy/kubernetes/rbac.yaml
```

That grants exactly: `pods` (create, get, list, delete) and `pods/log`
(get), in one namespace. No ClusterRole, no access to secrets.

Check what the cluster actually permits rather than what the file says, because
a Role left behind under an old name stays bound and keeps granting:

```bash
kubectl auth can-i --list --as=system:serviceaccount:coordinare:coordinare -n coordinare
```

**Performer Pods hold no cluster credential.** Kubernetes mounts a ServiceAccount
token into every Pod by default; coordinare sets `automountServiceAccountToken:
false` on the Pods it creates, so the token path does not exist inside a
performer. This matters more here than it would elsewhere: a performer runs
AI-generated code, and a mounted token would hand that code a live API
credential at a well-known path.

Then in your config:

```yaml
agent_transport: kubernetes
kubernetes_namespace: coordinare
# kubernetes_image_pull_secrets: [regcred]     # if your registry needs auth
# kubernetes_cache_claim: devenv-cache         # optional, see Caching
```

## Image distribution

**The performer image is about 4 GB**, and that is deliberate: a performer is a
developer workstation. Playwright and Chromium alone account for 1.5 GB, and an
implementer that cannot open a browser is missing a capability at the moment it
needs one. A role-specific slim variant was considered and rejected for that
reason.

**The pull happens once per node, not once per performer.** After the first pull
on a node, every subsequent performer scheduled there starts from that node's
image cache — `imagePullPolicy: IfNotPresent` is set for exactly this. So the
cost is a cold start on first schedule, not a recurring tax.

If that first dispatch matters to you, pre-pull with a DaemonSet:

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: performer-image-prepull}
spec:
  selector: {matchLabels: {app: performer-image-prepull}}
  template:
    metadata: {labels: {app: performer-image-prepull}}
    spec:
      initContainers:
        - name: prepull
          image: ghcr.io/vividynamics/coordinare-performer-full:latest
          command: ["true"]
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.9
```

## Caching

`kubernetes_cache_claim` names a PersistentVolumeClaim mounted at `/devenv` so the
environment cache survives between runs.

**It is optional on purpose.** Left unset, performers run with a cold cache. That
is slower, not broken — and it is what makes this work on clusters with no default
StorageClass, which describes most minikube and microk8s installs. Requesting a
PVC unconditionally would fail on exactly the small clusters this is meant to
support.

## Egress control is not available here

The Docker path can restrict a performer's egress with an allowlist, implemented
as iptables rules inside the container via `NET_ADMIN`.

**That does not carry over, and nothing silently replaces it.** The Helm chart now
offers an opt-in NetworkPolicy (`performers.egress.enabled`); this raw-manifest path
does not, and neither gives you hostname-level control.

**Support is often partial, not absent.** Measured on kind: kindnet enforces *ingress* but not *egress*, so a policy applied there is silently inert in exactly the direction that matters here. That is why the question to ask is not "does my CNI support NetworkPolicy" but "does it enforce the direction I am relying on" — and `coordinare doctor --check egress` measures it rather than inferring it from the CNI's name. The
Kubernetes-native equivalent is a NetworkPolicy, which only does anything if your
CNI enforces them — kind, minikube and microk8s defaults often do not. Shipping a
policy that quietly fails to apply would be worse than shipping none, because you
would believe you were protected.

If you need egress restriction, apply your own NetworkPolicy against the
`app.kubernetes.io/managed-by=coordinare` label, having confirmed your CNI enforces
it.

## What coordinare creates

One Pod per performer, labelled:

| Label | Value |
|---|---|
| `app.kubernetes.io/managed-by` | `coordinare` |
| `coordinare.vividynamics.com/performer-id` | the performer's id |

Pods use `restartPolicy: Never`. A performer is a long-running server that
coordinare terminates when the work is done; a restart would resurrect one
coordinare has already finished with. This is also why performers are Pods rather
than Jobs — a Job would read coordinare's termination as a failure and back off
into starting a second performer for work that is already over.

Logs are captured before a Pod is deleted, because Pod logs are unreachable the
moment it is gone.

## Where the daemon runs

**In-cluster is the supported deployment.** Coordinare reaches each performer at
`http://<pod-ip>:8088`, which works from inside the cluster.

Running the daemon on a laptop against a remote cluster is a development
convenience, not a supported mode: **Pod IPs are generally not routable from
outside the cluster**, so coordinare cannot reach the performers it starts. The
integration test works around this with `kubectl port-forward`, which is fine for
a test and not a way to run in production.

## Verifying

```bash
kubectl -n coordinare get pods -l app.kubernetes.io/managed-by=coordinare
kubectl -n coordinare logs <pod>
kubectl auth can-i --list --as=system:serviceaccount:coordinare:coordinare -n coordinare
```

The last one should show `pods` and `pods/log` and nothing else. If it shows
more, something has granted permissions this does not need.

## Known limits

- Egress allowlisting is unavailable, as above.
- The daemon must run in-cluster to reach performers.
- Portability is by construction — core APIs only — but has been exercised on
  kind. EKS, GKE and microk8s have not been run against.
