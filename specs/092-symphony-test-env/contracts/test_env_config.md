# Contract: `TestEnvConfig` + `SymphonyConfig.test_env`

**Module**: `src/coordinare/config.py` (validation also in `config_validation.py`)

## Schema

```python
class TestEnvConfig(BaseModel):
    """Symphony test-environment file. Names a dotenv-style file whose KEY=VALUE
    pairs coordinare injects into every container that runs project code."""
    model_config = ConfigDict(extra="forbid")

    repo_path: str | None = None    # resolved INSIDE the cloned symphony repo
    host_path: str | None = None    # absolute host path, read directly

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "TestEnvConfig": ...


class SymphonyConfig(BaseModel):
    ...
    test_env: TestEnvConfig | None = None
```

## YAML surface (`config.yaml` / `config.example.yaml`)

```yaml
symphonies:
  - name: website
    github_project_number: 51
    test_env:
      repo_path: .env.test          # ── OR ──
      # host_path: /run/secrets/website-test.env
```

## Validation rules (load time)

| Input | Result |
|-------|--------|
| `repo_path` set, `host_path` unset | ACCEPT |
| `host_path` set, `repo_path` unset | ACCEPT |
| both set | ERROR — "exactly one of repo_path / host_path" |
| neither set (empty block) | ERROR — "empty test_env block; omit it" |
| `repo_path` containing `..` | ERROR — escapes the clone |
| `repo_path` absolute | ERROR — must be repo-relative |
| unknown key under `test_env` | ERROR — `extra="forbid"` |
| no `test_env` key at all | ACCEPT — field is `None` |

## Behavioral guarantees

- The block is truly optional; omitting it is the documented common case (agent-discovery
  fallback applies).
- Errors are raised at config-load time, before any job runs — never deferred to a
  buried `exit 75` in a dry-run.
- `extra="forbid"` rejects typos in field names.

## Tests (unit)

- both-set rejected; neither-set rejected; each-alone accepted.
- `repo_path` with `..` rejected; absolute `repo_path` rejected.
- `SymphonyConfig` round-trips with and without the block; unknown key rejected.
