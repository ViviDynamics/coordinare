# Phase 1 Data Model: Symphony Test-Environment Injection

Entities introduced or extended by this feature. All models are pydantic 2.x with
`model_config = ConfigDict(extra="forbid")` unless they already exist with different config.

## TestEnvConfig (new) — `src/coordinare/config.py`

A symphony-level config sub-model naming a dotenv-style test-environment file.

| Field       | Type          | Default | Rules |
|-------------|---------------|---------|-------|
| `repo_path` | `str \| None` | `None`  | Path resolved **inside** the cloned symphony repo. Reject `..` segments and absolute paths (containment against `repo_root`). |
| `host_path` | `str \| None` | `None`  | Absolute host path, read directly by coordinare. |

**Config**: `extra="forbid"`.

**Cross-field validation** (`@model_validator(mode="after")`):
- Exactly one of `repo_path` / `host_path` MUST be set.
  - Both set → config-validation error (no silent precedence).
  - Neither set → config-validation error (an empty block is meaningless; omit it).
- `repo_path` containment is asserted both at config-load time (here / in
  `config_validation.py`) and again at read time in the loader (defense in depth).

**Relationship**: attached to `SymphonyConfig` as `test_env: TestEnvConfig | None = None`
(optional; mirrors `closer_pr_checks` / `persona_scope`).

## SymphonyConfig (extended) — `src/coordinare/config.py`

Add one optional field; no change to existing fields/validators.

| Field      | Type                    | Default | Notes |
|------------|-------------------------|---------|-------|
| `test_env` | `TestEnvConfig \| None` | `None`  | Optional. When present, coordinare loads it for every project-code context. When absent, the agent-discovered `test_env_source` fallback applies. |

## Loaded test environment (transient value) — produced by `load_test_env`

Not a persisted model — a plain `dict[str, str]` of `KEY → literal value`, treated as
secret-like:
- Returned by `test_env_loader.load_test_env(cfg, *, repo_root)`.
- Merged into the validator dry-run `env=` and into the payload `secrets` dict.
- **Never** persisted, logged, or echoed. Only the **keys** and the **source** appear in
  structured logs.

## ServicesManifest (extended) — `packages/service_inference/.../schema.py`

Add one optional, path-only field for the no-config fallback.

| Field             | Type          | Default | Rules |
|-------------------|---------------|---------|-------|
| `test_env_source` | `str \| None` | `None`  | Repo-relative path to a discovered test-env file. **Path only, never values.** Omitted by default. Surfaced in `manifest_json_schema()` so the inference agent can populate it. |

**Validation**: when present, a repo-relative path string (same containment expectation as
`repo_path` when coordinare re-parses it through `load_test_env`). Existing manifest fields
(`services`, `cache_inputs`, `agent_version`) unchanged; `extra="forbid"` preserved.

## Persisted env-cache state (extended) — `state_store.py` / env-cache state model

Persist the **discovered fallback path** (not values) so later QA-runtime and performer
contexts reload the same file.

| Field             | Type          | Default | Notes |
|-------------------|---------------|---------|-------|
| `test_env_source` | `str \| None` | `None`  | The path coordinare discovered/used for the fallback, stored alongside the cache. Path only. A configured `test_env` block takes precedence over a persisted discovered path. |

No test-env **values** are ever persisted. The JSON snapshot stores a path; values are
reloaded from the file at use time.

## Source-of-truth precedence (resolution order at use time)

For each project-code context coordinare resolves the test-env file as:

1. **Configured** `symphony.test_env` block (`repo_path` XOR `host_path`) — highest.
2. Else **persisted/agent-discovered** `test_env_source` path (parsed as a `repo_path`).
3. Else **none** — no test-env vars injected; a genuinely-required `password_env_var`
   still trips the existing `services-start.sh` `exit 75` gate (actionable, not silent).

Within the merged environment, **coordinare operational secrets are applied last** and win
over any test-env key of the same name.
