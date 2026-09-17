"""Tests for config_discovery module (008: T004, T015)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coordinare.config_discovery import ConfigDiscoveryError, discover_config_path


def _write_yaml(path: Path) -> Path:
    path.write_text("project_name: test\n")
    return path


# ---------------------------------------------------------------------------
# Explicit path (--config flag)
# ---------------------------------------------------------------------------


def test_explicit_path_used_when_provided(tmp_path: Path) -> None:
    """Explicit path to existing file → returns resolved path."""
    cfg = _write_yaml(tmp_path / "my.yaml")
    result = discover_config_path(cfg)
    assert result == cfg.resolve()


def test_explicit_path_error_when_not_found() -> None:
    """Explicit path that does not exist → raises ConfigDiscoveryError."""
    with pytest.raises(ConfigDiscoveryError, match="not found"):
        discover_config_path(Path("/nonexistent/path/config.yaml"))


def test_explicit_path_error_when_directory(tmp_path: Path) -> None:
    """Explicit path pointing to a directory → raises ConfigDiscoveryError with 'directory'."""
    with pytest.raises(ConfigDiscoveryError, match="directory"):
        discover_config_path(tmp_path)


# ---------------------------------------------------------------------------
# COORDINARE_CONFIG_PATH env var
# ---------------------------------------------------------------------------


def test_env_var_config_path_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COORDINARE_CONFIG_PATH pointing to existing file → returns resolved path."""
    cfg = _write_yaml(tmp_path / "env.yaml")
    monkeypatch.setenv("COORDINARE_CONFIG_PATH", str(cfg))
    result = discover_config_path(None)
    assert result == cfg.resolve()


def test_env_var_config_path_error_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """COORDINARE_CONFIG_PATH pointing to non-existent file → raises ConfigDiscoveryError."""
    monkeypatch.setenv("COORDINARE_CONFIG_PATH", "/nonexistent/path.yaml")
    with pytest.raises(ConfigDiscoveryError, match="not found"):
        discover_config_path(None)


def test_env_var_config_path_error_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COORDINARE_CONFIG_PATH pointing to directory → raises ConfigDiscoveryError."""
    monkeypatch.setenv("COORDINARE_CONFIG_PATH", str(tmp_path))
    with pytest.raises(ConfigDiscoveryError, match="directory"):
        discover_config_path(None)


# ---------------------------------------------------------------------------
# Automatic discovery (cwd and home)
# ---------------------------------------------------------------------------


def test_cwd_config_yaml_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config.yaml in cwd → returned without requiring --config flag."""
    monkeypatch.delenv("COORDINARE_CONFIG_PATH", raising=False)
    cfg = _write_yaml(tmp_path / "config.yaml")
    with patch("coordinare.config_discovery.Path.cwd", return_value=tmp_path):
        result = discover_config_path(None)
    assert result == cfg.resolve()


def test_home_config_found_when_cwd_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cwd config.yaml, but ~/.coordinare/config.yaml exists → returns home path."""
    monkeypatch.delenv("COORDINARE_CONFIG_PATH", raising=False)
    home_dir = tmp_path / "home"
    coordinare_dir = home_dir / ".coordinare"
    coordinare_dir.mkdir(parents=True)
    cfg = _write_yaml(coordinare_dir / "config.yaml")

    # Patch both cwd (no config.yaml) and home
    no_cwd = tmp_path / "empty_cwd"
    no_cwd.mkdir()
    with (
        patch("coordinare.config_discovery.Path.cwd", return_value=no_cwd),
        patch("coordinare.config_discovery.Path.home", return_value=home_dir),
    ):
        result = discover_config_path(None)
    assert result == cfg.resolve()


def test_none_returned_when_no_file_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No config file at any search location → returns None."""
    monkeypatch.delenv("COORDINARE_CONFIG_PATH", raising=False)
    empty_cwd = tmp_path / "cwd"
    empty_cwd.mkdir()
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    with (
        patch("coordinare.config_discovery.Path.cwd", return_value=empty_cwd),
        patch("coordinare.config_discovery.Path.home", return_value=empty_home),
    ):
        result = discover_config_path(None)
    assert result is None
