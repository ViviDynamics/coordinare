"""Test secret resolver precedence: init_payload > env > creds_file (T053).

Spec requirement: deterministic precedence for secret sources. When the same
secret is provided through multiple sources, the resolver must honor the
documented priority order.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from performer.server.secrets import (
    SecretMissingError,
    SecretResolver,
    SecretSourceConfig,
)


class TestPrecedenceOrder:
    """Precedence: init_payload > env > creds_file."""

    def test_init_payload_wins_over_all_sources(self, tmp_path: Path) -> None:
        """When init_payload provides a secret, it takes precedence."""
        # Create creds file
        creds_file = tmp_path / "secrets"
        creds_file.write_text("GITHUB_TOKEN=file_value\n")

        config = SecretSourceConfig(
            init_payload=True, env=True, creds_file=True
        )
        init_secrets = {"GITHUB_TOKEN": "payload_value"}

        resolver = SecretResolver(config, init_secrets=init_secrets, creds_file=creds_file)

        # init_payload should win
        with patch.dict("os.environ", {"GITHUB_TOKEN": "env_value"}):
            result = resolver.resolve("GITHUB_TOKEN")
            assert result == "payload_value"

    def test_env_wins_over_creds_file(self, tmp_path: Path) -> None:
        """When init_payload is absent, env wins over creds_file."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("GITHUB_TOKEN=file_value\n")

        config = SecretSourceConfig(
            init_payload=True, env=True, creds_file=True
        )

        resolver = SecretResolver(config, init_secrets={}, creds_file=creds_file)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "env_value"}):
            result = resolver.resolve("GITHUB_TOKEN")
            assert result == "env_value"

    def test_creds_file_used_when_init_and_env_absent(self, tmp_path: Path) -> None:
        """When init_payload and env are absent, use creds_file."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("GITHUB_TOKEN=file_value\n")

        config = SecretSourceConfig(
            init_payload=True, env=True, creds_file=True
        )

        resolver = SecretResolver(config, init_secrets={}, creds_file=creds_file)

        # Ensure env is not set
        with patch.dict("os.environ", {}, clear=True):
            result = resolver.resolve("GITHUB_TOKEN")
            assert result == "file_value"

    def test_missing_from_all_sources_raises(self, tmp_path: Path) -> None:
        """When secret is not in any source, raise SecretMissingError."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("OTHER_SECRET=value\n")

        config = SecretSourceConfig(
            init_payload=True, env=True, creds_file=True
        )

        resolver = SecretResolver(config, init_secrets={}, creds_file=creds_file)

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SecretMissingError) as exc_info:
                resolver.resolve("GITHUB_TOKEN")
            assert "GITHUB_TOKEN" in str(exc_info.value)

    def test_empty_string_from_init_payload_is_used(self, tmp_path: Path) -> None:
        """Empty string from init_payload is still a valid value (wins precedence)."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("GITHUB_TOKEN=file_value\n")

        config = SecretSourceConfig(
            init_payload=True, env=True, creds_file=True
        )
        # Empty string explicitly provided
        init_secrets = {"GITHUB_TOKEN": ""}

        resolver = SecretResolver(config, init_secrets=init_secrets, creds_file=creds_file)

        result = resolver.resolve("GITHUB_TOKEN")
        # Empty string is falsy but still "present" in init_payload
        assert result == ""

    def test_init_payload_none_falls_through(self, tmp_path: Path) -> None:
        """When init_payload secret is None, try next source."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("GITHUB_TOKEN=file_value\n")

        config = SecretSourceConfig(
            init_payload=True, env=True, creds_file=True
        )
        init_secrets = {"GITHUB_TOKEN": None}

        resolver = SecretResolver(
            config, init_secrets=init_secrets, creds_file=creds_file
        )

        with patch.dict("os.environ", {"GITHUB_TOKEN": "env_value"}):
            result = resolver.resolve("GITHUB_TOKEN")
            assert result == "env_value"
