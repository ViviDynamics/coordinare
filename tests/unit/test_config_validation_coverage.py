"""Tests for config_validation.py private helpers (coverage lines 151,154-157,169,171,185,205)."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.config_validation import (
    ErrorType,
    _count_env_var_fields,
    _dotted_path,
    _load_raw_yaml,
    _map_pydantic_error_type,
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When COORDINARE_PROJECT_NAME env var differs from raw, it counts as env-overridden."""
    from coordinare.config import ProjectConfiguration

    monkeypatch.setenv("COORDINARE_PROJECT_NAME", "env-project")
    config = ProjectConfiguration(**_MINIMAL_RAW)
    # raw has "project_name" = "myproject", env says "env-project" → mismatch → count 1
    count = _count_env_var_fields(_MINIMAL_RAW, config)
    assert count >= 1


def test_count_env_var_fields_counts_field_not_in_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A COORDINARE_* env var for a field absent from raw is always counted."""
    from coordinare.config import ProjectConfiguration

    monkeypatch.setenv("COORDINARE_POLL_INTERVAL_SECONDS", "60")
    raw_without_poll = dict(_MINIMAL_RAW)  # no poll_interval_seconds key
    config = ProjectConfiguration(**raw_without_poll)
    count = _count_env_var_fields(raw_without_poll, config)
    assert count >= 1
