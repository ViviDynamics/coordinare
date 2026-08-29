# Implementation Plan: Kubernetes Transport

**Branch**: `146-kubernetes-transport` | **Date**: 2026-08-28 | **Spec**: [spec.md](./spec.md)
**Issue**: [#200](https://github.com/ViviDynamics/coordinare/issues/200)

## Summary

Run performers as Kubernetes Pods so coordinare no longer needs `docker.sock`. Three pieces, in
order: extract a runtime-agnostic lifecycle seam, move the existing Docker code behind it
unchanged, and add a Kubernetes implementation alongside.

The spike (see research.md) established the hard part is already solved: the performer image runs
unmodified as a Pod, serves its HTTP contract, and is reachable across pod networking. What is
missing is not a runtime — it is the seam to plug one into.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12).
**New dependency**: the Kubernetes Python client. This is the one addition, and the alternative
(hand-rolling REST calls against the API server, including auth, in-cluster service-account token
handling, and watch semantics) is worse than taking a maintained client.
**Storage**: no coordinare state change. PVCs are cluster-side and optional.
**Testing**: `tests/unit/test_146_kubernetes_transport.py`, plus an integration path exercised
against the live kind cluster.
**Constraints**: standard core APIs only; no CRDs; must not assume a StorageClass, a
NetworkPolicy-enforcing CNI, or a LoadBalancer; existing Docker behaviour must not change.

## Constitution Check

| Principle / Gate | Assessment |
|---|---|
| **I. Code Quality First** | PASS. The refactor is an extraction, not a rewrite: the Docker code moves behind a Protocol with its behaviour and tests untouched. |
| **II. Testing Discipline** | PASS. Unit tests for name mapping, manifest construction, failure classification and RBAC sufficiency; an integration test against kind for the end-to-end path. The Docker suite passing **unmodified** is the evidence for FR-002. |
| **III. UX Consistency** | PASS. `agent_transport: kubernetes` already exists as config; this makes it mean something. Failures classify into existing states rather than new ones. |
| **IV. Performance by Design** | Applies at one point: `IfNotPresent` so nodes reuse cached images. The 4 GB cold start is documented, not engineered around (spec FR-013a). |
| **V. Clarity Before Action** | PASS. Three clarifications resolved with the approver before planning; a spike answered six questions empirically that would otherwise have been assumptions. |
| **Gate 4: Integration Tests** | Applies, and is the interesting one — see Testing Strategy. |
| **Gate 8: Code Review** | Structurally unsatisfiable solo, as recorded for 142/144/145. |

**Result: PASS.**

## The seam

The existing surface is already close to right. `performer_lifecycle` exposes:

```
start_ephemeral(...) -> StartedContainer(container_id, endpoint)
wait_ready(...)
stop(container_id, ...)
cleanup_orphaned_containers(...)
```

`StartedContainer.endpoint` is already an opaque URL (`"http://127.0.0.1:8080"`), which is why the
HTTP client needs no changes: Kubernetes returns `http://<pod-ip>:8088` and everything downstream
is identical.

The Protocol therefore mirrors that surface almost exactly, with `container_id` generalised to an
opaque handle. Renaming `StartedContainer` is deliberate: keeping "container" in the name of the
thing Kubernetes returns is how the next reader ends up confused.

## Project Structure

```text
src/coordinare/services/
├── performer_runtime.py          # NEW  the Protocol + StartedPerformer
├── docker_runtime.py             # NEW  existing performer_lifecycle behind the Protocol
├── kubernetes_runtime.py         # NEW  the Kubernetes implementation
├── performer_lifecycle.py        # UNCHANGED — see below; it must not be removed
└── http_performer_service.py     # EDIT depend on the Protocol, not the module

src/coordinare/transport/
└── kubernetes_transport.py       # REMOVED (FR-004) — misleading stub, wrong seam

deploy/kubernetes/
├── rbac.yaml                     # NEW  ServiceAccount + Role + RoleBinding (verified in spike)
└── README.md                     # NEW  running on Kubernetes

tests/unit/test_146_kubernetes_transport.py   # NEW
tests/integration/test_146_kind_e2e.py        # NEW  skipped unless a cluster is reachable
```

**`performer_lifecycle.py` stays exactly as it is.** An earlier draft of this plan suggested it
might be removed once nothing imported it. Things do import it: `src/coordinare/bench/runner.py`,
`src/coordinare/__main__.py`, and five test modules including
`tests/unit/services/test_performer_lifecycle.py` and `tests/contract/test_docker_label_schema.py`.
`docker_runtime.py` therefore **wraps** it rather than replacing it, which also keeps FR-002 cheap:
if the Docker code is untouched, its tests cannot break, and the benchmark runner is unaffected.

**Structure Decision**: the runtime implementations live in `services/`, beside the code they
replace, not in `transport/`. `transport/` is the subprocess wire protocol; putting a Pod
launcher there is what made issue #200 describe the wrong seam in the first place.

## Testing strategy, and its honest limits

Three layers, because the interesting failures live at different ones:

1. **Unit** — manifest construction, RFC 1123 name mapping, failure classification, endpoint
   derivation. Fast, no cluster.
2. **Integration against kind** — the real end-to-end path. **Skipped, not failed, when no
   cluster is reachable**, so the suite stays green on a machine without one, with the skip
   reason naming what to run. A silently-skipped integration test is how "it works on
   Kubernetes" becomes untrue without anyone noticing, so the skip must be visible.
3. **RBAC sufficiency** — run the operations under the shipped Role via `auth can-i`, proving
   the manifest is both sufficient and not excessive. Verified once in the spike; this makes it
   a standing check.

**What none of this proves**: behaviour on EKS, GKE, or microk8s specifically. kind is a
conformant cluster and the code uses only core APIs, but portability is an argument, not a test
result, and the spec should not claim otherwise.

## Phase 0: Research

See [research.md](./research.md) — the spike findings, and the architectural correction to the
issue's premise.

## Phase 2

`/speckit.tasks` generates `tasks.md`.
