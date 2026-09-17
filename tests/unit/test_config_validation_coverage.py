"""Tests for config_validation.py private helpers (coverage lines 151,154-157,169,171,185,205,317-325)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coordinare.config_validation import (
    ErrorType,
    _count_env_var_fields,
    _dotted_path,
    _load_raw_yaml,
    _map_pydantic_error_type,
    validate_config,
)

# ---------------------------------------------------------------------------
# _load_raw_yaml — error paths (lines 151, 154-157)
# ---------------------------------------------------------------------------


def test_load_raw_yaml_raises_file_not_found_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        _load_raw_yaml(tmp_path / "nonexistent.yaml")


def test_load_raw_yaml_raises_os_error_for_invalid_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(": : : invalid yaml ::")
    with pytest.raises(OSError, match="Failed to read config file"):
        _load_raw_yaml(bad)


def test_load_raw_yaml_raises_os_error_for_non_mapping_yaml(tmp_path: Path) -> None:
    """YAML that parses but is not a dict (e.g. a plain list) must raise OSError."""
    bad = tmp_path / "list.yaml"
    bad.write_text("- item1\n- item2\n")
    with pytest.raises(OSError, match="must contain a YAML mapping"):
        _load_raw_yaml(bad)


# ---------------------------------------------------------------------------
# _dotted_path — integer index and multi-segment paths (lines 169, 171)
# ---------------------------------------------------------------------------


def test_dotted_path_single_field() -> None:
    assert _dotted_path(("project_name",)) == "project_name"


def test_dotted_path_nested() -> None:
    assert _dotted_path(("resilience", "github_retry", "attempts")) == "resilience.github_retry.attempts"


def test_dotted_path_integer_index() -> None:
    """Integer items (list indices) are formatted as [N] not .N."""
    assert _dotted_path(("human_reviewers", 0)) == "human_reviewers[0]"


def test_dotted_path_mixed_integer_and_string() -> None:
    assert _dotted_path(("channels", 2, "name")) == "channels[2].name"


# ---------------------------------------------------------------------------
# _map_pydantic_error_type — fallback path (line 185)
# ---------------------------------------------------------------------------


def test_map_pydantic_error_type_missing() -> None:
    assert _map_pydantic_error_type("missing") == ErrorType.missing


def test_map_pydantic_error_type_int_type() -> None:
    assert _map_pydantic_error_type("int_type") == ErrorType.wrong_type


def test_map_pydantic_error_type_parsing() -> None:
    assert _map_pydantic_error_type("int_parsing") == ErrorType.wrong_type


def test_map_pydantic_error_type_fallback_returns_invalid_value() -> None:
    """An unrecognised pydantic type string falls back to invalid_value."""
    assert _map_pydantic_error_type("value_error") == ErrorType.invalid_value
    assert _map_pydantic_error_type("url_parsing") == ErrorType.wrong_type


# ---------------------------------------------------------------------------
# _count_env_var_fields — non-secret non-None path (line 205)
# ---------------------------------------------------------------------------


_MINIMAL_RAW = {
    "project_name": "myproject",
    "github_org": "myorg",
    "github_project_number": 1,
    "github_token": "ghp_test_token_value",
    "human_reviewers": ["reviewer1"],
}


def test_count_env_var_fields_counts_env_override_for_string_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When COORDINARE_PROJECT_NAME env var differs from raw, it counts as env-overridden."""
    from coordinare.config import ProjectConfiguration

    monkeypatch.setenv("COORDINARE_PROJECT_NAME", "env-project")
    config = ProjectConfiguration(**_MINIMAL_RAW)
    # raw has "project_name" = "myproject", env says "env-project" → mismatch → count 1
    count = _count_env_var_fields(_MINIMAL_RAW, config)
    assert count >= 1


def test_count_env_var_fields_counts_field_not_in_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A COORDINARE_* env var for a field absent from raw is always counted."""
    from coordinare.config import ProjectConfiguration

    monkeypatch.setenv("COORDINARE_POLL_INTERVAL_SECONDS", "60")
    raw_without_poll = dict(_MINIMAL_RAW)  # no poll_interval_seconds key
    config = ProjectConfiguration(**raw_without_poll)
    count = _count_env_var_fields(raw_without_poll, config)
    assert count >= 1


# ---------------------------------------------------------------------------
# validate_config — OSError path when loading YAML (lines 317-325)
# ---------------------------------------------------------------------------


def test_validate_config_returns_failure_on_os_error_loading_yaml(tmp_path: Path) -> None:
    """When _load_raw_yaml raises OSError validate_config returns passed=False."""
    # Create a real file so discovery succeeds and _load_raw_yaml is called
    config_file = tmp_path / "config.yaml"
    config_file.write_text("project_name: test\n")

    with patch(
        "coordinare.config_validation._load_raw_yaml",
        side_effect=OSError("disk read error"),
    ):
        result = validate_config(config_file)

    assert result.passed is False
    assert len(result.errors) >= 1
    error = result.errors[0]
    assert error.error_type == ErrorType.invalid_value
    # The fix_hint should contain the OSError message
    assert "disk read error" in error.fix_hint


def test_validate_config_returns_failure_on_file_not_found(tmp_path: Path) -> None:
    """FileNotFoundError during YAML load → ConfigValidationResult with passed=False (line 306)."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("project_name: test\n")

    with patch(
        "coordinare.config_validation._load_raw_yaml",
        side_effect=FileNotFoundError("no such file"),
    ):
        result = validate_config(config_file)

    assert result.passed is False
    assert len(result.errors) >= 1
    assert result.errors[0].error_type == ErrorType.missing


# ---------------------------------------------------------------------------
# _count_env_var_fields — False branch at line 206 (raw value == resolved value)
# ---------------------------------------------------------------------------


def test_count_env_var_fields_no_increment_when_values_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Line 206->193: when the YAML raw value equals the resolved config value,
    count is NOT incremented and the loop continues (False branch of line 206)."""

    from coordinare.config import ProjectConfiguration

    # Build a minimal valid config so from_yaml succeeds
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "project_name: Demo\n"
        "github_org: acme\n"
        "github_project_number: 1\n"
        "github_token: tok\n"
        "human_reviewers:\n"
        "  - alice\n",
    )
    config = ProjectConfiguration.from_yaml(config_yaml)

    # Set an env var whose value already matches the config (raw == resolved → no increment)
    monkeypatch.setenv("COORDINARE_PROJECT_NAME", "Demo")

    raw = {"project_name": "Demo"}
    count = _count_env_var_fields(raw, config)
    # project_name is in raw AND raw value "Demo" == resolved "Demo" → not counted
    assert count == 0
