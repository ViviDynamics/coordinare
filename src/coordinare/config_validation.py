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
    "coerce_multi_symphony_raw",
    "is_multi_symphony_config",
    "pre_validate_raw",
    "validate_config",
    "wrap_legacy_config",
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


def is_multi_symphony_config(raw: dict) -> bool:
    """Return True if the config uses the multi-symphony format (spec 057).

    An empty or absent 'symphonies' key falls back to single-symphony mode per spec FR-001.
    A present but non-list value (including null) is treated as multi-symphony so Pydantic
    surfaces a type error rather than silently ignoring the key.
    """
    if "symphonies" not in raw:
        return False
    val = raw["symphonies"]
    # Empty list → legacy; non-list or non-empty list → multi-symphony
    return not (isinstance(val, list) and len(val) == 0)


def coerce_multi_symphony_raw(raw: dict) -> dict:
    """Build the CoordinareConfiguration constructor dict from a multi-symphony raw config.

    Separates the symphony/orchestra top-level keys from the global config fields.
    """
    _sym_keys = frozenset({"symphonies", "orchestra"})
    return {
        "global_config": {k: v for k, v in raw.items() if k not in _sym_keys},
        "symphonies": raw.get("symphonies", []),
        "orchestra": raw.get("orchestra", {"mode": "shared_pool", "performers": []}),
    }


def wrap_legacy_config(raw: dict) -> dict:
    """Auto-wrap a legacy single-project config as a CoordinareConfiguration dict.

    Takes a legacy flat ProjectConfiguration dict and wraps it in a
    CoordinareConfiguration with a single 'default' symphony (underscore-free
    to pass validation regex; context default is "__default__" for backwards compat).
    """
    global_cfg = dict(raw)
    _env_pn = os.environ.get("COORDINARE_GITHUB_PROJECT_NUMBER", "") or ""
    try:
        project_number = global_cfg.get("github_project_number") or (int(_env_pn) if _env_pn else 0)
    except ValueError:
        project_number = 0
    return {
        "global_config": global_cfg,
        "symphonies": [
            {
                "name": "default",
                # Use placeholder 1 when value is absent/zero so SymphonyConfig(ge=1)
                # passes schema construction. validate_config() reports the missing field
                # as a ConfigFieldError separately.
                "github_project_number": project_number or 1,
            }
        ],
        "orchestra": {"mode": "shared_pool", "performers": []},
    }


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
    # Allow 057 top-level keys alongside global config fields, and the
    # 076 dispatcher_dedup top-level block.
    multi_symphony_keys = frozenset({"symphonies", "orchestra", "dispatcher_dedup"})
    all_known = known_fields | deprecated_keys | multi_symphony_keys

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

    # Fail validation when 'orchestra' is present without 'symphonies': the orchestra block
    # would be silently discarded because is_multi_symphony_config() returns False and
    # wrap_legacy_config() injects a default orchestra, overwriting the user-supplied one.
    symphonies_val = raw.get("symphonies")
    symphonies_absent = "symphonies" not in raw or (isinstance(symphonies_val, list) and not symphonies_val)
    if "orchestra" in raw and symphonies_absent:
        errors.append(
            ConfigFieldError(
                field_path="orchestra",
                error_type=ErrorType.invalid_value,
                fix_hint=(
                    "Field 'orchestra' is only valid in multi-symphony configs (when 'symphonies' "
                    "is also present). Without 'symphonies', the 'orchestra' block is ignored. "
                    "Add a 'symphonies' section or remove 'orchestra'."
                ),
                source="file",
            )
        )

    return errors, warnings


# ---------------------------------------------------------------------------
# Main validation function (T006 / T011 / T016)
# ---------------------------------------------------------------------------


# 077 FR-007: canonical performer-backend names. MUST stay in sync with the
# performer factory's ``supported_backends`` keys
# (agent/performer/src/performer/backends/__init__.py:get_backend). There is NO
# ``claude`` alias — the claude_code backend's canonical key is ``claude_code``
# (a config using ``backend: claude`` fails get_backend at dispatch). Validating
# this pre-dispatch turns a late in-container failure into an actionable config
# error. (Add ``pi`` here when the Pi backend lands — 077 T015.)
SUPPORTED_PERFORMER_BACKENDS: frozenset[str] = frozenset(
    {"opencode", "opencode_compat", "junie", "claude_code", "codex", "hermes", "pi", "openclaw"}
)


def _validate_performer_backends(raw: dict) -> list[ConfigFieldError]:
    """077 FR-007: reject an unknown ``performers.<role>.backend`` value before
    dispatch (it is the authoritative ``get_backend`` argument via score.backend).

    The endpoint ``env.BACKEND`` is intentionally NOT validated here — it is only
    a container default that ``score.backend`` overrides, and proven configs set
    it to non-canonical aliases (e.g. ``BACKEND: claude``) harmlessly.
    """
    errors: list[ConfigFieldError] = []
    performers = raw.get("performers")
    if not isinstance(performers, dict):
        return errors
    for role, cfg in performers.items():
        if not isinstance(cfg, dict):
            continue
        backend = cfg.get("backend")
        if backend is None:
            continue
        normalized = str(backend).replace("-", "_").lower()  # mirror performer normalization
        if normalized not in SUPPORTED_PERFORMER_BACKENDS:
            supported = ", ".join(sorted(SUPPORTED_PERFORMER_BACKENDS))
            errors.append(
                ConfigFieldError(
                    field_path=f"performers.{role}.backend",
                    error_type=ErrorType.invalid_value,
                    fix_hint=(
                        f"Unknown performer backend {backend!r}. Supported: {supported}. "
                        f"(Note: the claude_code backend's canonical name is 'claude_code', not 'claude'.)"
                    ),
                    source="file",
                )
            )
    return errors


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
        from coordinare.config import CoordinareConfiguration

        # Check if this is a multi-symphony format or legacy single-project format
        if is_multi_symphony_config(raw):
            # Multi-symphony format: root-level keys are global config; extract symphonies/orchestra.
            coerced = coerce_multi_symphony_raw(raw)
            coordinare_cfg = CoordinareConfiguration(**coerced)
            config_obj = coordinare_cfg.global_config
            env_var_fields_count = _count_env_var_fields(coerced["global_config"], config_obj)
            # Validate that each symphony's overrides can merge into a valid effective
            # config. effective_config() raises ValueError on unknown override keys;
            # catching it here keeps the contract: validate_config() pass ⇒ no startup crash.
            for _sym_idx, sym in enumerate(coordinare_cfg.symphonies):
                try:
                    sym.effective_config(coordinare_cfg.global_config)
                except ValueError as _eff_exc:
                    pydantic_errors.append(
                        ConfigFieldError(
                            field_path=f"symphonies[{_sym_idx}].overrides",
                            error_type=ErrorType.invalid_value,
                            fix_hint=str(_eff_exc),
                            source="file",
                        )
                    )
                except ValidationError as _val_exc:
                    for _verr in _val_exc.errors():
                        _loc = _verr.get("loc", ())
                        _field = _dotted_path(_loc) if _loc else "unknown"
                        pydantic_errors.append(
                            ConfigFieldError(
                                field_path=f"symphonies[{_sym_idx}].overrides.{_field}",
                                error_type=_map_pydantic_error_type(str(_verr.get("type", ""))),
                                fix_hint=str(_verr.get("msg", "")),
                                source="file",
                            )
                        )
        else:
            # Legacy format: auto-wrap and parse.
            # Check github_project_number BEFORE wrapping — SymphonyConfig.github_project_number
            # has Field(ge=1), so a missing/zero value would cause CoordinareConfiguration(**wrapped)
            # to raise ValidationError before config_obj is available, producing a confusing
            # symphonies[0].github_project_number path instead of the friendly fix_hint below.
            _env_lpn = os.environ.get("COORDINARE_GITHUB_PROJECT_NUMBER", "") or ""
            _project_number_parse_failed = False
            try:
                _legacy_project_number = raw.get("github_project_number") or (
                    int(_env_lpn) if _env_lpn else 0
                )
            except ValueError:
                _legacy_project_number = 0
                _project_number_parse_failed = True
                pydantic_errors.append(
                    ConfigFieldError(
                        field_path="github_project_number",
                        error_type=ErrorType.wrong_type,
                        fix_hint=(
                            "COORDINARE_GITHUB_PROJECT_NUMBER must be an integer. "
                            f"Got: {_env_lpn!r}"
                        ),
                        source="env_var:COORDINARE_GITHUB_PROJECT_NUMBER",
                    )
                )
            if not _legacy_project_number:
                if not _project_number_parse_failed:
                    pydantic_errors.append(
                        ConfigFieldError(
                            field_path="github_project_number",
                            error_type=ErrorType.missing,
                            fix_hint=(
                                "Field 'github_project_number' is required in legacy single-project mode. "
                                "Set it in your config file or via COORDINARE_GITHUB_PROJECT_NUMBER."
                            ),
                            source=None,
                        )
                    )
                # Inject placeholder so wrap_legacy_config produces a valid symphony dict
                # (ge=1 must pass for CoordinareConfiguration construction to succeed).
                # Rebind to a new dict — does not affect _count_env_var_fields because
                # github_project_number isn't in COORDINARE_* env vars when we reach here.
                raw = {**raw, "github_project_number": 1}
            wrapped = wrap_legacy_config(raw)
            coordinare_cfg = CoordinareConfiguration(**wrapped)
            config_obj = coordinare_cfg.global_config
            env_var_fields_count = _count_env_var_fields(raw, config_obj)
            # In legacy mode, project_name must also be set — it cannot be derived
            # from a symphony name as in multi-symphony mode.
            if not config_obj.project_name:
                pydantic_errors.append(
                    ConfigFieldError(
                        field_path="project_name",
                        error_type=ErrorType.missing,
                        fix_hint=(
                            "Field 'project_name' is required in legacy single-project mode. "
                            "Set it in your config file or via COORDINARE_PROJECT_NAME."
                        ),
                        source=None,
                    )
                )
    except ValidationError as exc:
        for err in exc.errors():
            loc = err.get("loc", ())
            field_path = _dotted_path(loc) if loc else "unknown"
            pydantic_type = str(err.get("type", ""))
            error_type = _map_pydantic_error_type(pydantic_type)

            # Determine source: env var or file
            source: str | None = None
            if loc:
                # For CoordinareConfiguration validation errors, the path starts with
                # "global_config" then the field name — unwrap one level.
                if len(loc) > 1 and loc[0] == "global_config":
                    top_field = str(loc[1])
                else:
                    top_field = str(loc[0])
                env_var_name = f"COORDINARE_{top_field.upper()}"

                if os.environ.get(env_var_name):
                    source = f"env_var:{env_var_name}"
                elif top_field in raw:
                    source = "file"

            pydantic_errors.append(
                ConfigFieldError(
                    field_path=field_path,
                    error_type=error_type,
                    fix_hint=str(err.get("msg", "")),
                    source=source,
                )
            )

    # 077 FR-007: reject unknown performer-backend names pre-dispatch.
    backend_errors = _validate_performer_backends(raw)

    all_errors = pre_errors + pydantic_errors + backend_errors
    return ConfigValidationResult(
        errors=all_errors,
        warnings=pre_warnings,
        config_file_path=resolved_path,
        env_var_fields_count=env_var_fields_count,
        passed=len(all_errors) == 0,
    )
