# Contract: `coordinare config validate` CLI

**Spec**: 008-config-management | **Date**: 2026-02-25

---

## Invocation

```
coordinare config validate [OPTIONS]

Options:
  --config PATH    Explicit path to config file (overrides discovery order)
  --strict         Treat deprecated fields as errors (exit 1) in addition to normal errors
  --help           Show help and exit
```

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Validation passed (no errors; no deprecations, or deprecations present but not in `--strict` mode) |
| `1` | Validation failed (one or more errors, OR `--strict` and one or more deprecation warnings) |

---

## Stdout Format

### Success (exit 0)

```
✓ Config valid — loaded from /path/to/config.yaml
  Fields resolved: 22 from file, 3 from environment variables
```

Or, when env-vars-only (no config file):

```
✓ Config valid — no config file (all required fields supplied via environment variables)
  Fields resolved: 25 from environment variables
```

### Success with deprecation warnings (exit 0 without `--strict`)

```
✓ Config valid — loaded from /path/to/config.yaml
  Fields resolved: 22 from file, 1 from environment variables

DEPRECATION WARNINGS (use --strict to treat as errors):
  [DEPRECATED] slack_webhook_url
    Removed in: 006-notification-alerting
    Replace with: notifications.channels[].webhook_url
    Migration: Add a 'notifications: channels:' entry with 'type: slack' and 'webhook_url: <your-webhook-url>'.
```

### Validation failure (exit 1)

```
✗ Config validation failed — loaded from /path/to/config.yaml

ERRORS:
  [MISSING] github_token
    Field 'github_token' is required but was not found in config.yaml or environment variables.
    Fix: Set COORDINARE_GITHUB_TOKEN=<token> or add 'github_token: <value>' to config.yaml.

  [WRONG_TYPE] poll_interval_seconds
    Expected integer (10–300), got string: "thirty"
    Fix: Change 'poll_interval_seconds' to an integer between 10 and 300.

  [UNKNOWN_FIELD] githubb_token
    Field 'githubb_token' is not a recognised configuration field.
    Fix: Did you mean 'github_token'? Remove or rename this field.
```

### No config file found (exit 1)

```
✗ Config validation failed — no config file found

Searched paths:
  1. (--config flag not provided)
  2. (COORDINARE_CONFIG_PATH not set)
  3. ./config.yaml — not found
  4. ~/.coordinare/config.yaml — not found

Fix: Create a config file at one of the above paths, or supply all required fields via COORDINARE_* environment variables.
```

---

## Output Rules

- One error/warning block per field; never truncated
- Errors section always before Warnings section
- Each entry includes: field path, error type label in brackets, description, fix hint
- `--strict` flag: when deprecation warnings are present, the "DEPRECATION WARNINGS" section header changes to "DEPRECATION ERRORS" and exit code is 1
- No ANSI colour codes (plain text; operators may redirect to log files)
- All output goes to stdout (not stderr), to allow easy capture in CI pipelines

---

## Validation Coverage

The `coordinare config validate` command validates:

| Check | Handled by |
|---|---|
| Missing required fields | pydantic ValidationError |
| Type mismatches (incl. env var source) | pydantic ValidationError |
| Business rule violations (bounds, pattern) | pydantic field_validator |
| Unknown fields (not in schema, not deprecated) | pre_validate_raw() |
| Deprecated fields (removed in prior specs) | pre_validate_raw() + DEPRECATION_REGISTRY |
| Empty string treated as absent | pydantic field_validator |
| Unresolved placeholder values (`${...}`) | existing pydantic field_validators |

---

## Config Discovery Contract

When `--config PATH` is provided:
- If PATH does not exist → error naming the path; exit 1
- If PATH is a directory → error "config path must point to a file, not a directory"; exit 1
- If PATH exists and is a file → use it unconditionally; do not search further

When `COORDINARE_CONFIG_PATH` is set (no `--config` flag):
- Same file/directory rules as above

Discovery search order (when no explicit path provided):
1. `./config.yaml` (working directory)
2. `~/.coordinare/config.yaml` (user home)

---

## Daemon Startup Parity Contract

- A config that passes `coordinare config validate` MUST allow the daemon to start without config-related errors.
- The daemon uses the identical validation function (`validate_config()`); `__main__.py` calls it at startup before service bootstrap.
- If validation fails at daemon startup, the daemon exits with code 2 and emits a structured log entry with all errors (same fields as the CLI output, but in JSON format via structlog).

---

## Startup Structured Log Contract

Emitted by the daemon (not the validate command) on every successful startup:

```json
{
  "event": "config_loaded",
  "config_file": "/absolute/path/to/config.yaml",
  "env_var_fields_count": 3,
  "deprecated_fields_detected": false,
  "level": "info",
  "timestamp": "2026-02-25T10:00:00Z"
}
```

- `config_file`: absolute path, or `"none"` if env-vars-only
- `env_var_fields_count`: integer count of scalar fields resolved from `COORDINARE_*` env vars
- `deprecated_fields_detected`: boolean
