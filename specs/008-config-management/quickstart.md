# Quickstart: Configuration Management (008)

**Branch**: `008-config-management` | **Date**: 2026-02-25

---

## For Operators

### Validate your config before starting the daemon

```bash
# Validate config at default location (./config.yaml or ~/.coordinare/config.yaml)
coordinare config validate

# Validate a specific file
coordinare config validate --config /path/to/my-config.yaml

# Strict mode: treat deprecated fields as errors (useful for CI pipelines)
coordinare config validate --strict
```

### Inject secrets via environment variables (CI/CD)

All scalar config fields can be overridden with `COORDINARE_<FIELD_NAME>` env vars. Env vars always win over file values.

```bash
# Top-level field override
export COORDINARE_GITHUB_TOKEN=ghp_your_token_here

# Nested scalar field override (double-underscore separator)
export COORDINARE_GITHUB__POLL_INTERVAL_SECONDS=60

# Start without a config file (supply all required fields via env vars)
coordinare  # no --config flag needed; env vars satisfy all required fields
```

**Note**: List-type fields (e.g., `notifications.channels`) cannot be supplied via env vars and must be defined in the config file.

### Config file discovery (no --config flag needed)

Coordinare searches for `config.yaml` in this order:
1. `--config PATH` flag (if provided)
2. `COORDINARE_CONFIG_PATH` env var (if set)
3. `./config.yaml` (current working directory)
4. `~/.coordinare/config.yaml` (user home)

The first match is used. If none found, coordinare starts from env vars only.

---

## For Developers

### Module layout (new files)

```
src/coordinare/
├── config.py                    # existing — ProjectConfiguration model (unchanged interface)
├── config_discovery.py          # NEW — discover_config_path()
├── config_validation.py         # NEW — DEPRECATION_REGISTRY, validate_config(), types
└── __main__.py                  # MODIFIED — add 'config validate' subcommand

tests/unit/
├── test_config.py               # existing — updated to use new validation path
├── test_config_discovery.py     # NEW
├── test_config_validation.py    # NEW
└── test_cli_config_validate.py  # NEW
```

### Running tests

```bash
cd src
pytest ../tests/unit/test_config_discovery.py -v
pytest ../tests/unit/test_config_validation.py -v
pytest ../tests/unit/test_cli_config_validate.py -v
pytest ../tests/unit/test_config.py -v   # existing tests must still pass
```

### Adding a new field to the deprecation registry

When a future spec removes a config field, add an entry to `DEPRECATION_REGISTRY` in `config_validation.py`:

```python
DEPRECATION_REGISTRY: dict[str, DeprecationEntry] = {
    # ... existing entries ...
    "my_old_field": DeprecationEntry(
        removed_in="009-my-spec",
        replacement_path="new.location.for.field",
        migration_hint="Move 'my_old_field' to 'new.location.for.field' in config.yaml.",
    ),
}
```

No other code changes are needed — the pre-validation pass reads this registry automatically.

### How validation works end-to-end

```
1. discover_config_path(explicit) → Path | None
      ↓
2. load raw YAML dict from file (or empty dict)
      ↓
3. pre_validate_raw(raw) → (field_errors, deprecation_warnings)
   - deprecated fields → ConfigDeprecationWarning
   - unknown fields   → ConfigFieldError(unknown_field)
      ↓
4. ProjectConfiguration(**raw)  [pydantic-settings merges env vars]
   - ValidationError → extract all field errors → ConfigFieldError(missing|wrong_type|invalid_value)
      ↓
5. ConfigValidationResult(errors, warnings, config_file_path, env_var_fields_count, passed)
      ↓
6. validate command: render to stdout, exit 0 or 1
   daemon startup:   emit structlog entry, proceed or raise SystemExit(2)
```

### Closest-field-name suggestion (unknown fields)

When an unknown field is detected (e.g., `githubb_token`), the error output suggests the closest known field name. The suggestion uses Python's `difflib.get_close_matches()` against `ProjectConfiguration.model_fields.keys()`. If no close match is found (similarity < 0.6), no suggestion is shown.

---

## Config Schema Reference

All supported fields (post-spec-006 model, assuming spec 006 is implemented):

```yaml
# Required
project_name: string
github_org: string
github_project_number: integer
github_token: string (SecretStr — set via COORDINARE_GITHUB_TOKEN)
agent_host: string
agent_user: string
agent_command: string  # must contain {card_context}
human_reviewers: [string, ...]  # list — env vars cannot override this

# Optional (with defaults)
agent_port: integer (default: 22)
agent_key_path: path (default: ~/.ssh/id_ed25519)
poll_interval_seconds: integer 10–300 (default: 30)
blocked_reminder_hours: integer (default: 24)
health_check_port: integer (default: 8080)
output_mode: "human" | "structured" (default: "human")
log_level: "debug" | "info" | "warning" | "error" (default: "info")
heartbeat_interval_seconds: integer 5–300 (default: 30)
max_cycles: integer | null (default: null — unlimited)

# Notifications block (spec 006)
notifications:
  channels:
    - name: string
      type: slack | email
      # ... channel-specific fields (list — must be in YAML file)
```

### Deprecated fields (removed in spec 006)

These fields are no longer valid. `coordinare config validate` will warn you and provide migration instructions:

- `slack_webhook_url` → `notifications.channels[].webhook_url`
- `slack_channel` → `notifications.channels[].name`
- `smtp_host` → `notifications.channels[].smtp_host`
- `smtp_port` → `notifications.channels[].smtp_port`
- `smtp_username` → `notifications.channels[].smtp_username`
- `smtp_password` → `notifications.channels[].smtp_password`
- `notification_email` → `notifications.channels[].sender_email`
