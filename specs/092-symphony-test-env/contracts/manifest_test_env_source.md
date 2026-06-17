# Contract: `ServicesManifest.test_env_source`

**Module**: `packages/service_inference/src/coordinare_service_inference/schema.py`
(prompt guidance in `prompt.py`)

## Schema addition

```python
class ServicesManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ...
    test_env_source: str | None = None   # repo-relative path; NEVER values
```

## Semantics

- **Path only, never values.** The field carries a repo-relative path to a discovered
  test-env file. The manifest must never contain literal secret values (spec-091
  invariant).
- **Optional, omitted by default.** Default `None`; absent in manifests where the symphony
  has a configured `test_env` block (config wins) or has no test-env file.
- **Fallback role.** When no `symphony.test_env` is configured, the inference agent
  identifies the project's test-env file and emits its path here. Coordinare parses that
  path through the *same* `load_test_env` (as a repo-relative source) for the dry-run and
  persists it with the env-cache for later QA-runtime / performer reloads.

## Prompt guidance (`prompt.py`)

Instruct the agent to:
- Emit `test_env_source` **only when no config-provided test-env file is in play** and the
  repo contains a recognizable test-env file (e.g. `.env.test`, `.env.ci`).
- Emit the **path only** — never read or transcribe values into the manifest.

## JSON schema

`manifest_json_schema()` surfaces `test_env_source` as an optional string property so the
LLM structured-output target can populate it.

## Tests (unit)

- `test_env_source` optional — manifest validates with it omitted (default `None`).
- Accepts a repo-relative path string when present.
- Existing manifest invariants unchanged: `extra="forbid"`, postgres `password_env_var`
  name-only rule, POSIX name patterns, NUL-byte guards.
- (Integration) discovered path persisted with the cache and reloaded for later contexts.
