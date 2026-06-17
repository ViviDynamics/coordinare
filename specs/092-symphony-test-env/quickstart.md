# Quickstart: Symphony Test-Environment Injection

How an operator uses this feature, and how to verify it end to end.

## Operator: configure a symphony's test env

Add a `test_env` block to the symphony in `config.yaml`. Use **exactly one** source.

**Committed test credentials (common case)** — a `.env.test` versioned in the symphony repo:

```yaml
symphonies:
  - name: website
    github_project_number: 51
    test_env:
      repo_path: .env.test
```

**Genuine secret (not committed)** — a volume-mounted file on the host:

```yaml
symphonies:
  - name: website
    github_project_number: 51
    test_env:
      host_path: /run/secrets/website-test.env
```

The file is dotenv-style:

```sh
# .env.test — values are taken literally (no shell, no interpolation)
POSTGRESQL_PASSWORD=test-pw-not-a-real-secret
export DATABASE_URL="postgres://app:test-pw-not-a-real-secret@localhost:5432/app_test"
```

Restart coordinare after editing config (remember `set -a && source .env && set +a` first,
so `config.yaml` `${VAR}` placeholders expand).

## Zero-config fallback

If you omit `test_env`, the inference agent will discover a recognizable test-env file
(e.g. `.env.test`) and emit its **path** as `test_env_source` in the services manifest.
Coordinare parses that same path and persists it with the env-cache — no operator action
needed. To override discovery (or to point at a non-committed secret), add an explicit
`test_env` block; the config block always wins.

## What coordinare injects, and where

The parsed `KEY=VALUE`s are injected into every context that runs project code:

1. the service-inference **start-phase dry-run** (so postgres `initdb` sees its password),
2. the **env-cache QA runtime** (where `services-start.sh` runs), and
3. **other code-running performers** (e.g. the implementer).

Coordinare-owned operational secrets (`GITHUB_TOKEN`, API keys) are injected **after**
test-env vars, so a test file can never clobber a credential.

## Verify (the website postgres fix)

1. With `test_env.repo_path: .env.test` (containing `POSTGRESQL_PASSWORD`) configured for
   `website`, trigger an env bootstrap.
2. **Expect**: the start-phase dry-run passes the postgres gate (no `exit 75`); the services
   manifest is **accepted**; `services-start.sh` boots postgres.
3. **Expect**: website QA reaches the database — no "Connection refused".
4. **Expect (redaction)**: grep the captured logs / manifest for the literal password value
   → **zero hits**. Logs show only the var **names** (`POSTGRESQL_PASSWORD`) and the
   **source** (`repo_path=.env.test`).
5. **Negative**: remove `POSTGRESQL_PASSWORD` from the file → the gate still trips
   `exit 75` with an actionable "requires env var POSTGRESQL_PASSWORD but it is unset" —
   meaning "declared in the manifest but not provided by the test env".

## Run the checks

```sh
.venv/bin/pytest tests/unit/services/test_test_env_loader.py
.venv/bin/pytest tests/unit/services/test_service_inference_schema.py
.venv/bin/pytest            # full suite; coverage MUST NOT decrease
.venv/bin/ruff check src/coordinare/services/test_env_loader.py src/coordinare/config.py
```
