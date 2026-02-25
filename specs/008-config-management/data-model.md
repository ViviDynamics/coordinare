# Data Model: Configuration Management (008)

**Branch**: `008-config-management` | **Date**: 2026-02-25

---

## Entities

### DeprecationEntry

A record in the static deprecation registry describing a single removed or renamed config field.

| Field | Type | Description |
|---|---|---|
| `removed_in` | `str` | Spec identifier that removed the field (e.g., `"006-notification-alerting"`) |
| `replacement_path` | `str` | Dotted path of the replacement field (e.g., `"notifications.channels[].webhook_url"`) |
| `migration_hint` | `str` | Plain-English instruction for migrating the value to the new location |

**Source**: Module-level constant `DEPRECATION_REGISTRY: dict[str, DeprecationEntry]` in `config_validation.py`. Populated at import time. Not user-configurable.

---

### ConfigDeprecationWarning

A warning produced when a deprecated field is detected in the raw config source.

| Field | Type | Description |
|---|---|---|
| `field_name` | `str` | The deprecated field name as it appeared in the source |
| `removed_in` | `str` | Spec identifier that removed this field |
| `replacement_path` | `str` | Dotted path of the new field |
| `migration_hint` | `str` | Plain-English migration instruction |

**Constraints**:
- `field_name` MUST be a key in `DEPRECATION_REGISTRY`
- Reported as a warning by default; escalated to error under `--strict`

---

### ConfigFieldError

An error produced during the validation pass.

| Field | Type | Description |
|---|---|---|
| `field_path` | `str` | Dotted field path (e.g., `"notifications.channels[0].webhook_url"`) |
| `error_type` | `ErrorType` | Enum: `missing`, `wrong_type`, `unknown_field`, `invalid_value` |
| `fix_hint` | `str` | Plain-English description of what to change and how |
| `source` | `str \| None` | `"env_var:<NAME>"` if the error originates from an env var, `"file"` otherwise |

**ErrorType enum values**:
- `missing` — Required field is absent from all sources
- `wrong_type` — Field present but value cannot be coerced to expected type (includes empty string treated as absent)
- `unknown_field` — Field name not in current schema and not in deprecation registry
- `invalid_value` — Field present and correct type but fails business rule (e.g., `poll_interval_seconds=5`)

---

### ConfigValidationResult

The outcome of a complete validation run.

| Field | Type | Description |
|---|---|---|
| `errors` | `list[ConfigFieldError]` | All hard errors (exit 1 if any) |
| `warnings` | `list[ConfigDeprecationWarning]` | Deprecation warnings (exit 1 only under `--strict`) |
| `config_file_path` | `Path \| None` | Resolved config file path, or `None` if env-vars-only |
| `env_var_fields_count` | `int` | Number of scalar fields resolved from env vars |
| `passed` | `bool` | True only if `errors` is empty |

**Derived property**:
- `passed_strict` → `True` only if `errors` is empty AND `warnings` is empty

---

### ConfigSource

Metadata about a single input to the resolved configuration.

| Field | Type | Description |
|---|---|---|
| `source_type` | `SourceType` | Enum: `file`, `env_var`, `default` |
| `path_or_name` | `str` | File path (for `file`), env var name (for `env_var`), or `"default"` |
| `precedence_rank` | `int` | Lower is higher priority: `env_var=1`, `file=2`, `default=3` |

**Note**: `ConfigSource` is used for provenance tracking in the startup log entry; it is not persisted or user-visible beyond the structured log.

---

## Deprecation Registry (initial population)

All entries correspond to fields removed in spec 006-notification-alerting:

```
DEPRECATION_REGISTRY = {
    "slack_webhook_url": DeprecationEntry(
        removed_in="006-notification-alerting",
        replacement_path="notifications.channels[].webhook_url",
        migration_hint=(
            "Add a 'notifications: channels:' entry with 'type: slack' "
            "and 'webhook_url: <your-webhook-url>'."
        ),
    ),
    "slack_channel": DeprecationEntry(
        removed_in="006-notification-alerting",
        replacement_path="notifications.channels[].name",
        migration_hint=(
            "The channel name is now the 'name' field of the Slack channel entry "
            "in 'notifications.channels'."
        ),
    ),
    "smtp_host": DeprecationEntry(
        removed_in="006-notification-alerting",
        replacement_path="notifications.channels[].smtp_host",
        migration_hint=(
            "Add a 'notifications: channels:' entry with 'type: email' "
            "and include 'smtp_host' within it."
        ),
    ),
    "smtp_port": DeprecationEntry(
        removed_in="006-notification-alerting",
        replacement_path="notifications.channels[].smtp_port",
        migration_hint="Move 'smtp_port' into the email channel block under 'notifications.channels'.",
    ),
    "smtp_username": DeprecationEntry(
        removed_in="006-notification-alerting",
        replacement_path="notifications.channels[].smtp_username",
        migration_hint="Move 'smtp_username' into the email channel block under 'notifications.channels'.",
    ),
    "smtp_password": DeprecationEntry(
        removed_in="006-notification-alerting",
        replacement_path="notifications.channels[].smtp_password",
        migration_hint="Move 'smtp_password' into the email channel block under 'notifications.channels'.",
    ),
    "notification_email": DeprecationEntry(
        removed_in="006-notification-alerting",
        replacement_path="notifications.channels[].sender_email",
        migration_hint=(
            "Move 'notification_email' to the 'sender_email' field "
            "of the email channel block under 'notifications.channels'."
        ),
    ),
}
```

---

## Config Discovery State Machine

```
discover_config_path(explicit: Path | None) → Path | None

Inputs (checked in order):
  1. explicit             → if provided: must exist as file; error if not found or is directory
  2. COORDINARE_CONFIG_PATH env var → if set: must exist as file; error if not found or is directory
  3. Path.cwd() / "config.yaml" → if exists: return it
  4. Path.home() / ".coordinare" / "config.yaml" → if exists: return it
  5. None                → no file found; caller handles env-vars-only case

State transitions:
  EXPLICIT_PROVIDED ──exists?──→ FOUND(explicit)
                    └─no──→ ERROR("config file not found: {path}")

  ENV_CONFIG_SET ──exists?──→ is_file?──→ FOUND(env_path)
                 │                 └─no──→ ERROR("config path must point to a file, not a directory")
                 └─not_exists──→ ERROR("config file not found: {env_path}")

  CWD_SEARCH ──exists?──→ FOUND(cwd/config.yaml)
              └─no──→ HOME_SEARCH

  HOME_SEARCH ──exists?──→ FOUND(~/.coordinare/config.yaml)
               └─no──→ NONE
```

---

## Validation Sequence

```
validate_config(config_path: Path | None, strict: bool) → ConfigValidationResult

Step 1: Load raw dict from file (or empty dict if config_path is None)
Step 2: pre_validate_raw(raw) → (field_errors, deprecation_warnings)
        - For each key in raw:
            if key in DEPRECATION_REGISTRY → append ConfigDeprecationWarning
            elif key not in ProjectConfiguration.model_fields → append ConfigFieldError(unknown_field)
Step 3: Attempt ProjectConfiguration(**raw) with pydantic-settings
        - Catch ValidationError → extract all errors → append to field_errors as ConfigFieldError
        - On success → compute env_var_fields_count
Step 4: Assemble ConfigValidationResult
        - passed = len(field_errors) == 0
Step 5: Return result (caller decides exit code based on strict flag)
```
