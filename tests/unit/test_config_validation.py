"""Tests for config_validation module (008: T002, T009, T012, T022)."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.config_validation import (
    DEPRECATION_REGISTRY,
    ConfigDeprecationWarning,
    ConfigValidationResult,
    ErrorType,
    pre_validate_raw,
    validate_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_YAML = "\n".join([
    'project_name: "Demo"',
    'github_org: "acme"',
    "github_project_number: 12",
    'github_token: "ghp_faketoken"',
    'agent_executable: "/usr/local/bin/agent"',
    'human_reviewers: ["alice"]',
])


def _write_valid_config(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(_VALID_YAML + ("\n" + extra if extra else ""))
    return p


# ---------------------------------------------------------------------------
# US1 tests (T009): core validation logic
# ---------------------------------------------------------------------------


def test_missing_required_field_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing github_token in PAT mode → config validation fails with auth error."""
    monkeypatch.delenv("COORDINARE_GITHUB_TOKEN", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("\n".join([
        'project_name: "Demo"',
        'github_org: "acme"',
        "github_project_number: 12",
        'agent_executable: "/bin/agent"',
        'human_reviewers: ["alice"]',
    ]))
    result = validate_config(config_file)
    assert not result.passed
    # github_token is now optional; the auth model_validator produces the error
    auth_errors = [e for e in result.errors if "github" in e.fix_hint.lower()]
    assert auth_errors, f"Expected auth config error, got: {result.errors}"


def test_type_mismatch_error(tmp_path: Path) -> None:
    """Type mismatch (poll_interval_seconds='abc') → ConfigFieldError with error_type==wrong_type."""
    config_file = _write_valid_config(tmp_path, extra="poll_interval_seconds: abc")
    result = validate_config(config_file)
    assert not result.passed
    type_errs = [e for e in result.errors if "poll_interval_seconds" in e.field_path]
    assert type_errs, f"Expected type error for poll_interval_seconds, got: {result.errors}"
    assert type_errs[0].error_type == ErrorType.wrong_type


def test_all_errors_collected_in_single_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Config missing required fields (github_org, github_token, human_reviewers) → all errors returned in one call."""
    for var in ("COORDINARE_GITHUB_TOKEN", "COORDINARE_GITHUB_ORG", "COORDINARE_HUMAN_REVIEWERS"):
        monkeypatch.delenv(var, raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text('project_name: "Demo"\ngithub_project_number: 12\n')
    result = validate_config(config_file)
    assert not result.passed
    assert len(result.errors) >= 2


def test_valid_config_passes(tmp_path: Path) -> None:
    """Valid config → result.passed == True and no errors."""
    config_file = _write_valid_config(tmp_path)
    result = validate_config(config_file)
    assert result.passed
    assert result.errors == []
    assert result.config_file_path == config_file.resolve()


def test_unknown_field_raises_error(tmp_path: Path) -> None:
    """Config with unrecognised field → ConfigFieldError with error_type==unknown_field."""
    config_file = _write_valid_config(tmp_path, extra="zzzcompletely_bogus_field: 123")
    result = validate_config(config_file)
    assert not result.passed
    unknown = [e for e in result.errors if e.error_type == ErrorType.unknown_field]
    assert unknown, f"Expected unknown_field error, got: {result.errors}"
    assert "zzzcompletely_bogus_field" in unknown[0].field_path


def test_unknown_field_suggests_closest_match(tmp_path: Path) -> None:
    """Near-typo unknown field → fix_hint contains the correct field name suggestion."""
    config_file = _write_valid_config(tmp_path, extra="githubb_token: abc")
    result = validate_config(config_file)
    unknown = [e for e in result.errors if e.error_type == ErrorType.unknown_field]
    assert unknown
    assert "github_token" in unknown[0].fix_hint


def test_truly_unknown_field_no_suggestion(tmp_path: Path) -> None:
    """Field with no close match → fix_hint does NOT contain 'Did you mean'."""
    config_file = _write_valid_config(tmp_path, extra="zzzxxx_completely_unknown: 1")
    result = validate_config(config_file)
    unknown = [e for e in result.errors if e.error_type == ErrorType.unknown_field]
    assert unknown
    assert "Did you mean" not in unknown[0].fix_hint


# ---------------------------------------------------------------------------
# US2 tests (T012): env var overrides
# ---------------------------------------------------------------------------


def test_empty_string_env_var_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COORDINARE_GITHUB_TOKEN='' with no github_token in file → field treated as absent (error)."""
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "")
    config_file = tmp_path / "config.yaml"
    config_file.write_text("\n".join([
        'project_name: "Demo"',
        'github_org: "acme"',
        "github_project_number: 12",
        'human_reviewers: ["alice"]',
    ]))
    result = validate_config(config_file)
    assert not result.passed
    # github_token is now optional; PAT mode with absent/empty token triggers auth model validator
    token_errors = [
        e for e in result.errors
        if "github_token" in e.field_path or "github" in e.fix_hint.lower()
    ]
    assert token_errors, f"Expected error for github_token/auth config, got: {result.errors}"


def test_env_var_fields_count_computed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fields supplied via env var (absent from file) → env_var_fields_count >= 1."""
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "env-token-value")
    # Config file without github_token
    config_file = tmp_path / "config.yaml"
    config_file.write_text("\n".join([
        'project_name: "Demo"',
        'github_org: "acme"',
        "github_project_number: 12",
        'agent_executable: "/bin/agent"',
        'human_reviewers: ["alice"]',
    ]))
    result = validate_config(config_file)
    assert result.passed, f"Expected passed, errors: {result.errors}"
    assert result.env_var_fields_count >= 1


def test_no_file_env_vars_only_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """All required fields supplied via COORDINARE_* env vars with no config file → passed."""
    monkeypatch.setenv("COORDINARE_PROJECT_NAME", "EnvDemo")
    monkeypatch.setenv("COORDINARE_GITHUB_ORG", "envorg")
    monkeypatch.setenv("COORDINARE_GITHUB_PROJECT_NUMBER", "99")
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "ghp_envtoken")
    monkeypatch.setenv("COORDINARE_HUMAN_REVIEWERS", '["bob"]')
    monkeypatch.delenv("COORDINARE_CONFIG_PATH", raising=False)
    # Isolate discovery: point cwd and home to empty directories so no config.yaml is found
    from unittest.mock import patch

    empty = tmp_path / "empty"
    empty.mkdir()
    with (
        patch("coordinare.config_discovery.Path.cwd", return_value=empty),
        patch("coordinare.config_discovery.Path.home", return_value=empty),
    ):
        result = validate_config(None)
    assert result.passed, f"Expected passed with env vars only, errors: {result.errors}"
    assert result.config_file_path is None


# ---------------------------------------------------------------------------
# US4 tests (T022): deprecation registry
# ---------------------------------------------------------------------------


def test_deprecated_field_raises_warning(tmp_path: Path) -> None:
    """Config with slack_webhook_url → ConfigDeprecationWarning for that field."""
    config_file = _write_valid_config(tmp_path, extra="slack_webhook_url: https://hooks.slack.com/x")
    result = validate_config(config_file)
    dep_warnings = [w for w in result.warnings if w.field_name == "slack_webhook_url"]
    assert dep_warnings, f"Expected deprecation warning, got: {result.warnings}"
    assert dep_warnings[0].removed_in == "006-notification-alerting"


def test_all_spec006_fields_in_registry() -> None:
    """All 7 fields removed in spec 006 are keys in DEPRECATION_REGISTRY."""
    expected = {
        "slack_webhook_url",
        "slack_channel",
        "smtp_host",
        "smtp_port",
        "smtp_username",
        "smtp_password",
        "notification_email",
    }
    assert expected <= frozenset(DEPRECATION_REGISTRY.keys())


def test_deprecated_field_not_treated_as_unknown(tmp_path: Path) -> None:
    """Deprecated field (slack_webhook_url) must NOT appear as an unknown_field error."""
    config_file = _write_valid_config(tmp_path, extra="slack_webhook_url: https://hooks.slack.com/x")
    result = validate_config(config_file)
    unknown_errs = [e for e in result.errors if e.error_type == ErrorType.unknown_field]
    assert not any("slack_webhook_url" in e.field_path for e in unknown_errs)


def test_valid_config_no_deprecation_warnings(tmp_path: Path) -> None:
    """Clean post-spec-006 config → result.warnings == []."""
    config_file = _write_valid_config(tmp_path)
    result = validate_config(config_file)
    assert result.warnings == []


def test_validate_config_reports_all_spec006_deprecated_fields_end_to_end(
    tmp_path: Path,
) -> None:
    """Config with all 7 deprecated fields → all 7 appear in result.warnings (SC-002)."""
    deprecated_fields = "\n".join([
        "slack_webhook_url: https://hooks.slack.com/x",
        "slack_channel: '#general'",
        "smtp_host: mail.example.com",
        "smtp_port: 587",
        "smtp_username: user@example.com",
        "smtp_password: secret",
        "notification_email: admin@example.com",
    ])
    config_file = _write_valid_config(tmp_path, extra=deprecated_fields)
    result = validate_config(config_file)
    warned_fields = {w.field_name for w in result.warnings}
    assert len(result.warnings) == 7, f"Expected 7 warnings, got: {warned_fields}"
    expected = {
        "slack_webhook_url", "slack_channel", "smtp_host", "smtp_port",
        "smtp_username", "smtp_password", "notification_email",
    }
    assert warned_fields == expected


def test_pre_validate_raw_no_issues_on_clean_dict() -> None:
    """pre_validate_raw on a dict with only known fields returns empty lists."""
    errors, warnings = pre_validate_raw({"project_name": "x", "github_org": "y"})
    assert errors == []
    assert warnings == []


def test_config_validation_result_passed_strict_property() -> None:
    """passed_strict is True only when both errors and warnings are empty."""
    r = ConfigValidationResult(passed=True, warnings=[])
    assert r.passed_strict is True

    w = ConfigDeprecationWarning("slack_webhook_url", "006", "x", "y")
    r2 = ConfigValidationResult(passed=True, warnings=[w])
    assert r2.passed_strict is False


# ---------------------------------------------------------------------------
# 077 FR-007: performer backend-name validation
# ---------------------------------------------------------------------------


def test_unknown_performer_backend_rejected(tmp_path: Path) -> None:
    """An unknown performers.<role>.backend value fails validation pre-dispatch."""
    config_file = _write_valid_config(
        tmp_path,
        extra="performers:\n  reviewer:\n    backend: bogus\n    model: x\n",
    )
    result = validate_config(config_file)
    assert result.passed is False
    backend_errs = [e for e in result.errors if e.field_path == "performers.reviewer.backend"]
    assert backend_errs, result.errors
    assert backend_errs[0].error_type == ErrorType.invalid_value
    assert "bogus" in backend_errs[0].fix_hint


def test_claude_alias_rejected_in_favor_of_claude_code(tmp_path: Path) -> None:
    """`backend: claude` is NOT a valid alias — canonical name is claude_code.

    Catches the exact 077 config bug (a `claude` performer backend would fail
    get_backend at dispatch).
    """
    config_file = _write_valid_config(
        tmp_path,
        extra="performers:\n  qa:\n    backend: claude\n    model: x\n",
    )
    result = validate_config(config_file)
    assert result.passed is False
    errs = [e for e in result.errors if e.field_path == "performers.qa.backend"]
    assert errs, result.errors
    assert "claude_code" in errs[0].fix_hint


def test_valid_performer_backends_pass(tmp_path: Path) -> None:
    """Canonical backend names (claude_code, codex) produce no backend error."""
    config_file = _write_valid_config(
        tmp_path,
        extra=(
            "performers:\n"
            "  reviewer:\n    backend: claude_code\n    model: x\n"
            "  architect:\n    backend: codex\n    model: x\n"
        ),
    )
    result = validate_config(config_file)
    backend_errs = [e for e in result.errors if e.field_path.endswith(".backend")]
    assert backend_errs == [], backend_errs
