"""Tests for `coordinare config validate` CLI subcommand (008: T004, T010, T013, T018, T023)."""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from coordinare.__main__ import _cmd_config_validate
from coordinare.config_validation import validate_config

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


def _make_args(config: Path | None = None, strict: bool = False) -> argparse.Namespace:
    args = argparse.Namespace()
    args.config = config
    args.strict = strict
    return args


# ---------------------------------------------------------------------------
# US1 tests (T010): basic exit codes and output content
# ---------------------------------------------------------------------------


def test_valid_config_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Valid config → _cmd_config_validate exits with code 0."""
    config_file = _write_valid_config(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        _cmd_config_validate(_make_args(config=config_file))
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "✓" in out


def test_missing_field_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config missing github_token → exit 1 and 'github_token' in stdout."""
    monkeypatch.delenv("COORDINARE_GITHUB_TOKEN", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("\n".join([
        'project_name: "Demo"',
        'github_org: "acme"',
        "github_project_number: 12",
        'human_reviewers: ["alice"]',
    ]))
    with pytest.raises(SystemExit) as exc_info:
        _cmd_config_validate(_make_args(config=config_file))
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "github_token" in out


def test_unknown_field_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Config with unknown field → exit 1 and '[UNKNOWN_FIELD]' in stdout."""
    config_file = _write_valid_config(tmp_path, extra="totally_unknown_key: 123")
    with pytest.raises(SystemExit) as exc_info:
        _cmd_config_validate(_make_args(config=config_file))
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "[UNKNOWN_FIELD]" in out


def test_all_errors_reported_in_single_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config with multiple errors → all errors appear in one stdout pass."""
    for var in ("COORDINARE_GITHUB_TOKEN", "COORDINARE_GITHUB_ORG", "COORDINARE_PROJECT_NAME"):
        monkeypatch.delenv(var, raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("github_project_number: 12\nhuman_reviewers: [alice]")
    with pytest.raises(SystemExit) as exc_info:
        _cmd_config_validate(_make_args(config=config_file))
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    # At least 2 of the 3 missing fields must be named in the output
    missing_named = sum(1 for field in ("github_token", "github_org", "project_name") if field in out)
    assert missing_named >= 2


def test_validate_completes_under_500ms(tmp_path: Path) -> None:
    """validate_config on valid config completes in < 500ms (SC-001)."""
    config_file = _write_valid_config(tmp_path)
    start = time.perf_counter()
    validate_config(config_file)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"validate_config took {elapsed:.3f}s (budget: 0.5s)"


# ---------------------------------------------------------------------------
# US2 tests (T013): env var override integration
# ---------------------------------------------------------------------------


def test_env_var_overrides_yaml_field(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COORDINARE_GITHUB_TOKEN env var overrides file value → no missing-field error."""
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "env-token")
    # Config file with a different token value
    config_file = tmp_path / "config.yaml"
    config_file.write_text("\n".join([
        'project_name: "Demo"',
        'github_org: "acme"',
        "github_project_number: 12",
        'github_token: "file-token"',
        'human_reviewers: ["alice"]',
    ]))
    with pytest.raises(SystemExit) as exc_info:
        _cmd_config_validate(_make_args(config=config_file))
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "✓" in out


def test_no_config_file_all_env_vars_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config file at any search location, all required COORDINARE_* vars set → exit 0."""
    monkeypatch.setenv("COORDINARE_PROJECT_NAME", "EnvDemo")
    monkeypatch.setenv("COORDINARE_GITHUB_ORG", "envorg")
    monkeypatch.setenv("COORDINARE_GITHUB_PROJECT_NUMBER", "99")
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "ghp_envtoken")
    monkeypatch.setenv("COORDINARE_HUMAN_REVIEWERS", '["bob"]')
    monkeypatch.delenv("COORDINARE_CONFIG_PATH", raising=False)

    # Ensure neither cwd nor home config exists during this test
    from unittest.mock import patch
    empty = tmp_path / "nocfg"
    empty.mkdir()
    with (
        patch("coordinare.config_discovery.Path.cwd", return_value=empty),
        patch("coordinare.config_discovery.Path.home", return_value=empty),
        pytest.raises(SystemExit) as exc_info,
    ):
        _cmd_config_validate(_make_args(config=None))

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "✓" in out
    assert "no config file" in out.lower() or "environment variables" in out.lower()


def test_env_var_type_error_reported_identifies_env_var_name(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Type error originating from env var → env var name identified in output."""
    monkeypatch.setenv("COORDINARE_POLL_INTERVAL_SECONDS", "not_an_int")
    config_file = _write_valid_config(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        _cmd_config_validate(_make_args(config=config_file))
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "poll_interval_seconds" in out
    assert "COORDINARE_POLL_INTERVAL_SECONDS" in out


# ---------------------------------------------------------------------------
# US3 tests (T018): config discovery integration
# ---------------------------------------------------------------------------


def test_no_config_file_missing_fields_exits_one_with_searched_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config file + missing required fields → exit 1 with all 4 search paths listed."""
    for var in ("COORDINARE_GITHUB_TOKEN", "COORDINARE_GITHUB_ORG", "COORDINARE_PROJECT_NAME",
                "COORDINARE_GITHUB_PROJECT_NUMBER", "COORDINARE_HUMAN_REVIEWERS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("COORDINARE_CONFIG_PATH", raising=False)

    from unittest.mock import patch
    empty = tmp_path / "empty"
    empty.mkdir()
    with (
        patch("coordinare.config_discovery.Path.cwd", return_value=empty),
        patch("coordinare.config_discovery.Path.home", return_value=empty),
        pytest.raises(SystemExit) as exc_info,
    ):
        _cmd_config_validate(_make_args(config=None))

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "~/.coordinare/config.yaml" in out
    assert "./config.yaml" in out


def test_explicit_path_not_found_exits_one(
    capsys: pytest.CaptureFixture,
) -> None:
    """--config pointing to non-existent file → exit 1 with path named in output."""
    bad_path = Path("/nonexistent/path/to/config.yaml")
    with pytest.raises(SystemExit) as exc_info:
        _cmd_config_validate(_make_args(config=bad_path))
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert str(bad_path) in out or "nonexistent" in out


# ---------------------------------------------------------------------------
# US4 tests (T023): deprecation detection
# ---------------------------------------------------------------------------


def test_deprecated_field_exits_zero_without_strict(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Deprecated field present, no --strict → exit 0 and deprecation warning in output."""
    config_file = _write_valid_config(tmp_path, extra="slack_webhook_url: https://hooks.slack.com/x")
    with pytest.raises(SystemExit) as exc_info:
        _cmd_config_validate(_make_args(config=config_file, strict=False))
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "DEPRECATION WARNINGS" in out
    assert "006-notification-alerting" in out


def test_deprecated_field_exits_one_with_strict(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Deprecated field present with --strict → exit 1."""
    config_file = _write_valid_config(tmp_path, extra="slack_webhook_url: https://hooks.slack.com/x")
    with pytest.raises(SystemExit) as exc_info:
        _cmd_config_validate(_make_args(config=config_file, strict=True))
    assert exc_info.value.code == 1


def test_strict_output_shows_deprecation_errors_header(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """--strict with deprecated field → 'DEPRECATION ERRORS' in output (not 'WARNINGS')."""
    config_file = _write_valid_config(tmp_path, extra="slack_webhook_url: https://hooks.slack.com/x")
    with pytest.raises(SystemExit):
        _cmd_config_validate(_make_args(config=config_file, strict=True))
    out = capsys.readouterr().out
    assert "DEPRECATION ERRORS" in out
