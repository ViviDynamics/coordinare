# Research: Configuration Management (008)

**Branch**: `008-config-management` | **Date**: 2026-02-25

---

## R1 — Existing Config Infrastructure

**Decision**: Build on the existing `ProjectConfiguration(BaseSettings)` model; do not replace it.

**Findings**:
- `config.py` uses `pydantic_settings.BaseSettings` with `env_prefix="COORDINARE_"`.
- `settings_customise_sources()` already puts `env_settings` first — env vars override file values natively with zero new code.
- The model currently uses `extra="ignore"` which silently discards unknown YAML keys. Spec 008 requires these to be errors.
- `from_yaml()` loads YAML into a raw dict and passes `**raw` to the constructor; this is the integration seam for the pre-validation layer.
- The flat deprecated fields (`slack_webhook_url`, `slack_channel`, `smtp_host`, `smtp_port`, `smtp_username`, `smtp_password`, `notification_email`) are still in the live model, pending spec 006 removal.

**Rationale**: The existing model is clean and modern (pydantic v2). The gaps are all additive: a pre-validation layer, a discovery function, and a CLI subcommand. No model rewrite needed.

**Alternatives considered**: Replacing with a plain `dataclass` + manual env-var reading — rejected; pydantic-settings already handles the precedence correctly and adds field validation for free.

---

## R2 — Unknown Field Detection Strategy

**Decision**: Pre-validation pass on the raw YAML dict before pydantic model instantiation.

**Findings**:
- Setting `extra="forbid"` on `BaseSettings` would raise a pydantic `ValidationError` for unknown env vars (e.g., `COORDINARE_ANTHROPIC_API_KEY` if a user has additional `COORDINARE_` vars not in the schema), which is undesirable.
- Changing to `extra="forbid"` on the pydantic model also causes issues in tests where `ProjectConfiguration(...)` is called with `**raw` and the raw dict contains deprecated fields that are still present in the YAML but were removed from the model.
- **Chosen approach**: Keep `extra="ignore"` on the pydantic model. Add a separate `pre_validate_raw(raw: dict) -> tuple[list[ConfigFieldError], list[ConfigDeprecationWarning]]` function that:
  1. Computes `known_fields = frozenset(ProjectConfiguration.model_fields.keys())`
  2. Computes `deprecated_fields = frozenset(DEPRECATION_REGISTRY.keys())`
  3. For each top-level key in `raw`:
     - If key in `deprecated_fields` → `ConfigDeprecationWarning`
     - Elif key not in `known_fields` → `ConfigFieldError` (unknown field, suggests closest match)
  4. Pydantic model instantiation then handles missing required fields and type errors.
- Nested fields within known sections (e.g., `notifications.channels[]` from spec 006) are validated by nested model validators that can use `extra="forbid"` at their level.

**Rationale**: Clean separation of concerns. The pre-validation pass runs on the raw YAML dict (file-sourced only); pydantic handles everything else. This avoids spurious "unknown field" errors from extra env vars.

**Alternatives considered**: Subclassing `SettingsConfigDict` with a custom `extra` mode that only applies to YAML sources — too invasive, fragile with pydantic-settings internals.

---

## R3 — Config Discovery Implementation

**Decision**: `discover_config_path(explicit: Path | None) -> Path | None` — a pure function returning the first matching path.

**Search order** (highest to lowest priority):
1. `explicit` (from `--config` CLI flag) — if provided, use it unconditionally (error if not found)
2. `os.environ.get("COORDINARE_CONFIG_PATH")` — if set, use it (error if not found)
3. `Path.cwd() / "config.yaml"` — working directory
4. `Path.home() / ".coordinare" / "config.yaml"` — user home

**Key behaviors**:
- If an explicit path or `COORDINARE_CONFIG_PATH` is provided but does not exist → raise `ConfigDiscoveryError` naming the exact path (fail-fast; operators explicitly said where the config is)
- If `COORDINARE_CONFIG_PATH` points to a directory → raise `ConfigDiscoveryError("config path must point to a file, not a directory")`
- If neither cwd nor home path exists → return `None` (no config file; env vars may still satisfy all required fields)
- Return type `Path | None` allows the caller to handle the "no file, env-vars only" case cleanly

**Rationale**: Mirrors the convention used by git (`.git/config`, `~/.gitconfig`), ssh (`~/.ssh/config`), and Docker (`~/.docker/config.json`). Priority order is unambiguous: explicit > env > cwd > home.

**Alternatives considered**: XDG base directory (`$XDG_CONFIG_HOME/coordinare/config.yaml`) — added as a stretch goal for a later spec; out of scope for 008.

---

## R4 — Deprecation Registry Design

**Decision**: Module-level constant dict `DEPRECATION_REGISTRY: dict[str, DeprecationEntry]` in `config_validation.py`.

**Registry entries (from spec 006 removals)**:
| Old Field | Removed In | Replacement Path | Migration Hint |
|---|---|---|---|
| `slack_webhook_url` | 006-notification-alerting | `notifications.channels[].webhook_url` | Add a `notifications: channels:` entry with `type: slack` |
| `slack_channel` | 006-notification-alerting | `notifications.channels[].name` | Channel name is now the channel's `name` identifier |
| `smtp_host` | 006-notification-alerting | `notifications.channels[].smtp_host` | Add a `notifications: channels:` entry with `type: email` |
| `smtp_port` | 006-notification-alerting | `notifications.channels[].smtp_port` | Part of the email channel config block |
| `smtp_username` | 006-notification-alerting | `notifications.channels[].smtp_username` | Part of the email channel config block |
| `smtp_password` | 006-notification-alerting | `notifications.channels[].smtp_password` | Part of the email channel config block |
| `notification_email` | 006-notification-alerting | `notifications.channels[].sender_email` | Part of the email channel config block |

**Rationale**: A static dict is the simplest data structure; it can be extended with new entries whenever a field is removed in a future spec without any schema migration. Populated at import time, not user-configurable.

**Alternatives considered**: TOML/YAML registry file — adds file I/O and parser dependency; unnecessary for what is effectively a small static lookup table.

---

## R5 — CLI Subcommand Architecture

**Decision**: Add `argparse` subparsers to `__main__.py`. Top-level default behaviour (no subcommand) runs the daemon. `coordinare config validate [--strict] [--config PATH]` is the new subcommand.

**Structure**:
```
coordinare                        # runs daemon (existing behavior)
coordinare config validate        # new subcommand
  --config PATH                  # optional explicit config path
  --strict                       # treat deprecations as errors
```

**Implementation**: `argparse.add_subparsers(dest="command")` with a `config` subparser that has a nested `validate` action via a further subparser or positional argument. Use `parser.set_defaults(func=...)` to dispatch to the appropriate handler function.

**Rationale**: argparse subparsers are the standard Python CLI pattern; no new dependency (typer/click) needed. The existing `_build_arg_parser()` function is refactored to add the `config validate` path without changing the top-level daemon invocation.

**Alternatives considered**: `typer` or `click` — would require a new dependency; constitution Principle I (Minimal dependencies) requires justification. argparse is sufficient for the limited surface area of this CLI.

---

## R6 — Validation Error Collection (Single-Pass)

**Decision**: Collect all errors before returning; never raise on first error.

**Implementation**:
- The pre-validation pass accumulates deprecated and unknown field errors into lists.
- Pydantic v2's `model_validate()` raises a single `ValidationError` that already contains ALL field errors (pydantic v2 collects all validation errors by default, not fail-fast).
- Extract each error from `ValidationError.errors()` and map to `ConfigFieldError(field_path, error_type, fix_hint)`.
- Return a `ConfigValidationResult(errors, warnings, passed)` that the `validate` command renders to stdout.

**Rationale**: Pydantic v2's error collection behaviour makes single-pass reporting straightforward. The `ValidationError.errors()` list gives field location, error type, and message, which map cleanly to the required output format.

**Alternatives considered**: Running validation in multiple phases (first check missing, then check types) — unnecessary; pydantic v2 already does this in one pass.

---

## R7 — Structured Startup Log

**Decision**: A single `structlog` log entry emitted after config is loaded in the daemon startup path, using the existing `structlog` logger.

**Log entry fields**:
```json
{
  "event": "config_loaded",
  "config_file": "/path/to/config.yaml",
  "env_var_fields_count": 3,
  "deprecated_fields_detected": false
}
```
- `config_file`: absolute path of loaded file, or `"none"` if env-vars-only
- `env_var_fields_count`: count of fields resolved from env vars (computed by comparing raw YAML dict values vs resolved model values)
- `deprecated_fields_detected`: boolean, true if any deprecated field was found in the raw YAML

**Rationale**: structlog is already present. A single structured entry at startup is machine-parseable (satisfies SC-006) and adds no overhead.
