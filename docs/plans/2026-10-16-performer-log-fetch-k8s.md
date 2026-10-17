# env_cache.bootstrap_terminal_failure log fetch uses docker binary on kubernetes transport; performer traceback lost

Issue #344

## Scope

In: the daemon's bootstrap log fetch becomes transport-aware. `_snapshot_container_logs`
and `_bootstrap_failure_logs` in `src/coordinare/daemon.py` read the performer's log tail
through the service's runtime (`PerformerRuntime.tail_logs`, already part of the protocol
and implemented by both `DockerRuntime` and `KubernetesRuntime`) instead of shelling out to
the `docker` binary. On the kubernetes transport this is the kubernetes `pods/log` API, so
`logs_tail` on `env_cache.bootstrap_terminal_failure` carries real performer pod output,
including the traceback, fetched before the pod is deleted. Services that expose no runtime
seam keep the legacy docker-direct path, so docker-transport behaviour is preserved.

Out: no change to `HTTPPerformerService` teardown ordering (the pod is already deleted
inside `check_status` on the terminal poll, which is exactly why the pre-poll snapshot in
the bootstrap poll loop is the fetch that matters); no change to the run-log capture (132);
no chart or config changes.

## Assumptions

- `tail_logs` is on the `PerformerRuntime` protocol and is best-effort by contract: it
  returns `[]` rather than raising when the handle is gone (pod deleted) or the API call
  fails. Both production runtimes implement it, so the daemon never needs to know which
  transport it is on, only whether the service exposes a runtime.
- The `<docker logs failed: ...>` placeholder disappears on the failure path where
  `tail_logs` returns empty; the existing `get_agent_logs` last-resort fallback still runs,
  which is a strict improvement over emitting the placeholder.
- Access to the runtime follows the established pattern in `_bootstrap_failure_logs`
  (`getattr(svc, "_active_jobs", ...)`): `getattr(svc, "_runtime", None)`.
- `_docker_logs_lines` stays only for services without a `_runtime` attribute (test fakes
  and any non-runtime service); production `HTTPPerformerService` always has one, and its
  docker runtime runs the same `docker logs --tail` subprocess.

## Tasks

- [ ] 1. Red: unit tests in `tests/unit/test_564_bootstrap_failure_logs_k8s.py` proving
  `_bootstrap_failure_logs` and `_snapshot_container_logs` fetch through the service
  runtime's `tail_logs` (kubernetes-shaped fake) and never invoke the docker path; watch
  them fail for the right reason.
- [ ] 2. Green: minimal change in `src/coordinare/daemon.py` — both functions prefer
  `runtime.tail_logs`, poll loop passes `svc` into `_snapshot_container_logs`.
- [ ] 3. Regression: full unit suite, lint and typecheck green (preflight).
