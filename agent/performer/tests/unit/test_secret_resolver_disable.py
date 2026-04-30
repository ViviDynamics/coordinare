"""Test per-source disable for secret resolver (T054).

Spec requirement: operators can disable any of the three sources per performer,
so a deployment can opt out of (for example) over-the-wire credentials.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from performer.server.secrets import (
    SecretMissingError,
    SecretResolver,
    SecretSourceConfig,
)


class TestPerSourceDisable:
    """When a source is disabled, it should be skipped entirely."""

    def test_disabled_init_payload_skipped(self, tmp_path: Path) -> None:
        """When init_payload is disabled, skip it even if value is present."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("GITHUB_TOKEN=file_value\n")

        config = SecretSourceConfig(
            init_payload=False, env=True, creds_file=True
        )
        init_secrets = {"GITHUB_TOKEN": "payload_value"}

        resolver = SecretResolver(config, init_secrets=init_secrets, creds_file=creds_file)

        # Should skip init_payload and use env
        with patch.dict("os.environ", {"GITHUB_TOKEN": "env_value"}):
            result = resolver.resolve("GITHUB_TOKEN")
            assert result == "env_value"

    def test_disabled_init_payload_falls_through_to_creds(self, tmp_path: Path) -> None:
        """When init_payload is disabled, fall through to creds_file."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("GITHUB_TOKEN=file_value\n")

        config = SecretSourceConfig(
            init_payload=False, env=True, creds_file=True
        )
        init_secrets = {"GITHUB_TOKEN": "payload_value"}

        resolver = SecretResolver(config, init_secrets=init_secrets, creds_file=creds_file)

        # No env, should use file
        with patch.dict("os.environ", {}, clear=True):
            result = resolver.resolve("GITHUB_TOKEN")
            assert result == "file_value"

    def test_disabled_env_skipped(self, tmp_path: Path) -> None:
        """When env is disabled, skip it even if value is present."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("GITHUB_TOKEN=file_value\n")

        config = SecretSourceConfig(
            init_payload=False, env=False, creds_file=True
        )

        resolver = SecretResolver(config, init_secrets={}, creds_file=creds_file)

        # env should be skipped
        with patch.dict("os.environ", {"GITHUB_TOKEN": "env_value"}):
            result = resolver.resolve("GITHUB_TOKEN")
            assert result == "file_value"

    def test_disabled_creds_file_skipped(self, tmp_path: Path) -> None:
        """When creds_file is disabled, skip it even if file exists."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("GITHUB_TOKEN=file_value\n")

        config = SecretSourceConfig(
            init_payload=False, env=True, creds_file=False
        )

        resolver = SecretResolver(config, init_secrets={}, creds_file=creds_file)

        # creds_file should be skipped
        with patch.dict("os.environ", {"GITHUB_TOKEN": "env_value"}):
            result = resolver.resolve("GITHUB_TOKEN")
            assert result == "env_value"

    def test_all_sources_disabled_raises(self, tmp_path: Path) -> None:
        """When all sources are disabled, raise error immediately."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("GITHUB_TOKEN=file_value\n")

        config = SecretSourceConfig(
            init_payload=False, env=False, creds_file=False
        )
        init_secrets = {"GITHUB_TOKEN": "payload_value"}

        resolver = SecretResolver(config, init_secrets=init_secrets, creds_file=creds_file)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "env_value"}):
            with pytest.raises(SecretMissingError):
                resolver.resolve("GITHUB_TOKEN")

    def test_disabled_sources_never_reach_output(self, tmp_path: Path) -> None:
        """Values from disabled sources should never appear in resolved output."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("GITHUB_TOKEN=file_value\n")

        config = SecretSourceConfig(
            init_payload=False, env=False, creds_file=True
        )
        init_secrets = {"GITHUB_TOKEN": "payload_value"}

        resolver = SecretResolver(config, init_secrets=init_secrets, creds_file=creds_file)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "env_value"}):
            result = resolver.resolve("GITHUB_TOKEN")
            # Only creds_file value should be present
            assert result == "file_value"
            assert result != "payload_value"
            assert result != "env_value"
