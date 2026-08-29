---
description: "Task list for Kubernetes transport (Spec 146, issue #200)"
---

# Tasks: Kubernetes Transport

**Prerequisites**: spec.md, plan.md, research.md
**Tests**: Requested (Constitution II).
**Branch**: `146-kubernetes-transport`

## Scope guardrails

- **Existing Docker behaviour must not change.** Its tests pass **unmodified** — that is the
  evidence, so editing one is a signal the refactor went wrong, not a fix.
- Standard core Kubernetes APIs only. No CRDs, no vendor extensions.
- Assume no StorageClass, no NetworkPolicy enforcement, no LoadBalancer.
- Do not claim egress control on Kubernetes.
- No push, PR, or board move without explicit approval.

---

## Phase 1: The seam (blocks everything)

- [x] T001 (FR-001) Create `src/coordinare/services/performer_runtime.py`: a `PerformerRuntime` Protocol mirroring the existing surface — `start_ephemeral`, `wait_ready`, `stop`, `cleanup_orphaned` — plus a `StartedPerformer` dataclass with an opaque `handle` and an `endpoint` URL. Deliberately **not** named `StartedContainer`: Kubernetes does not return a container, and carrying the word forward is how the next reader is misled.
- [x] T002 (FR-002) Create `src/coordinare/services/docker_runtime.py` implementing the Protocol by delegating to the existing `performer_lifecycle` functions. **Move, do not rewrite.** The exception types stay where they are so existing `except` clauses keep working.
- [x] T003 [P] In `tests/unit/test_146_kubernetes_transport.py`, assert `docker_runtime` satisfies the Protocol structurally and that `StartedPerformer.endpoint` round-trips what `StartedContainer.endpoint` did.
- [x] T004 (FR-003) Change `http_performer_service.py` to accept a `PerformerRuntime` rather than importing `performer_lifecycle` (currently line 33, used at 316/338/722). Default to the Docker runtime so nothing changes for existing deployments.
- [x] T004a (FR-002) Confirm the other importers of `performer_lifecycle` are unaffected: `src/coordinare/bench/runner.py`, `src/coordinare/__main__.py`, and the five test modules that import it directly. The module stays; `docker_runtime` wraps it. If any of them needed changing, the wrap is wrong.
- [x] T005 (SC-003) **Run the full existing suite.** Any Docker-path failure means the extraction changed behaviour — fix the extraction, never the test (FR-002, SC-003).

**Checkpoint**: the seam exists, Docker runs through it, nothing observable changed.

---

## Phase 2: Kubernetes runtime (US1)

### Tests first

- [x] T006 [P] Pod-manifest construction: correct image, `imagePullPolicy: IfNotPresent` (FR-013), `restartPolicy: Never`, port 8088, env, labels tying the Pod to its performer, and an ownerReference so Pods cannot outlive the daemon (FR-008). Verify it FAILS.
- [x] T007 [P] RFC 1123 name mapping (FR-010): performer identifiers become valid Pod names — lowercase, ≤63 chars, no invalid characters, collision-resistant, and traceable back to the performer. Include identifiers that are already valid, ones needing truncation, and ones with characters Docker allows but Kubernetes does not. Verify it FAILS.
- [x] T008 [P] Failure classification (FR-009): eviction, OOMKilled, node loss and image-pull failure each map to an **existing** error classification. Assert no new terminal state is introduced — this is the requirement most likely to be satisfied by accident and then quietly broken.
- [x] T009 [P] Endpoint derivation: a running Pod yields `http://<pod-ip>:8088`, matching what the spike proved reachable.

### Implementation

- [x] T010 Add the Kubernetes client dependency; note in `pyproject.toml` why (hand-rolling API-server auth, in-cluster token handling and watch semantics is worse than a maintained client).
- [x] T011 (FR-005, FR-006) Create `src/coordinare/services/kubernetes_runtime.py` implementing the Protocol: create a **bare Pod, not a Job** (research: the performer is a long-running server the daemon terminates; a Job would read the kill as failure and backoff-restart it, producing a second performer for work the daemon believes ended).
- [x] T012 Implement `wait_ready` by polling the performer's own `/status` endpoint, not merely Pod readiness. A Ready Pod whose HTTP service has not bound is exactly the race that produces a confusing first failure.
- [x] T013 Implement `stop`: **capture `pods/log` before deleting** (FR-007) — logs vanish with the Pod, so the ordering is the requirement.
- [x] T014 Implement `cleanup_orphaned`: find Pods by coordinare's label and remove those whose owner is gone.
- [x] T015 Wire runtime selection from `agent_transport` config; **delete `src/coordinare/transport/kubernetes_transport.py`** and its import in `__main__.py:59,433` (FR-004).

---

## Phase 3: Portability and permissions (US2)

- [x] T016 [P] Assert no CRDs, no vendor API groups, and no cluster-scoped resources are referenced (FR-011).
- [x] T017 (FR-012, SC-004) Make persistent caching optional: with no usable StorageClass, run with a cold cache rather than failing. Test the no-StorageClass path explicitly — it is the default on the small clusters this must support.
- [x] T018 (FR-015) Write `deploy/kubernetes/rbac.yaml`: ServiceAccount, Role (`pods` create/get/list/watch/delete, `pods/log` get), RoleBinding. This exact manifest was verified sufficient in the spike.
- [x] T019 [P] (SC-002) Assert RBAC sufficiency **and non-excess** (FR-016, FR-017): the required verbs are permitted; secrets and cluster-scoped reads are denied. A Role that works because it is over-broad passes the first half of that and fails the point.

---

## Phase 4: Integration against a real cluster

- [x] T020 Write `tests/integration/test_146_kind_e2e.py`: create a performer through the Kubernetes runtime on the live cluster, confirm `/status`, exercise stop, confirm the Pod is gone. **Skip — do not fail — when no cluster is reachable**, with a skip reason naming the command to create one.
- [x] T021 Verify the skip is *visible*: a silently-skipped integration test is how "it works on Kubernetes" stays green while becoming untrue.
- [x] T022 (SC-001, SC-005, SC-006) Run the end-to-end path on the kind cluster and record the result, including timings: a card reaches a performer Pod and completes with no Docker socket involved; logs are captured for a deliberately failed performer; a killed Pod classifies into an existing error state.

---

## Phase 5: Documentation

- [x] T023 (FR-013a, FR-014, FR-018) Write `deploy/kubernetes/README.md`: prerequisites, the RBAC manifest, image distribution (registry, pull secrets, the once-per-node 4 GB cold start and the pre-pull DaemonSet mitigation), the cold-cache fallback, and **the absence of egress control** (FR-014, FR-018).
- [x] T024 Update `docs/security/threat-model.md` (FR-019): the `docker.sock` root-equivalent trust decision **does not apply** on the Kubernetes path — that is this feature's headline security result — and state the Kubernetes path's own boundaries. Note spec 144's test asserting every boundary carries a residual risk; a new boundary needs one too.

---

## Phase 6: Polish

- [x] T025 Full suite: unit, contract, integration, performer. `make lint` clean.
- [x] T026 Re-read the acceptance criteria and record any not met. **SC-007 (portability) is an argument, not a test result** — kind is conformant and only core APIs are used, but no EKS/GKE/microk8s run has happened, and the writeup must say so rather than implying coverage.
- [x] T027 Verify scope: no workflow changes, no Docker-path test edited, no coordinare state schema change.
- [x] T028 Commit code and specs together, Conventional Commits, `Closes #200`. No push or PR without approval.

---

## Dependencies

```
Phase 1 (seam) ─→ Phase 2 (k8s runtime) ─→ Phase 3 (portability/RBAC) ─→ Phase 4 (integration)
                                                                       └─→ Phase 5 (docs)
Phase 6 depends on all.
```

Phase 1 is a genuine blocker: there is nothing to implement against until the seam exists.

## Risks

| Risk | Handling |
|---|---|
| The extraction changes Docker behaviour | T005 gates on the existing suite passing **unmodified** |
| A Ready Pod whose HTTP service has not bound | T012 polls `/status`, not Pod readiness |
| Logs lost on delete | T013 makes ordering the requirement |
| Portability claimed but untested beyond kind | T026 requires saying so plainly |
| The new client dependency | T010 records why; it is the one addition and the alternative is worse |

## Task Summary

| Phase | Tasks | Count |
|---|---|---|
| Seam | T001-T005 | 5 |
| Kubernetes runtime | T006-T015 | 10 |
| Portability + RBAC | T016-T019 | 4 |
| Integration | T020-T022 | 3 |
| Documentation | T023-T024 | 2 |
| Polish | T025-T028 | 4 |
| **Total** | | **28** |
