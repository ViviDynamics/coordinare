"""Unit tests for coordinare.auth — build_auth() and validate_auth_config() (015)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coordinare.auth import build_auth, validate_auth_config
from coordinare.auth.app import AppAuth
from coordinare.auth.pat import PatAuth

# ---------------------------------------------------------------------------
# build_auth helpers
# ---------------------------------------------------------------------------


def _pat_config(token: str = "ghp_abc") -> MagicMock:
    cfg = MagicMock()
    cfg.github_auth = "pat"
    token_field = MagicMock()
    token_field.get_secret_value.return_value = token
    cfg.github_token = token_field
    return cfg


_FAKE_PEM = b"-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0Z3VS5JJcds3xHn/ygWep4PAtEsHABDKAL4RuVJAKFnN" + b"A" * 100 + b"\n-----END RSA PRIVATE KEY-----\n"


def _app_config(key_path: Path | None = None) -> MagicMock:
    cfg = MagicMock()
    cfg.github_auth = "app"
    cfg.github_app_id = 12345
    cfg.github_private_key_path = key_path or Path("/tmp/key.pem")
    cfg.github_installation_id = 67890
    cfg.github_api_url = "https://api.github.com"
    return cfg


# ---------------------------------------------------------------------------
# build_auth tests
# ---------------------------------------------------------------------------


def test_build_auth_pat_mode_returns_pat_auth() -> None:
    auth = build_auth(_pat_config())
    assert isinstance(auth, PatAuth)


def test_build_auth_pat_mode_token_is_correct() -> None:
    auth = build_auth(_pat_config("ghp_xyz"))
    import asyncio
    assert asyncio.run(auth.get_token()) == "ghp_xyz"


def test_build_auth_app_mode_returns_app_auth(tmp_path: Path) -> None:
    key = tmp_path / "key.pem"
    key.write_bytes(_FAKE_PEM)
    auth = build_auth(_app_config(key))
    assert isinstance(auth, AppAuth)


def test_build_auth_app_mode_fields(tmp_path: Path) -> None:
    key = tmp_path / "key.pem"
    key.write_bytes(_FAKE_PEM)
    auth = build_auth(_app_config(key))
    assert isinstance(auth, AppAuth)
    assert auth._app_id == 12345
    assert auth._installation_id == 67890


def test_build_auth_unknown_mode_raises() -> None:
    cfg = MagicMock()
    cfg.github_auth = "oauth"  # unsupported
    with pytest.raises(ValueError, match="Unsupported github_auth mode"):
        build_auth(cfg)


# ---------------------------------------------------------------------------
# validate_auth_config tests
# ---------------------------------------------------------------------------


def test_validate_auth_config_non_app_mode_returns_immediately() -> None:
    cfg = MagicMock()
    cfg.github_auth = "pat"
    # Should not raise or call sys.exit
    validate_auth_config(cfg)


def test_validate_auth_config_app_no_key_path_returns() -> None:
    """App mode with github_private_key_path=None should return without error."""
    cfg = MagicMock()
    cfg.github_auth = "app"
    cfg.github_private_key_path = None
    validate_auth_config(cfg)  # must not raise


def test_validate_auth_config_missing_key_file_exits(tmp_path: Path) -> None:
    cfg = MagicMock()
    cfg.github_auth = "app"
    cfg.github_private_key_path = tmp_path / "nonexistent.pem"
    with pytest.raises(SystemExit):
        validate_auth_config(cfg)


def test_validate_auth_config_missing_key_file_prints_error(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    cfg = MagicMock()
    cfg.github_auth = "app"
    cfg.github_private_key_path = tmp_path / "nonexistent.pem"
    with pytest.raises(SystemExit):
        validate_auth_config(cfg)
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_validate_auth_config_path_is_directory_exits(tmp_path: Path) -> None:
    """If github_private_key_path points to a directory, should sys.exit(1)."""
    cfg = MagicMock()
    cfg.github_auth = "app"
    cfg.github_private_key_path = tmp_path  # exists but is not a file
    with pytest.raises(SystemExit):
        validate_auth_config(cfg)


def test_validate_auth_config_path_is_directory_prints_error(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    cfg = MagicMock()
    cfg.github_auth = "app"
    cfg.github_private_key_path = tmp_path
    with pytest.raises(SystemExit):
        validate_auth_config(cfg)
    err = capsys.readouterr().err
    assert "not a regular file" in err


def test_validate_auth_config_valid_key_file_passes(tmp_path: Path) -> None:
    key_file = tmp_path / "key.pem"
    key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\n")
    cfg = MagicMock()
    cfg.github_auth = "app"
    cfg.github_private_key_path = key_file
    validate_auth_config(cfg)  # must not raise or exit
