"""Config validation layer: pre-validation pass, deprecation registry, validate_config() (008)."""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "DEPRECATION_REGISTRY",
    "ConfigDeprecationWarning",
    "ConfigFieldError",
    "ConfigValidationResult",
    "DeprecationEntry",
    "ErrorType",
    "_load_raw_yaml",
    "pre_validate_raw",
    "validate_config",
]


# ---------------------------------------------------------------------------
# Type definitions (T002)
# ---------------------------------------------------------------------------


@dataclass
class DeprecationEntry:
    removed_in: str
    replacement_path: str
    migration_hint: str


class ErrorType(StrEnum):
    missing = "missing"
    wrong_type = "wrong_type"
    unknown_field = "unknown_field"
    invalid_value = "invalid_value"


@dataclass
class ConfigFieldError:
    field_path: str
    error_type: ErrorType
    fix_hint: str
    source: str | None = None


@dataclass
class ConfigDeprecationWarning:
    field_name: str
    removed_in: str
    replacement_path: str
    migration_hint: str


@dataclass
class ConfigValidationResult:
    errors: list[ConfigFieldError] = field(default_factory=list)
    warnings: list[ConfigDeprecationWarning] = field(default_factory=list)
    config_file_path: Path | None = None
    env_var_fields_count: int = 0
    passed: bool = True

    @property
    def passed_strict(self) -> bool:
        return self.passed and not self.warnings


# ---------------------------------------------------------------------------
# Deprecation registry — spec-006 removals (T019)
# ---------------------------------------------------------------------------

DEPRECATION_REGISTRY: dict[str, DeprecationEntry] = {
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
        migration_hint=(
            "Move 'smtp_port' into the email channel block under 'notifications.channels'."
        ),
    ),
    "smtp_username": DeprecationEntry(
        removed_in="006-notification-alerting",
        replacement_path="notifications.channels[].smtp_username",
        migration_hint=(
            "Move 'smtp_username' into the email channel block under 'notifications.channels'."
        ),
    ),
    "smtp_password": DeprecationEntry(
        removed_in="006-notification-alerting",
        replacement_path="notifications.channels[].smtp_password",
        migration_hint=(
            "Move 'smtp_password' into the email channel block under 'notifications.channels'."
        ),
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_raw_yaml(path: Path) -> dict:
    """Load a YAML config file and return its content as a dict.

    Raises FileNotFoundError if path does not exist or is not a file.
    Raises OSError if the file cannot be read or is not valid YAML.
    T006/T017 both call this helper so YAML loading is not inlined anywhere.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    try:
        loaded = yaml.safe_load(os.path.expandvars(path.read_text()))
    except (yaml.YAMLError, OSError) as exc:
        raise OSError(f"Failed to read config file {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise OSError(
            f"Config file {path} must contain a YAML mapping at the top level, "
            f"got {type(loaded).__name__}"
        )
    return loaded


def _dotted_path(loc: tuple) -> str:
    """Convert a pydantic error loc tuple to a dotted field path string."""
    result = ""
    for item in loc:
        if isinstance(item, int):
            result += f"[{item}]"
        elif result:
            result += f".{item}"
        else:
            result = str(item)
    return result


def _map_pydantic_error_type(pydantic_type: str) -> ErrorType:
    if "missing" in pydantic_type:
        return ErrorType.missing
    # "int_type", "str_type", "bool_type", etc. → wrong_type
    # "int_parsing", "float_parsing", "decimal_parsing", etc. → wrong_type
    # "pars" matches "parsing" (note: "parse" is NOT a substring of "parsing")
    if "type" in pydantic_type or "pars" in pydantic_type:
        return ErrorType.wrong_type
    return ErrorType.invalid_value


def _count_env_var_fields(raw: dict, config: object) -> int:
    """Count top-level scalar fields resolved from COORDINARE_* env vars (T011/DD-3)."""
    from coordinare.config import ProjectConfiguration

    count = 0
    for field_name in ProjectConfiguration.model_fields:
        env_var = f"COORDINARE_{field_name.upper()}"
        env_val = os.environ.get(env_var, "")
        if not env_val:
            continue
        if field_name not in raw:
            count += 1
        else:
            resolved = getattr(config, field_name, None)
            if hasattr(resolved, "get_secret_value"):
                resolved_str = resolved.get_secret_value()
            else:
                resolved_str = str(resolved) if resolved is not None else ""
            if str(raw.get(field_name, "")) != resolved_str:
                count += 1
    return count


# ---------------------------------------------------------------------------
# Pre-validation pass (T005 / T020)
# ---------------------------------------------------------------------------


def pre_validate_raw(
    raw: dict,
) -> tuple[list[ConfigFieldError], list[ConfigDeprecationWarning]]:
    """Check raw YAML dict for deprecated and unknown fields before pydantic validation.

    Returns (field_errors, deprecation_warnings).
    Deprecated fields are reported as warnings, not as unknown-field errors.
    """
    from coordinare.config import ProjectConfiguration

    known_fields = frozenset(ProjectConfiguration.model_fields.keys())
    deprecated_keys = frozenset(DEPRECATION_REGISTRY.keys())
    all_known = known_fields | deprecated_keys

    errors: list[ConfigFieldError] = []
    warnings: list[ConfigDeprecationWarning] = []

    for key in raw:
        if key in deprecated_keys:
            entry = DEPRECATION_REGISTRY[key]
            warnings.append(
                ConfigDeprecationWarning(
                    field_name=key,
                    removed_in=entry.removed_in,
                    replacement_path=entry.replacement_path,
                    migration_hint=entry.migration_hint,
                )
            )
        elif key not in all_known:
            matches = difflib.get_close_matches(key, known_fields, n=1, cutoff=0.6)
            if matches:
                hint = (
                    f"Field '{key}' is not a recognised configuration field. "
                    f"Did you mean '{matches[0]}'? Remove or rename this field."
                )
            else:
                hint = (
                    f"Field '{key}' is not a recognised configuration field. "
                    "Remove or rename this field."
                )
            errors.append(
                ConfigFieldError(
                    field_path=key,
                    error_type=ErrorType.unknown_field,
                    fix_hint=hint,
                    source="file",
                )
            )

    return errors, warnings


# ---------------------------------------------------------------------------
# Main validation function (T006 / T011 / T016)
# ---------------------------------------------------------------------------


def validate_config(
    config_path: Path | None,
) -> ConfigValidationResult:
    """Validate the resolved configuration (file + env vars) in a single pass.

    Calls discover_config_path(config_path) internally (T016).
    Returns ConfigValidationResult — never raises.
    Strict-mode semantics (treating warnings as errors) are the caller's responsibility.
    """
    from coordinare.config_discovery import ConfigDiscoveryError, discover_config_path

    # Step 1: Discover config file path
    try:
        resolved_path = discover_config_path(config_path)
    except ConfigDiscoveryError as exc:
        return ConfigValidationResult(
            errors=[
                ConfigFieldError(
                    field_path="config_file",
                    error_type=ErrorType.missing,
                    fix_hint=str(exc),
                    source=None,
                )
            ],
            passed=False,
        )

    # Step 2: Load raw YAML (empty dict if no file)
    raw: dict = {}
    if resolved_path is not None:
        try:
            raw = _load_raw_yaml(resolved_path)
        except FileNotFoundError as exc:
            return ConfigValidationResult(
                errors=[
                    ConfigFieldError(
                        field_path="config_file",
                        error_type=ErrorType.missing,
                        fix_hint=str(exc),
                        source=None,
                    )
                ],
                passed=False,
            )
        except OSError as exc:
            return ConfigValidationResult(
                errors=[
                    ConfigFieldError(
                        field_path="config_file",
                        error_type=ErrorType.invalid_value,
                        fix_hint=str(exc),
                        source=None,
                    )
                ],
                passed=False,
            )

    # Step 3: Pre-validation pass (deprecated + unknown fields)
    pre_errors, pre_warnings = pre_validate_raw(raw)

    # Step 4: Attempt pydantic model instantiation.
    # env_ignore_empty=True in ProjectConfiguration.model_config ensures that
    # empty COORDINARE_* env vars are treated as absent (not as values).
    pydantic_errors: list[ConfigFieldError] = []
    config_obj = None
    env_var_fields_count = 0

    try:
        from coordinare.config import ProjectConfiguration

        config_obj = ProjectConfiguration(**raw)
        env_var_fields_count = _count_env_var_fields(raw, config_obj)
    except ValidationError as exc:
        for err in exc.errors():
            loc = err.get("loc", ())
            field_path = _dotted_path(loc) if loc else "unknown"
            pydantic_type = str(err.get("type", ""))
            error_type = _map_pydantic_error_type(pydantic_type)

            # Determine source: env var or file
            source: str | None = None
            if loc:
                top_field = str(loc[0])
                env_var_name = f"COORDINARE_{top_field.upper()}"
                if os.environ.get(env_var_name):
                    source = f"env_var:{env_var_name}"
                elif field_path in raw or top_field in raw:
                    source = "file"

            pydantic_errors.append(
                ConfigFieldError(
                    field_path=field_path,
                    error_type=error_type,
                    fix_hint=str(err.get("msg", "")),
                    source=source,
                )
            )

    all_errors = pre_errors + pydantic_errors
    return ConfigValidationResult(
        errors=all_errors,
        warnings=pre_warnings,
        config_file_path=resolved_path,
        env_var_fields_count=env_var_fields_count,
        passed=len(all_errors) == 0,
    )
