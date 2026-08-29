# Tasks: Helm Deployment — coordinare-controller In-Cluster

**Feature**: 147-helm-deployment | **Issue**: #201 | **Branch**: `147-helm-deployment`
**Inputs**: [spec.md](./spec.md), [plan.md](./plan.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/values-contract.md](./contracts/values-contract.md)

**Scope guardrails for every task**: do not alter spec-146 runtime behaviour; do not change any
existing workflow's triggers, required status, or runtime; no push, PR, or board move without
explicit approval.

## Phase 1: Setup

- [x] T001 Create the chart skeleton at `deploy/helm/coordinare/` — `Chart.yaml` (name `coordinare`, apiVersion v2, chart version 0.1.0, appVersion matching the pinned image), `.helmignore`, and empty `templates/` — so `helm lint` has something to run against.
- [x] T002 Verify `helm`, `kind` and `kubectl` are available locally and record the versions in the task notes; these are the tools every later phase depends on.

## Phase 2: Foundational — tests before templates (Constitution II, BLOCKS Phase 3)

Each test below MUST be written and MUST FAIL before the corresponding template exists.

- [x] T003 Create `tests/unit/test_147_helm_deployment.py` with a `_render(**values)` helper that shells out to `helm template`, parses the multi-document YAML, and returns objects indexed by kind — plus a module-level skip that names the install command if `helm` is absent. Verify it fails (no chart templates yet).
- [x] T004 [P] Test FR-001/FR-002: the rendered workload is a single-replica `StatefulSet`; `replicaCount: 2` fails the render; the failure message names the data-loss consequence; `allowUnsafeMultiReplica: true` permits it. Verify it FAILS.
- [x] T005 [P] Test FR-003 (highest-consequence, per research R9): the rendered `state_file_path` is **absolute** and under `state.mountPath`, and the StatefulSet mounts a claim there. Verify it FAILS.
- [x] T006 [P] Test FR-006/FR-007: render with sentinel credential values; assert each sentinel appears **only** inside a `Secret` and in no `ConfigMap` or other object, and that the rendered `config.yaml` references `${VAR}` names rather than values. Verify it FAILS.
- [x] T007 [P] Test FR-008: with `existingSecret` set, no `Secret` is rendered and the StatefulSet's `envFrom` references the named one. Verify it FAILS.
- [x] T008 [P] Test FR-009: the chart's `Role` verbs equal `deploy/kubernetes/rbac.yaml`'s exactly, and `watch` is absent from both. Verify it FAILS.
- [x] T009 [P] Test FR-010/FR-011: the performer namespace defaults to the release namespace and is overridable; the controller's ServiceAccount token is **not** suppressed (that suppression belongs to performer Pods only). Verify it FAILS.
- [x] T010 [P] Test FR-012/FR-013: liveness uses `/live` and readiness `/ready` on the health port; nothing exposes `/metrics` by default. Verify it FAILS.
- [x] T011 [P] Test FR-014/FR-015/FR-016: no `Ingress` renders by default; a ClusterIP `Service` does; `trusted_dashboard_hosts` contains exactly the release's Service DNS name and no wildcard or bind-all address; the entry is present in `values.yaml` as shipped. Verify it FAILS.
- [x] T012 [P] Test FR-005: `image.tag` is neither empty nor `latest`, and the rendered image reference is fully pinned. Verify it FAILS.
- [x] T013 [P] Test contract "Optional means optional": rendering with `performers.cacheClaim` empty produces a valid StatefulSet with no cache volume — the no-StorageClass path (SC-008). Verify it FAILS.
- [x] T014 Create `tests/integration/test_147_helm_install.py` with a cluster-availability skip whose reason names `kind create cluster --name coordinare-dev` (spec-146 skip discipline, FR-023). No install logic yet.

## Phase 3: User Story 1 + 2 — install, and keep secrets out (P1, MVP)

- [x] T015 Write `deploy/helm/coordinare/templates/_helpers.tpl`: name/fullname/labels helpers and the Service DNS-name helper that both `service.yaml` and the trusted-host value depend on.
- [x] T016 Write `deploy/helm/coordinare/values.yaml` per data-model's values table, with `dashboard.trustedHosts` **visible here** (FR-016) and each security-relevant default carrying a one-line comment saying why it is the default.
- [x] T017 Write `templates/rbac.yaml`: ServiceAccount, Role (`pods` create/get/list/delete, `pods/log` get — no `watch`), RoleBinding, scoped to the performer namespace. Makes T008, T009 pass.
- [x] T018 Write `templates/configmap.yaml` rendering `config.yaml` with `${VAR}` placeholders, `state_file_path` under the mount, `dashboard_host: 0.0.0.0`, `trusted_dashboard_hosts` from values, and the spec-146 `kubernetes_*` fields. Makes T005 pass.
- [x] T019 Write `templates/secret.yaml`, skipped entirely when `existingSecret` is set. Makes T006, T007 pass.
- [x] T020 Write `templates/statefulset.yaml`: replicas with the `fail` guard and its explanation, `volumeClaimTemplate` for state, `envFrom`, ConfigMap and app-key mounts, probes, pinned image. Makes T004, T010, T012, T013 pass.
- [x] T021 Write `templates/service.yaml` (ClusterIP) and `templates/ingress.yaml` (disabled, with the no-authentication warning). Makes T011 pass.
- [x] T022 Write `templates/NOTES.txt`: the port-forward command, the single-replica note, and a one-line statement that the dashboard is unauthenticated.
- [x] T023 Run `helm lint` and the full T003-T013 set; all green.

## Phase 4: The daemon image (P1 — the chart is not installable without it)

- [x] T024 Refresh `Dockerfile.daemon`: correct `EXPOSE` to the real ports (8080 health, 8090 dashboard — it currently says 9090/9091), current Python base, run as a non-root user where practical, and **no docker.sock assumption**. Record why in a header comment.
- [x] T025 Add a `build-daemon` job to `.github/workflows/main-branch-build.yml` mirroring `build-base`/`build-full` exactly — same GHCR registry, same CalVer from `Derive Version`, same permissions block — and add the daemon image to `Tag Latest`. Do not touch any existing job's triggers, `needs`, or required status (FR-020, SC-010).
- [x] T026 [P] Test that the new job does not alter existing workflow triggers or required checks: parse the workflow YAML and assert the pre-existing jobs' `on:`, `needs:` and names are unchanged from `main`.
- [x] T027 Build the daemon image locally and confirm it starts and serves `/live`, so the chart is not the first place the image is exercised.

## Phase 5: User Story 4 + live verification — upgrade and state

- [x] T028 Implement the integration install test (T014): `helm install` into a kind cluster, controller reaches Ready, `/ready` answers.
- [x] T029 Integration: dispatch reaches a performer **Pod** (not a Job) in the configured namespace under the chart's RBAC — SC-002, and the spec-146 tie-in.
- [x] T030 Integration: delete the controller Pod, confirm it reschedules and resumes from the persisted snapshot — SC-004 first half.
- [x] T031 Integration: `helm upgrade` retains the same state claim and the daemon resumes — SC-004 second half, FR-004.
- [x] T032 Integration: exercise SC-008 honestly. kind ships a *default* StorageClass (`local-path`), so remove the default annotation for the duration of the test, then install with `performers.cacheClaim` empty and `state.storageClass` named explicitly. Assert the install succeeds. Restore the annotation afterwards. Testing this against kind's defaults unchanged would assert nothing.

## Phase 6: Documentation

- [x] T033 Write `deploy/helm/coordinare/README.md` (FR-021): install; the trust boundary in spec-144's voice; dashboard access **and the exact trusted-host entry the chart adds, what it widens, and why**; the single-replica constraint and its reason; the optional performer cache; upgrade and the `CURRENT_SCHEMA_VERSION` snapshot-compatibility promise; that the state claim survives uninstall.
- [x] T034 [P] Add a pointer from `deploy/kubernetes/README.md` to the chart, so the raw-manifest path and the Helm path are discoverable from each other.
- [x] T035 [P] Test FR-021/SC-006: the chart README names the trusted-host widening and states the dashboard is unauthenticated — the claim must be findable by an operator, not only true in the templates.

## Phase 7: CI

- [x] T036 Add chart lint + render assertions to `.github/workflows/pr-ci.yml` as steps in the existing test job or a new fast job — no cluster needed. Must not change existing job triggers or required status.
- [x] T037 Add a kind-based install job to `pr-ci.yml` that explicitly installs `kind` and `helm` rather than assuming the self-hosted runner has them (research R7). Keep it **non-required** until it has proven reliable; an unreliable required check trains people to ignore red.

## Phase 8: Polish and verification

- [x] T038 Run the full suite (`.venv/bin/pytest tests/unit tests/contract`) plus `make lint`; all green.
- [x] T039 Re-read SC-001 through SC-010 against what actually shipped and record any that are not literally met, with the reason — spec 146's SC-003 caveat is the precedent for stating this rather than glossing it.
- [x] T040 Scope check: `git status` shows only chart, image, workflow, test, spec and doc files; no spec-146 runtime file modified.
- [x] T041 Commit code and `specs/147-helm-deployment/` together, conventional message, `Closes #201`. No push or PR without explicit approval.

## Dependencies

```
Phase 1 (T001-T002)
   └─> Phase 2 tests (T003-T014)      [BLOCKS Phase 3 — tests must fail first]
          └─> Phase 3 chart (T015-T023)
                 ├─> Phase 4 image (T024-T027)   [chart not installable without it]
                 │      └─> Phase 5 live (T028-T032)
                 └─> Phase 6 docs (T033-T035)
                        └─> Phase 7 CI (T036-T037)
                               └─> Phase 8 (T038-T041)
```

Phase 4 can begin in parallel with Phase 3 (different files), but Phase 5 needs both.

## Parallel opportunities

- T004-T013 are independent test functions in one new file; write them in one pass.
- T024 (Dockerfile) and T015-T022 (templates) touch disjoint files.
- T034 and T035 are independent of each other.

## MVP

Phases 1-4 deliver the MVP: a chart that installs a real, published image with state on a volume
and no leaked credentials. Phase 5 proves it against a live cluster; 6-8 make it usable and keep
it honest.
