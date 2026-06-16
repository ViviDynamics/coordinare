# Quickstart: Hosting a stateful service in the QA env-cache

**Branch**: `091-stateful-service-hosting` | **Date**: 2026-06-15

This walks an operator through declaring a Postgres service so DB-backed QA runs connect
successfully instead of failing with "Connection refused". The coordinare owns the init
recipe; you supply the connection target.

> Scope note: aligning the consuming repo's own `config/database.yml` to the connection
> target below is a separate downstream change (Out of Scope here).

## 1. Declare the service in `.coordinare/score.json`

The durable manual-override path (`agent_version="manual-override"`) is trusted verbatim over
flaky LLM inference. Add a Postgres entry with a `kind` and an `init` block:

```json
{
  "agent_version": "manual-override",
  "cache_inputs": ["config/database.yml"],
  "services": [
    {
      "name": "postgres",
      "kind": "postgres",
      "binary": "postgres",
      "version": "16",
      "data_dir": "${XDG_RUNTIME_DIR}/coordinare-services/postgres",
      "port": 5432,
      "why_needed": "Rails app DB; config/database.yml expects root@localhost:5432",
      "init": {
        "superuser": "root",
        "databases": ["app_test"],
        "password_env_var": "PGPASSWORD"
      }
    }
  ]
}
```

- `kind: "postgres"` selects the coordinare-owned `initdb` → create-role → create-db recipe.
- `init.superuser` / `init.databases` are the *parameters*; the recipe is coordinare-owned.
- `init.password_env_var` names the env var holding the admin secret — the secret value is
  never written into the declaration, argv, or logs.
- `data_dir` resolves under `$XDG_RUNTIME_DIR` so it stays writable even when the cache mount
  is read-only.

A `redis` entry needs no `init` (`kind: "redis"`, omit `init`); a `generic` entry behaves
exactly as before.

## 2. Env-bootstrap installs the binary into the cache

Because the entry declares a service `kind`, the env-bootstrap checklist gains a
service-binary install item: the bootstrap performer downloads the Postgres `.deb` into
`<cache>/debs/` using the existing system-package delivery. The performer base image stays
agnostic — it bakes in no database.

Verify: after bootstrap, the binary is present in the cache; inspecting the base image shows
it absent.

## 3. services-start brings the service up (idempotently)

On the first QA run against an empty `data_dir`, the rendered `services-start.sh`:

1. `initdb` the `data_dir` (guarded by the `PG_VERSION` sentinel),
2. creates the `root` superuser (create-if-missing),
3. creates `app_test` (create-if-missing),
4. launches Postgres listening on `localhost:5432`.

On every subsequent run the sentinel is present, so init is skipped — the service just
starts. No re-init, no data loss.

## 4. Readiness is verified before QA proceeds

`services-health.sh` runs a `pg_isready`-style connect probe against `localhost:5432` within
the health budget (60 s). Only when the database actually accepts connections is the service
declared ready. If it never becomes reachable, you get an **environment-attributed** timeout
— the failure is not blamed on the project's code under test.

## 5. Teardown stops the service

When the QA run completes or is torn down, the service's background process is stopped
(`stop_env_cache_services`).

## Validate locally (deterministic, no live DB)

```bash
# Schema accepts the extended entry; rejects init on a non-initializing kind
.venv/bin/pytest tests/unit/services/test_service_inference_schema.py

# Postgres init renders idempotently; redis/generic render unchanged (golden)
.venv/bin/pytest tests/unit/services/test_service_inference_templater.py

# A declared service contributes a binary-install item to the bootstrap checklist
.venv/bin/pytest tests/unit/services/test_env_manifest.py

# score.json → render → services-start contract
.venv/bin/pytest tests/integration/test_service_inference_manual_override.py
```

## Troubleshooting

- **Still "Connection refused"**: confirm `services: []` did not come from LLM inference —
  the `manual-override` declaration must be present and take precedence (FR-008). Check the
  rendered `services-start.sh` actually contains the postgres init block.
- **Port already bound**: the pipeline reports an environment-attributed error rather than
  hanging; free the port or change the declared `port`.
- **Read-only cache errors during initdb**: confirm `data_dir` resolves under
  `$XDG_RUNTIME_DIR`, not inside the cache mount.
