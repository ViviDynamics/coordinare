# Symphony Test-Environment Injection — Design

**Date:** 2026-06-16
**Status:** Design (approved for plan)
**Related:** spec 091 (Stateful Service Hosting); service-inference start-phase validation

## Problem

Spec 091 made coordinare install and host stateful services (postgres, redis) from
the inferred services manifest. For a postgres entry with an `init.password_env_var`,
the generated `services-start.sh` pre-checks that the named env var is set, then runs
`initdb` with `--pwfile=<(printf '%s' "$VAR")` so the literal secret never lands in
argv, logs, or the manifest. If the var is unset the script exits 75 (EX_TEMPFAIL).

The service-inference validator runs a `validation_phase=start` dry-run that actually
renders and executes `services-start.sh`. That subprocess inherits only the validator
process environment (`env={**os.environ, **(env or {})}` in
`validator.py`, with the main loop passing no `env`). The postgres admin password
(e.g. `POSTGRESQL_PASSWORD`) is not present there, so the gate trips `exit 75` and the
entire manifest is rejected — leaving the env-cache with a stale `services-start.sh`
that never starts postgres. Downstream, the `website` symphony's QA hits
"Connection refused" because the app's database never came up.

The same `services-start.sh` also runs at real QA runtime in the env-cache container,
and other performers (e.g. the implementer) run project code that expects the project's
test environment to be present. The gap is therefore not specific to validation: there
is no mechanism by which the project's **test environment settings** reach the
containers that run project code.

## Constraint (carried from spec 091, non-negotiable)

The postgres `init.password_env_var` — and the manifest generally — stores only an
env-var **name** (regex `[A-Z_][A-Z0-9_]*`), never a literal secret. The literal value
must live only in a file and in-process environment. This design preserves that
invariant end to end.

## Decision

Introduce a first-class, symphony-level **test environment** concept: a configured
dotenv-style file whose `KEY=VALUE` pairs coordinare injects into the environment of
every container that runs project code:

1. the service-inference start-phase validation dry-run,
2. the env-cache QA runtime (where `services-start.sh` runs), and
3. other code-running performers (e.g. the implementer).

The file is supplied via a symphony-level config block, either as a path inside the
symphony's own repository or as an absolute host path (a volume-mounted file for
symphonies whose test password is a genuine secret rather than a committed test
credential). When no block is configured, the inference agent discovers the test-env
file and emits its **path** (never values) in the manifest, and coordinare parses that
same file through the identical loader.

Approach chosen: **coordinare-owned parse-and-inject** (over mount-and-source, which
carries arbitrary-code-execution risk via `source`, handles repo vs. host sources
differently, can't redact cleanly, and doesn't naturally cover the in-process validator
subprocess; and over agent-only discovery, which is non-deterministic and offers no
escape hatch for non-committed secrets).

## Section 1 — Config surface & loader contract

### Config model

New sub-model on `SymphonyConfig` (`src/coordinare/config.py`), mirroring the existing
`CloserPrChecksConfig` style (`extra="forbid"`, truly-optional):

```python
class TestEnvConfig(BaseModel):
    """Symphony test-environment file. Names a dotenv-style file whose KEY=VALUE
    pairs coordinare injects into every container that runs project code."""
    model_config = ConfigDict(extra="forbid")

    repo_path: str | None = None    # path resolved INSIDE the cloned symphony repo
    host_path: str | None = None    # absolute host path, read directly by coordinare

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "TestEnvConfig":
        # exactly one of repo_path / host_path must be set:
        #   both set   -> hard config-validation error (no silent precedence)
        #   neither set -> error (an empty block is meaningless; omit it instead)
        ...
```

On `SymphonyConfig`: `test_env: TestEnvConfig | None = None`.

Cross-field rules validated at config load time (`config_validation.py` →
`SymphonyConfig` model validators), before any job runs:

- exactly one of `repo_path` / `host_path`;
- `repo_path` must not escape the clone — reject `..` segments and absolute paths
  (containment check against `repo_root`).

`config.yaml` / `config.example.yaml`:

```yaml
symphonies:
  - name: website
    github_project_number: 51
    test_env:
      repo_path: .env.test          # ── OR ──
      # host_path: /run/secrets/website-test.env
```

### Loader

New service `src/coordinare/services/test_env_loader.py`, one pure function:

```python
def load_test_env(cfg: TestEnvConfig, *, repo_root: Path) -> dict[str, str]: ...
```

- **Source resolution:** `host_path` read directly; `repo_path` resolved under
  `repo_root` with a containment check (the same rule enforced at config-load time,
  re-asserted at read time as defense in depth).
- **Parsing — dotenv-style, never `source`:** split on the first `=`; skip blank lines
  and `#` comments; strip one layer of matching surrounding quotes; ignore a leading
  `export `. No shell execution, no command substitution, no inter-variable
  interpolation — a value is taken literally (e.g. `$(rm -rf /)` is returned as the
  literal string).
- **Returns** a plain `dict[str, str]`.
- **Missing/unreadable file when a `test_env` block is configured** → a clear,
  coordinare-side, environment-attributed error naming the resolved path and the field
  that pointed at it — not a silent empty dict.

## Section 2 — Injection points & agent-scan fallback

Coordinare calls `load_test_env(...)` per context that needs the vars and merges the
result into that context's environment. Values are runtime env only — never baked into
the cache image — so a reused cache works across symphonies.

1. **Inference start-phase dry-run.** The validator already accepts `env=`
   (`validator.py` merges `{**os.environ, **(env or {})}`); the main loop
   (`__init__.py`) does not currently pass it. Inference runs inside the bootstrap
   performer with the repo cloned, so the bootstrap persona resolves the test-env vars
   and threads them into `validate(..., env=test_env_vars)`. The `password_env_var`
   gate then sees the value, `initdb` runs, and the manifest is accepted.

2. **Env-cache QA runtime** and **3. implementer / other code-running performers.**
   Both build payloads through the `secrets` seam (`_build_regular_payload()` in
   `http_performer_service.py`; the env-bootstrap builder has its own). Coordinare loads
   the symphony's `test_env` and merges its `KEY=VALUE`s into the payload `secrets`
   dict, so any performer running project code (and the cache's `services-start.sh` it
   invokes) has them in-environment.

**Precedence:** coordinare-owned operational secrets (`GITHUB_TOKEN`, backend API keys)
are injected **after** test-env vars, so a test file can never clobber an operational
credential.

### Fallback (no `test_env` configured)

The inference agent already reads the repo; it identifies the test-env file and emits
its **path only** in the manifest via a new optional field on `ServicesManifest`:

```python
test_env_source: str | None = None   # repo-relative path; never values
```

Coordinare feeds that discovered path through the **same** `load_test_env` parser (as if
it were a `repo_path`) for the dry-run, and **persists it alongside the cache** so the
QA-runtime and implementer contexts reload the same file later.

If neither the config nor a discovered path yields the var the gate needs, inference is
rejected as genuinely-missing — correct behavior, not a silent pass.

## Section 3 — Secret handling, error attribution, testing

### Redaction

Every value returned by `load_test_env` is treated as secret-like and routed through
the existing redacted `secrets` channel — never logged, never echoed. Structured log
events emit only the **keys** (var names) and the **source** (`repo_path` / `host_path`
/ agent-discovered path), never values. The literal secret lives only in the file and
in-process environment; the manifest and logs carry only names and paths.

### Error attribution

- `test_env` configured but file missing/unreadable → distinct coordinare-side error
  naming the resolved path and the field that pointed at it (not an `exit 75` buried in
  a dry-run).
- File present but the specific `password_env_var` the gate needs is absent → the
  existing `exit 75` "requires env var X but it is unset" still fires, now meaning
  "declared in the manifest but not provided by the test env" — actionable.
- `repo_path` escaping the clone → config-validation error at load time.

### Testing

- **Unit — loader:** dotenv parse edge cases (quotes, leading `export `, `#` comments,
  blank lines, `=` inside value, CRLF); containment rejection of `../` and absolute
  `repo_path`; host vs. repo source resolution; missing-file error; no shell execution
  (a `$(...)` value is returned literally).
- **Unit — config:** `TestEnvConfig` both-set rejected, neither-set rejected, each-alone
  accepted; `SymphonyConfig` round-trips with and without the block.
- **Unit — manifest:** `test_env_source` optional, path-only, omitted by default;
  persisted and reloaded with the cache.
- **Integration:** start-phase dry-run with injected `POSTGRESQL_PASSWORD` passes the
  postgres gate; absent value still trips `exit 75`; redaction asserts no value appears
  in captured logs.
- Commands: tests `.venv/bin/pytest`; lint `.venv/bin/ruff check`.

## Out of scope (YAGNI)

- Multiple test-env files per symphony.
- Var-name remapping / templating.
- Inter-variable interpolation.
- Encrypted-at-rest files (the host-mount source already covers genuine-secret cases).

## Files touched (anticipated)

- `src/coordinare/config.py` — `TestEnvConfig` + `SymphonyConfig.test_env`.
- `src/coordinare/config_validation.py` — cross-field validation wiring (if not fully on
  the model).
- `src/coordinare/services/test_env_loader.py` — new loader service.
- `src/coordinare/services/http_performer_service.py` — inject into `_build_regular_payload`
  and the env-bootstrap payload builder; thread `env=` into the inference validate call.
- `packages/service_inference/src/coordinare_service_inference/schema.py` — `test_env_source`
  on `ServicesManifest`.
- `packages/service_inference/src/coordinare_service_inference/prompt.py` — instruct the agent
  to emit `test_env_source` when no config-provided file is in play.
- env-cache state persistence — store the discovered fallback path with the cache.
- `config.example.yaml` — documented `test_env` example.
- Tests under `tests/unit/` and integration.
