# Phase 1 Data Model: Stateful Service Hosting in the QA Env-Cache

**Branch**: `091-stateful-service-hosting` | **Date**: 2026-06-15 | **Plan**: [plan.md](./plan.md)

This describes the additive schema changes to `coordinare_service_inference.schema`. All changes
preserve the existing `ServiceEntry`/`ServicesManifest` contract: an entry with `kind`
defaulting to `"generic"` and `init=None` renders identically to today.

---

## Entity: `ServiceEntry` (extended)

Existing fields (unchanged): `name`, `binary`, `version`, `data_dir`, `port`, `why_needed`,
`sources`, `external_required`, `required_env_vars`, `start_args`.

### New fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kind` | `Literal["generic", "postgres", "redis"]` | `"generic"` | Selects the coordinare-owned init recipe and readiness probe. `"generic"` = no init (today's behavior). |
| `init` | `ServiceInit \| None` | `None` | First-run initialization parameters. Only meaningful for kinds that initialize (currently `postgres`). |

### Validation rules

- **VR-1 — kind is closed**: `kind` must be one of the literal values. An unknown kind is a
  load-time validation error (not a silent no-op). *(D2)*
- **VR-2 — init requires an initializing kind**: if `init is not None`, `kind` must be a kind
  that supports initialization (`postgres`). Declaring `init` on `generic`/`redis` is a
  validation error — it signals an authoring mistake rather than being silently ignored.
  *(FR-013, D2)*
- **VR-3 — generic/redis unchanged**: `kind in {"generic", "redis"}` with `init=None` MUST
  produce the same rendered start script as an entry with no `kind`/`init` at all. *(FR-013)*
- **VR-4 — connection target drives binding**: when `kind == "postgres"`, the rendered start
  command MUST bind the declared `port` on loopback rather than relying on the binary's default.
  Host is not declarable (fixed to loopback per the single-host assumption). *(FR-002)*

---

## Entity: `ServiceInit` (new)

Cohesive parameter block for a stateful service's idempotent first-run setup. Carries
parameters only — never the init recipe and never a literal secret.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `superuser` | `str` | *(required)* | Admin/superuser role name to create (e.g. `root`). |
| `databases` | `list[str]` | `[]` | Databases to create on first run; each created independently and idempotently. |
| `password_env_var` | `str \| None` | `None` | Name of the env var holding the admin secret. The recipe references this var; the secret value is never stored in the declaration, argv, or logs. *(D5, FR-011)* |

### Validation rules

- **VR-5 — superuser identifier-safe**: `superuser` must match the same identifier-safe
  pattern used for service names (`^[a-z][a-z0-9_]*$`) so it is safe to interpolate into the
  role-creation step. *(FR-011 safety, D5)*
- **VR-6 — database names identifier-safe**: every entry in `databases` must be
  identifier-safe; no whitespace-only or empty names.
- **VR-7 — password_env_var is env-var-safe**: when set, `password_env_var` must match
  `^[A-Z_][A-Z0-9_]*$` (a valid shell environment variable name), so the recipe can read it
  with `${VAR}`. It must NOT be the secret value. *(D5, FR-011)*
- **VR-8 — no secret literals**: `ServiceInit` has no field for a literal password; the only
  credential channel is `password_env_var`. *(FR-011)*

---

## Entity: `ServicesManifest` (unchanged shape)

No structural change. `services: list[ServiceEntry]` now carries the extended entries.
Existing validators (`_agent_version_safe`, `_check_external_entries_have_env_vars`,
`_unique_service_names`) are unaffected. The manual-override path
(`apply_manual_override`, `agent_version="manual-override"`) continues to take precedence
over LLM inference and is the trusted carrier for stateful declarations. *(FR-008, US3)*

---

## Service runtime state (on-disk, not persisted coordinare state)

Not a pydantic model — this is the filesystem state that makes start/init idempotent. It
lives in the service's `data_dir` under `$XDG_RUNTIME_DIR`, never in the read-only cache
mount. *(D4, D8)*

| State | Representation | Used for |
|-------|----------------|----------|
| Initialized | init sentinel present in `data_dir` (for postgres: `PG_VERSION`) | skip `initdb` on re-run *(FR-003)* |
| Running | PID file + `kill -0` liveness (`_is_running`) | skip duplicate launch *(FR-010)* |
| Reachable | kind-aware readiness probe passes within budget | declare ready / time out *(FR-004)* |

### State transitions (per service, per services-start invocation)

```text
            ┌─ sentinel present ─────────────► [Initialized]
[Empty] ──► │
            └─ sentinel absent ─► run recipe ─► [Initialized]   (failure ⇒ env-attributed error, FR-005)
                                  (initdb → create superuser → create databases, each guarded)

[Initialized] ──► already running? ─yes─► [Running] (no-op launch, FR-010)
                                   └─no──► launch on declared port ─► [Running] (FR-002)

[Running] ──► readiness probe within budget ─pass─► [Reachable]  (SC-006)
                                              └─fail/timeout─► env-attributed timeout (FR-004/FR-005)

teardown ──► stop background process(es) ──► [Stopped]  (FR-012)
```

Convergence on partial prior init: a present sentinel does not assume role/database creation
completed; the superuser and each database are created create-if-missing, so a run that died
mid-recipe converges on the next run. *(D4, "partial prior initialization" edge case)*

---

## Install-derivation bridge (coordinare side)

Not a new entity — a mapping. A declared `ServiceEntry` with a service `kind` contributes a
service-binary install item to the env-bootstrap checklist (via `env_manifest` derivation,
surfaced by `_build_env_bootstrap_payload`), reusing the existing system-package/deb delivery
into `<cache>/debs/`. This is what makes the binary present in the cache (SC-004) while the
image stays agnostic (FR-007). *(D3)*
