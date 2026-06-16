# Phase 0 Research: Stateful Service Hosting in the QA Env-Cache

**Branch**: `091-stateful-service-hosting` | **Date**: 2026-06-15 | **Plan**: [plan.md](./plan.md)

This document resolves the load-bearing design decisions for the feature. There are no
`NEEDS CLARIFICATION` markers in the Technical Context; the items below are the decisions
that govern Phase 1 design.

---

## D1 — Init-recipe ownership (the load-bearing decision)

**Decision**: The coordinare owns the deterministic, idempotent initialization *recipe*,
keyed by a service `kind`. The durable service declaration supplies only the *parameters*
(connection target, database name, admin account). The recipe lives in the coordinare-owned
Jinja templates (`services-start.sh.j2` / `services-health.sh.j2`) shipped in the
`coordinare_service_inference` wheel, alongside the existing launch logic.

**Rationale**:
- Mirrors the established `render_activate_sh` / `render_verify_sh` pattern: the coordinare
  owns the deterministic shell recipe and the manifest/declaration supplies data only. This
  is the project's "durable contracts over forgetful/flaky models" principle applied to a
  new surface — the model never authors the `initdb` recipe.
- Keeps product-specific knowledge (Postgres needs `initdb`; Redis needs nothing) out of the
  declaration contract, so a `score.json` author declares *what* they need, not *how* to
  initialize it. This satisfies FR-014 (multi-kind expressiveness without special-casing a
  product in the declaration).
- A recipe expressed as a free-form `init_args`/script field in the declaration would push
  the burden back onto whoever (or whatever model) writes the declaration, reintroducing the
  exact flaky-authorship failure mode the feature exists to remove.

**Alternatives considered**:
- *Free-form `init_script` string in the declaration.* Rejected: maximal flexibility but
  zero determinism guarantee; the declaration becomes a shell-injection surface and the
  recipe is only as reliable as its author. Contradicts D1's rationale and FR-011.
- *A second LLM "init inference" pass.* Rejected outright: adds a flaky model on the exact
  path the feature is meant to make deterministic.
- *Bake init into the performer image entrypoint.* Rejected: violates FR-007 (image stays
  agnostic) and couples every project's init to image releases.

---

## D2 — Schema extension shape (`kind` + `ServiceInit`)

**Decision**: Extend `ServiceEntry` with:
- `kind: Literal["generic", "postgres", "redis"]` defaulting to `"generic"` — the
  discriminator that selects the coordinare-owned recipe.
- `init: ServiceInit | None = None` — a new optional parameter block. `ServiceInit` carries
  the init parameters: `superuser` (admin role name), `databases: list[str]` (databases to
  create), and `password_env_var: str | None` (the env var name holding the admin secret,
  never the secret itself).

`generic` with `init=None` renders exactly today's behavior (mkdir → launch), so the change
is strictly additive and every existing manifest renders byte-for-byte unchanged.

**Rationale**:
- A closed `Literal` for `kind` keeps the recipe set explicit and validatable, and makes
  "unknown kind" a load-time error rather than a silent runtime no-op.
- A separate `ServiceInit` model keeps init parameters cohesive and lets validators enforce
  cross-field rules (e.g. `init` is only meaningful for kinds that initialize; `superuser`
  must be identifier-safe; `password_env_var` must be an env-var-safe name).
- Defaulting `kind="generic"` and `init=None` preserves the existing `ServiceEntry`
  contract; no existing field changes meaning.

**Alternatives considered**:
- *Open `str` kind.* Rejected: loses the "unknown kind fails loudly" guarantee and lets
  typos render an uninitialized service.
- *Inline init fields directly on `ServiceEntry`.* Rejected: bloats the entry, weakens
  cohesion, and makes the "init only applies to some kinds" rule harder to validate.

---

## D3 — The install bridge (declaration → env-bootstrap checklist)

**Decision**: Bridge the durable services declaration into the env-bootstrap install
checklist via `env_manifest` derivation plus the existing persona builder
(`http_performer_service._build_env_bootstrap_payload`). A declared service contributes a
service-binary install item (a system-package / `.deb` install) to the bootstrap checklist,
reusing the existing system-package delivery path (`<cache>/debs/`). The base image gains
nothing.

**Rationale**:
- The env-bootstrap persona already has a SYSTEM PACKAGES / deb install section
  (`_build_env_bootstrap_payload`, the deb block); a service binary is just another deb to
  fetch into the cache. Reusing that path means no new delivery mechanism and satisfies
  FR-006/FR-007 and SC-004 (binary present in cache, absent in image).
- `env_manifest` is already the single place that turns project signals into an install
  checklist; deriving a service-install item there keeps one source of truth for "what the
  bootstrap must install."

**Alternatives considered**:
- *A separate bootstrap stage just for services.* Rejected: duplicates the deb-install
  machinery and adds a stage to sequence and fail-attribute. The existing path already
  installs debs into the cache.
- *Install at services-start time.* Rejected: services-start runs against a (often
  read-only) cache with no network guarantee; installation belongs in env-bootstrap, which
  already owns network fetches into the cache.

---

## D4 — Idempotency mechanism (sentinel-guarded init)

**Decision**: Init is guarded by an on-disk sentinel in the service's `data_dir`. For
Postgres the natural sentinel is the `PG_VERSION` file that `initdb` creates; the recipe
runs `initdb`/role/db creation only when the sentinel is absent, and treats an
already-initialized `data_dir` as a no-op. `redis`/`generic` have no init phase and so are
trivially idempotent.

**Rationale**:
- A filesystem sentinel is the standard, race-light way to make first-run setup idempotent
  and survives across runs in the persisted `data_dir`. It directly satisfies FR-003
  (idempotent: no re-init, no data loss, no failure on re-run) and SC-002 (exactly one
  initialized instance).
- Using Postgres's own `PG_VERSION` rather than a bespoke marker avoids a "marker present
  but data half-written" divergence for the common case.

**Edge — partial prior init**: If the sentinel exists but a required database/role is
missing (a prior run died mid-recipe), the recipe must converge — create-if-missing for the
role and database steps (each independently guarded), rather than assuming the sentinel
implies full completion. This addresses the "partial prior initialization" edge case.

**Alternatives considered**:
- *Lockfile + completion marker written by the recipe.* Viable and used for the concurrency
  guard (D7); but for *init-already-done* detection the service's own data-dir state is the
  more reliable source of truth than a marker we maintain.
- *Always re-run init.* Rejected: `initdb` on a populated dir fails and risks data loss.

---

## D5 — Secrets handling

**Decision**: Admin credentials are passed to the init/start recipe through an environment
variable named by `ServiceInit.password_env_var`; the recipe references the variable, never
the literal secret, and never echoes it. No credential is interpolated into argv, log lines,
or observability records. All declaration-supplied values are `shq`-quoted; the secret value
is read from the environment at runtime inside the container.

**Rationale**:
- Satisfies FR-011 and the "Credentials/secrets" edge case. Argv is world-readable via
  `/proc`, so passwords must not appear there; an env-var reference keeps the value out of
  the rendered script, the process table, and logs.
- Consistent with how the rest of the pipeline handles required env vars
  (`required_env_vars` / `external_required`).

**Alternatives considered**:
- *Password as a `ServiceInit` literal field.* Rejected: would render the secret into the
  script text and risk it reaching logs/observability — direct FR-011 violation.

---

## D6 — Readiness probe

**Decision**: `services-health.sh.j2` gains a `kind`-aware readiness probe. For `postgres`
the probe is a connection-level liveness check against the declared host/port (e.g.
`pg_isready`-style TCP/connect probe) rather than the generic port-bound check alone; for
`redis`/`generic` the existing port/liveness check is retained. The probe runs within the
existing health budget (60 s) and a failure to become reachable inside the window is reported
as an environment-attributed timeout.

**Rationale**:
- A port being bound is not the same as the database accepting connections; FR-004 requires
  verifying actual reachability on the connection target, and SC-006 bounds it. A
  kind-aware probe closes the "starts but never becomes reachable" edge case.
- Reuses the existing health budget rather than introducing a new tunable.

**Alternatives considered**:
- *Port-bound check only.* Rejected: Postgres binds the port before it is ready to serve;
  a bound port would falsely report ready.

---

## D7 — Concurrency guard

**Decision**: Reuse the existing `services-start.sh` PID-file / `_is_running` and
`_port_bound` guards, and gate the init phase behind the same data-dir sentinel (D4) plus a
short-lived init lock so two concurrent start attempts cannot both run `initdb` or both bind
the port. The first attempt wins; the second no-ops (already running / already initialized).

**Rationale**:
- The template already has `_is_running` (PID `kill -0`) and `_port_bound` helpers; layering
  the sentinel + lock on init satisfies FR-010 (init at most once, bind at most once) and
  the "concurrent start race" edge case without new infrastructure.
- Single-host single-process is the deployment assumption, so a filesystem lock is
  sufficient; no distributed coordination is needed.

**Alternatives considered**:
- *No guard (rely on single-process assumption).* Rejected: services-start can be invoked
  more than once against the same cache (re-run, retry), so the guard must be explicit.

---

## D8 — Writable storage under a read-only cache mount

**Decision**: A stateful service's mutable `data_dir` lives under `$XDG_RUNTIME_DIR`
(`${XDG_RUNTIME_DIR:-/tmp}/coordinare-services`, the existing `SERVICES_DIR` root), never
inside the read-only env-cache mount. The cache mount carries the *binary*; the runtime dir
carries the *state*.

**Rationale**:
- The env-cache is frequently mounted read-only; `initdb` and the running service need a
  writable data area. The template already roots `SERVICES_DIR` at `$XDG_RUNTIME_DIR`, so
  this is consistent with current behavior and satisfies FR-009 and the "read-only cache
  mount" edge case.

**Alternatives considered**:
- *data_dir inside the cache.* Rejected: fails on a read-only mount and conflates immutable
  cache content with mutable runtime state.

---

## Cross-cutting: environment attribution

Init/start/readiness failures are surfaced as environment-attributed failures by reusing the
spec-088 QA-verdict-integrity wiring already in the services-start invocation path
(`_start_env_cache_services` / `_run_env_cache_health_check`). No new attribution mechanism
is introduced; the new failure points (init failure, readiness timeout) feed the same
channel, satisfying FR-005 and SC-003.

## Summary of resolved decisions

| ID | Decision | Drives |
|----|----------|--------|
| D1 | Coordinare owns kind-keyed recipe; declaration supplies params | FR-001, FR-014 |
| D2 | `kind` Literal + optional `ServiceInit`, default preserves behavior | FR-001, FR-013, FR-014 |
| D3 | Reuse deb-install path via `env_manifest` + persona bridge | FR-006, FR-007, SC-004 |
| D4 | Sentinel-guarded, convergent idempotent init | FR-003, SC-002 |
| D5 | Secret via `password_env_var`, never argv/logs | FR-011 |
| D6 | Kind-aware readiness probe within health budget | FR-004, SC-006 |
| D7 | PID/sentinel/lock concurrency guard | FR-010 |
| D8 | data_dir under `$XDG_RUNTIME_DIR`, not the RO mount | FR-009 |

No `NEEDS CLARIFICATION` markers remain. Ready for Phase 1.
