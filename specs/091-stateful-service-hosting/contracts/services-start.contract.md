# Contract: Rendered `services-start.sh` (and `services-health.sh`) behavior

**Branch**: `091-stateful-service-hosting` | **Date**: 2026-06-15

This is the behavioral contract the templater (`coordinare_service_inference.templater.render`)
must satisfy for the extended manifest. It is the test oracle for the golden-render and
idempotency unit tests. The contract is expressed over *rendered script content and
behavior*, not over a live database (tests stay deterministic — Constitution II).

## Shell-safety invariants (all kinds, unchanged from 063 + 087)

- **C-1**: The rendered scripts MUST NOT use `set -e` or `set -u` in a way that aborts a
  sourced shell, and MUST NOT exit non-zero merely on being sourced. Same contract as
  `render_activate_sh`.
- **C-2**: Every value interpolated from the manifest (names, ports, paths, superuser,
  database names, env-var names) MUST be `shq`-quoted in the rendered output.
- **C-3**: No credential value appears in the rendered script, in argv, or in any echoed
  line. The only credential reference is `${<password_env_var>}`, read at runtime. *(FR-011)*

## Generic / redis (no init) — unchanged

- **C-4**: For `kind in {"generic", "redis"}` with `init=None`, the rendered start block MUST
  be byte-for-byte equivalent to the 063 output (mkdir `data_dir` → launch
  `start_args` | `<binary> --port=<port> --data-dir=<data_dir>` in the background). *(FR-013,
  VR-3 — golden test asserts this equivalence.)*

## Postgres (init) — new

- **C-5 — init phase before launch**: For `kind == "postgres"`, the start block MUST run the
  init recipe *before* launching the server: when the init sentinel is absent, `initdb` the
  `data_dir`, then create the `superuser` role, then create each database in
  `init.databases`. *(FR-001)*
- **C-6 — idempotent**: The init recipe MUST be guarded so that, when the sentinel
  (`<data_dir>/PG_VERSION`) is present, `initdb` is skipped. Role and database creation MUST
  be create-if-missing so a re-run never errors and never loses data. *(FR-003, SC-002, D4)*
- **C-7 — convergent on partial init**: A present sentinel MUST NOT short-circuit role/db
  creation; each of superuser and database creation is independently guarded so a run that
  died mid-recipe converges on the next run. *(D4, "partial prior initialization" edge.)*
- **C-8 — connection target drives binding**: The launched server MUST listen on the declared
  `port` on loopback, not the binary default. *(FR-002, VR-4)*
- **C-9 — concurrency guard**: Re-using the existing PID/`_is_running` and `_port_bound`
  guards plus the init sentinel/lock, two concurrent invocations MUST NOT both `initdb` or
  both bind the port; the second no-ops. *(FR-010)*
- **C-10 — writable state**: `data_dir` for an initializing service MUST resolve under the
  writable `$XDG_RUNTIME_DIR` services root, never inside the read-only cache mount. *(FR-009,
  D8)*

## Readiness (`services-health.sh`) — new kind-awareness

- **C-11 — kind-aware probe**: For `kind == "postgres"` the health script MUST probe actual
  connection readiness on the declared connection target (a `pg_isready`-style connect
  probe), not merely that the port is bound — Postgres binds before it is ready to serve.
  `redis`/`generic` retain the existing liveness/port check. *(FR-004, D6.)*
- **C-12 — bounded + attributed**: A service that does not become reachable within the health
  budget MUST surface an environment-attributed timeout (reusing the spec-088 channel), not a
  hang and not a code-under-test failure. *(FR-004, FR-005, SC-006.)*

## Failure attribution (all new failure points)

- **C-13**: Init failure, start failure, and readiness timeout MUST all be reported with a
  reason and attributed to the environment, via the same channel the existing services-start
  invocation already uses. *(FR-005, SC-003.)*

## Teardown (`services-stop.sh`) — new kind-awareness

- **C-13b — kind-aware teardown**: For `kind == "postgres"` teardown MUST stop the server via a
  clean fast shutdown (`pg_ctl stop -m fast`) rather than the bare SIGTERM→SIGKILL pid kill, so
  the next start needs no crash recovery (and a SIGTERM smart-shutdown stall is avoided).
  `redis`/`generic` retain the existing `_stop_pid` behavior. *(FR-012)*

## Install bridge (env-bootstrap side) — verified separately

- **C-14**: A declared service with a service `kind` MUST contribute a service-binary install
  item to the env-bootstrap checklist (`_build_env_bootstrap_payload`), targeting the
  existing deb-into-`<cache>/debs/` delivery. The base image MUST gain nothing. *(FR-006,
  FR-007, SC-004 — verified by the env_manifest derivation unit test, not the templater.)*
