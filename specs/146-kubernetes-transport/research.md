# Spike findings: coordinare performers on Kubernetes

**Date**: 2026-08-28 | **For**: issue #200 (spec 146, Kubernetes transport)
**Environment**: kind v0.33.0, Kubernetes v1.37.0, cluster `coordinare-dev`, single node

Everything below was **run**, not reasoned about.

## What works out of the box

| Question | Result |
|---|---|
| Does the performer image run unmodified as a Pod? | **Yes** — `coordinare-performer:base` reaches Ready |
| Does it serve its HTTP contract? | **Yes** — `/status` → 200 `{"availability":"idle",...}` |
| Does it need `docker.sock`? | **No** |
| Does it need privileged / `NET_ADMIN`? | **No**, in the base case (see below) |
| Can another pod reach it? | **Yes** — `10.244.0.5:8088/status` from a separate pod |
| Does pod delete map to coordinare's `stop()`? | **Yes** — completes in ~1.2s with a 10s grace period |

The performer image's entrypoint is already `python -m performer --serve --port 8088`, so the
existing HTTP performer contract carries over unchanged. This is the single biggest de-risking
result: **no image changes are needed.**

## Correction to the issue's assumption: Pod, not Job

The issue says "spawn performer as a Job (or bare Pod)". The evidence says **bare Pod**.

`performer_lifecycle.stop()` explicitly runs `docker stop` — the performer is a **long-running
server that coordinare terminates**, not a batch task that runs to completion. Under a Job:

- the container never exits 0 on its own, so the Job never completes naturally;
- coordinare's termination reads as a failed pod, and Job backoff would **restart it**, spawning
  a second performer for a task coordinare believes it has ended.

A bare Pod with `restartPolicy: Never` and an ownerReference for garbage collection matches the
actual lifecycle. If a Job is wanted for bookkeeping, it needs `backoffLimit: 0` and the kill
path has to be understood as the normal exit.

## `NET_ADMIN` is conditional, and Kubernetes has a better answer

`performer_lifecycle.py:150` adds `--cap-add NET_ADMIN` **only when `config.egress_allowlist` is
set**, to install iptables egress rules inside the container. So:

- **Base case**: no capabilities. Runs under the restricted Pod Security Standard as-is.
- **Egress-restricted case**: in-container iptables would need
  `securityContext.capabilities.add: ["NET_ADMIN"]`, which restricted PSS forbids.

**On Kubernetes the native answer is a NetworkPolicy**, applied by coordinare alongside the pod.
It needs no capability, cannot be tampered with from inside the container (unlike in-container
iptables, which the workload itself could flush), and survives restricted PSS. This is a case
where the k8s implementation should be *better* than the Docker one rather than a port of it.

Caveat: NetworkPolicy requires a CNI that enforces it. kind's default (kindnet) does **not**;
testing this needs Calico or Cilium in the kind cluster. That is a real gap to plan for.

## RBAC: verified minimal and sufficient

Applied and checked with `kubectl auth can-i --as=system:serviceaccount:default:coordinare`:

```yaml
rules:
  - apiGroups: [""]
    resources: [pods]
    verbs: [create, get, list, watch, delete]
  - apiGroups: [""]
    resources: [pods/log]
    verbs: [get]
```

| Check | Result |
|---|---|
| create / get / delete pods | yes |
| get pods/log | yes |
| read secrets | **no** |
| list nodes (cluster-scoped) | **no** |

Namespace-scoped, no cluster-scoped permissions, no secret access. Satisfies acceptance
criterion 3 as written.

## Open questions the spike surfaced

1. **Image distribution.** `base` has `backends: []` — it carries no AI backends. Real work needs
   `full` (4.07 GB) or `extra` (5.11 GB). In-cluster that means either a registry the cluster can
   pull from, or `kind load` for local dev. Pull strategy and `imagePullSecrets` need deciding;
   `imagePullPolicy: Never` works for kind but not for a real cluster.
2. **Env-cache volumes.** The Docker path reuses a cache directory across runs. On k8s that is a
   PVC per symphony, or an explicit cold-cache-per-run fallback. Untested here.
3. **NetworkPolicy CNI.** As above — kindnet will silently not enforce policies, so a test that
   "passes" against it proves nothing. Needs Calico/Cilium to test honestly.
4. **Log capture.** `stop()` currently runs `docker logs` into a host directory before removal.
   The k8s equivalent is reading `pods/log` before delete — permission already in the Role above,
   but the ordering matters since logs vanish with the pod.

## Reproducing this

```bash
kind create cluster --name coordinare-dev
kind load docker-image coordinare-performer:base --name coordinare-dev
kubectl --context kind-coordinare-dev apply -f <pod manifest with imagePullPolicy: Never>
kubectl --context kind-coordinare-dev exec performer-spike -- \
  python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8088/status').read())"
```

Cluster `coordinare-dev` is left running, with the ServiceAccount and Role applied in `default`.
Tear down with `kind delete cluster --name coordinare-dev`.

---

## The issue's premise is wrong (architectural correction)

Issue #200 states: *"The seam already exists — `agent_transport: kubernetes` is an accepted config
literal and `__main__.py:415,709` instantiates `KubernetesTransport` — but the class is a 9-line
`NotImplementedError` stub."*

That seam is real but it is **the wrong one**. `KubernetesTransport` implements the
`AgentTransport` protocol, whose entire surface is:

```python
async def send(message: ProtocolMessage, ...) -> ProtocolResponse
```

That is the **subprocess wire protocol**. HTTP performers never use it. The path that actually
runs a containerised performer is:

```
http_performer_service.py:33   from coordinare.services import performer_lifecycle
                        :316   await performer_lifecycle.start_ephemeral(...)
                        :338   await performer_lifecycle.wait_ready(...)
                        :722   await performer_lifecycle.stop(...)
```

A direct module import, hard-wired to Docker. **There is no abstraction to implement against.**

**Consequence for scope**: the work is not "fill in a 9-line stub". It is to introduce a lifecycle
seam, move Docker behind it without behaviour change, and add Kubernetes alongside. Larger than
the issue implies, and much cheaper to discover now than three days in.

**Consequence for the codebase**: the stub is actively harmful and should be deleted (FR-004). A
file named `kubernetes_transport.py` sitting in `transport/` is what led the issue to describe the
wrong seam; leaving it there guarantees someone repeats the mistake.

**The good news**, from the same reading: `StartedContainer.endpoint` is already an opaque URL.
Kubernetes returns `http://<pod-ip>:8088` and every downstream consumer is unchanged. The seam is
close to the shape the code already has.

## Image size: considered and rejected as a problem

The performer image (`coordinare-performer:full`, the configured default) is 4.07 GB. Layers:

| Layer | Size |
|---|---|
| Playwright + Chromium | 1.5 GB |
| pip packages | 391 MB |
| npm globals | 202 MB |
| apt | 42 MB |

An early proposal was a browser-less variant for non-QA roles. **Rejected on the grounds that a
performer is a developer workstation**: an implementer that cannot open a browser is missing a
capability at the moment it needs one, and the trade is a one-time cost for a permanent gap.

The cost is also smaller than first stated. The pull is **once per node**, not once per performer;
subsequent performers on that node start from the node's image cache. It is a cold-start cost on
first schedule, not a recurring tax. `imagePullPolicy: IfNotPresent` plus a documented pre-pull
DaemonSet is the whole mitigation.
