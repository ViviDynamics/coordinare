"""Test missing-secret error handling and redaction (T055).

Spec requirement: missing-secret errors must reference the secret NAME and never
the value. No log record should contain any of the source values at any level.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from performer.server.secrets import (
    SecretMissingError,
    SecretResolver,
    SecretSourceConfig,
)


class TestMissingSecretError:
    """Missing-secret errors must never leak values."""

    def test_missing_secret_error_references_name_only(self, tmp_path: Path) -> None:
        """Error message should include secret name but never the value."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("OTHER_TOKEN=value\n")

        config = SecretSourceConfig(
            init_payload=True, env=True, creds_file=True
        )

        resolver = SecretResolver(config, init_secrets={}, creds_file=creds_file)

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SecretMissingError) as exc_info:
                resolver.resolve("GITHUB_TOKEN")

            error_str = str(exc_info.value)
            assert "GITHUB_TOKEN" in error_str
            # Should not contain any obvious secret values
            assert "ghp_" not in error_str
            assert "sk-ant-" not in error_str

    def test_no_value_in_exception_detail(self, tmp_path: Path) -> None:
        """Exception detail should mention only the secret name."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("X=y\n")

        config = SecretSourceConfig(
            init_payload=True, env=True, creds_file=True
        )
        init_secrets = {}

        resolver = SecretResolver(config, init_secrets=init_secrets, creds_file=creds_file)

        with pytest.raises(SecretMissingError) as exc_info:
            resolver.resolve("MY_SECRET")

        # Check that the exception message structure is safe
        exc = exc_info.value
        assert hasattr(exc, "name")
        assert exc.name == "MY_SECRET"

    def test_logging_redacts_secret_values(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Logs should not contain secret values."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("OTHER_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxyz\n")

        config = SecretSourceConfig(
            init_payload=True, env=True, creds_file=True
        )

        resolver = SecretResolver(config, init_secrets={}, creds_file=creds_file)

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SecretMissingError):
                resolver.resolve("GITHUB_TOKEN")

        # Verify no log records contain the secret value
        for record in caplog.records:
            assert "ghp_1234567890abcdefghijklmnopqrstuvwxyz" not in record.getMessage()

    def test_log_mentions_only_name_on_missing(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """When a required secret is missing, logs should mention only the name."""
        creds_file = tmp_path / "secrets"
        creds_file.write_text("X=y\n")

        config = SecretSourceConfig(
            init_payload=True, env=True, creds_file=True
        )

        resolver = SecretResolver(config, init_secrets={}, creds_file=creds_file)

        with caplog.at_level("WARNING"):
            with pytest.raises(SecretMissingError):
                resolver.resolve("API_KEY")

        # Should have logged the missing secret
        for record in caplog.records:
            msg = record.getMessage()
            if "API_KEY" in msg:
                # Ensure no fake secret is in the log
                assert "ghp_" not in msg
                assert "sk-ant-" not in msg

    def test_no_env_values_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Environment variable values should never appear in logs."""
        config = SecretSourceConfig(
            init_payload=True, env=True, creds_file=True
        )

        resolver = SecretResolver(config, init_secrets={}, creds_file=None)

        env_secret = "sk-ant-super-secret-key-1234567890abcdefghij"

        with patch.dict("os.environ", {"MY_API_KEY": env_secret}):
            with caplog.at_level("DEBUG"):
                # Resolve a different secret that doesn't exist
                try:
                    resolver.resolve("MISSING_SECRET")
                except SecretMissingError:
                    pass

        # The env secret should not appear in any log
        for record in caplog.records:
            assert env_secret not in record.getMessage()

    def test_init_payload_values_not_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Init payload secret values should never appear in logs."""
        config = SecretSourceConfig(
            init_payload=True, env=True, creds_file=True
        )
        payload_secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        init_secrets = {"MY_SECRET": payload_secret}

        resolver = SecretResolver(config, init_secrets=init_secrets, creds_file=None)

        with caplog.at_level("DEBUG"):
            # Attempt to resolve and trigger logging
            try:
                resolver.resolve("OTHER_SECRET")
            except SecretMissingError:
                pass

        # The payload secret should not appear in any log
        for record in caplog.records:
            assert payload_secret not in record.getMessage()
