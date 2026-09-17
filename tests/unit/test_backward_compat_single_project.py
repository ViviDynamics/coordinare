from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import (
    is_multi_symphony_config,
    validate_config,
    wrap_legacy_config,
)


class TestLegacyConfigDetection:
    """Test detection of legacy vs. multi-symphony configs."""

    def test_legacy_config_no_symphonies_key(self) -> None:
        """Config without 'symphonies' key is legacy."""
        raw = {
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 42,
            "github_token": "token",
            "human_reviewers": ["alice"],
        }
        assert not is_multi_symphony_config(raw)

    def test_multi_symphony_config_has_symphonies_key(self) -> None:
        """Config with 'symphonies' key is multi-symphony (new flat format)."""
        raw = {
            "github_org": "acme",
            "github_token": "token",
            "human_reviewers": ["alice"],
            "symphonies": [
                {"name": "api", "github_project_number": 1},
            ],
        }
        assert is_multi_symphony_config(raw)

    def test_global_config_key_alone_is_not_multi_symphony(self) -> None:
        """Config with only 'global_config' key (no symphonies) is NOT multi-symphony.

        The new format is detected by the presence of 'symphonies:' only.
        """
        raw = {
            "global_config": {
                "github_org": "acme",
                "github_token": "token",
                "human_reviewers": ["alice"],
            },
        }
        assert not is_multi_symphony_config(raw)

    def test_mixed_keys_detected_as_multi_symphony(self) -> None:
        """Config with global fields + non-empty symphonies + orchestra is multi-symphony."""
        raw = {
            "github_org": "acme",
            "github_token": "token",
            "human_reviewers": ["alice"],
            "symphonies": [{"name": "main", "github_project_number": 1}],
            "orchestra": {"mode": "shared_pool"},
        }
        assert is_multi_symphony_config(raw)

    def test_empty_symphonies_falls_back_to_legacy(self) -> None:
        """Config with symphonies: [] is treated as legacy single-symphony per spec FR-001."""
        raw = {
            "github_org": "acme",
            "github_token": "token",
            "symphonies": [],
        }
        assert not is_multi_symphony_config(raw)


class TestLegacyConfigWrapping:
    """Test wrap_legacy_config() conversion."""

    def test_wrap_legacy_config_creates_default_symphony(self) -> None:
        """Legacy config wraps as single 'default' symphony."""
        raw = {
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 42,
            "github_token": "token",
            "human_reviewers": ["alice"],
            "poll_interval_seconds": 45,
        }
        wrapped = wrap_legacy_config(raw)

        assert "global_config" in wrapped
        assert "symphonies" in wrapped
        assert "orchestra" in wrapped

        # global_config contains all legacy fields
        assert wrapped["global_config"]["project_name"] == "Demo"
        assert wrapped["global_config"]["github_org"] == "acme"
        assert wrapped["global_config"]["github_project_number"] == 42
        assert wrapped["global_config"]["poll_interval_seconds"] == 45

        # Exactly one symphony named "default"
        assert len(wrapped["symphonies"]) == 1
        assert wrapped["symphonies"][0]["name"] == "default"
        assert wrapped["symphonies"][0]["github_project_number"] == 42

        # Orchestra is empty/default
        assert wrapped["orchestra"]["mode"] == "shared_pool"
        assert wrapped["orchestra"]["performers"] == []

    def test_wrap_legacy_config_preserves_github_project_number(self) -> None:
        """Wrapped symphony gets github_project_number from legacy config."""
        raw = {
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 999,
            "github_token": "token",
            "human_reviewers": ["alice"],
        }
        wrapped = wrap_legacy_config(raw)

        assert wrapped["global_config"]["github_project_number"] == 999
        assert wrapped["symphonies"][0]["github_project_number"] == 999

    def test_wrap_legacy_config_default_github_project_number(self) -> None:
        """Wrapped symphony uses placeholder 1 when github_project_number is absent."""
        raw = {
            "project_name": "Demo",
            "github_org": "acme",
            "github_token": "token",
            "human_reviewers": ["alice"],
        }
        wrapped = wrap_legacy_config(raw)

        # Placeholder 1 ensures SymphonyConfig(ge=1) passes; validate_config() reports
        # the missing field as a ConfigFieldError separately.
        assert wrapped["symphonies"][0]["github_project_number"] == 1

    def test_wrapped_config_is_valid_coordinare_config(self) -> None:
        """Wrapped legacy config parses as valid CoordinareConfiguration."""
        raw = {
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 42,
            "github_token": "token",
            "human_reviewers": ["alice"],
        }
        wrapped = wrap_legacy_config(raw)

        # Should parse without error
        config = CoordinareConfiguration(**wrapped)
        assert len(config.symphonies) == 1
        assert config.symphonies[0].name == "default"
        assert config.global_config.github_org == "acme"


class TestBackwardCompatMerging:
    """Test that legacy config effective_config() matches expected behavior."""

    def test_wrapped_default_symphony_effective_config(self) -> None:
        """Default symphony's effective_config matches global_config."""
        raw = {
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 42,
            "github_token": "token",
            "human_reviewers": ["alice"],
            "poll_interval_seconds": 60,
        }
        wrapped = wrap_legacy_config(raw)
        config = CoordinareConfiguration(**wrapped)

        default_symphony = config.symphonies[0]
        effective = default_symphony.effective_config(config.global_config)

        # Effective config equals global (no overrides)
        assert effective.project_name == config.global_config.project_name
        assert effective.github_org == config.global_config.github_org
        assert effective.github_project_number == config.global_config.github_project_number
        assert effective.poll_interval_seconds == 60


class TestValidateConfigLegacy:
    """Test validate_config() with legacy configs."""

    def test_validate_legacy_config_passes(self, tmp_path: Path) -> None:
        """Legacy config file validates successfully."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
project_name: "Demo"
github_org: "acme"
github_project_number: 42
github_token: "token"
human_reviewers:
  - alice
""",
        )

        with patch(
            "coordinare.config_discovery.discover_config_path",
            return_value=config_file,
        ):
            result = validate_config(config_file)

        assert result.passed
        assert len(result.errors) == 0

    def test_validate_legacy_config_with_env_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Legacy config with env var overrides validates."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
project_name: "Demo"
github_org: "acme"
github_project_number: 42
github_token: "token-from-file"
human_reviewers:
  - alice
""",
        )
        monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "token-from-env")

        with patch(
            "coordinare.config_discovery.discover_config_path",
            return_value=config_file,
        ):
            result = validate_config(config_file)

        assert result.passed
        assert len(result.errors) == 0


class TestMultiSymphonyCoexistence:
    """Test that legacy and multi-symphony configs coexist."""

    def test_legacy_config_in_memory(self) -> None:
        """In-memory legacy dict validates as CoordinareConfiguration."""
        legacy_dict = {
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 42,
            "github_token": "token",
            "human_reviewers": ["alice"],
        }

        # This is what validate_config() does internally
        wrapped = wrap_legacy_config(legacy_dict)
        config = CoordinareConfiguration(**wrapped)

        assert len(config.symphonies) == 1
        assert config.symphonies[0].name == "default"

    def test_multi_symphony_config_in_memory(self) -> None:
        """Multi-symphony dict parses directly."""
        multi_dict = {
            "global_config": {
                "project_name": "Demo",
                "github_org": "acme",
                "github_project_number": 0,
                "github_token": "token",
                "human_reviewers": ["alice"],
            },
            "symphonies": [
                {"name": "api", "github_project_number": 1},
                {"name": "web", "github_project_number": 2},
            ],
        }

        config = CoordinareConfiguration(**multi_dict)
        assert len(config.symphonies) == 2
        assert config.symphonies[0].name == "api"
        assert config.symphonies[1].name == "web"


class TestLegacyAndMultiSymmetry:
    """Test symmetry: wrapped legacy config behaves like single-symphony config."""

    def test_wrapped_legacy_equals_explicit_single_symphony(self) -> None:
        """Wrapped legacy config matches explicitly-created single-symphony config."""
        legacy_dict = {
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 42,
            "github_token": "token",
            "human_reviewers": ["alice"],
        }

        # Legacy path: wrap then create
        wrapped = wrap_legacy_config(legacy_dict)
        legacy_config = CoordinareConfiguration(**wrapped)

        # Explicit path: create from dict directly
        explicit_dict = {
            "global_config": {
                "project_name": "Demo",
                "github_org": "acme",
                "github_project_number": 42,
                "github_token": "token",
                "human_reviewers": ["alice"],
            },
            "symphonies": [
                {"name": "default", "github_project_number": 42},
            ],
        }
        explicit_config = CoordinareConfiguration(**explicit_dict)

        # Both should have same structure
        assert len(legacy_config.symphonies) == len(explicit_config.symphonies)
        assert (
            legacy_config.symphonies[0].name == explicit_config.symphonies[0].name
        )
        assert (
            legacy_config.global_config.github_org
            == explicit_config.global_config.github_org
        )
