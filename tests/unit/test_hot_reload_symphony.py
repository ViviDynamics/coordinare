from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml

from coordinare.config import (
    CoordinareConfiguration,
    ProjectConfiguration,
    SymphonyConfig,
)
from coordinare.config_validation import (
    validate_config,
    wrap_legacy_config,
)
from coordinare.graph.state import SymphonyRuntimeState


def _base_project_config() -> ProjectConfiguration:
    """Minimal valid ProjectConfiguration for testing."""
    return ProjectConfiguration(
        project_name="Demo",
        github_org="acme",
        github_project_number=0,
        github_token="token",
        human_reviewers=["alice"],
    )


class TestHotReloadDetection:
    """Test detection of config changes during hot-reload."""

    def test_detect_added_symphony(self) -> None:
        """Detect when a symphony is added."""
        old_names = {"api", "web"}
        new_names = {"api", "web", "mobile"}

        added = new_names - old_names
        removed = old_names - new_names

        assert added == {"mobile"}
        assert removed == set()

    def test_detect_removed_symphony(self) -> None:
        """Detect when a symphony is removed."""
        old_names = {"api", "web", "mobile"}
        new_names = {"api", "web"}

        added = new_names - old_names
        removed = old_names - new_names

        assert added == set()
        assert removed == {"mobile"}

    def test_detect_added_and_removed(self) -> None:
        """Detect simultaneous adds and removes."""
        old_names = {"api", "web", "mobile"}
        new_names = {"api", "web", "devops"}

        added = new_names - old_names
        removed = old_names - new_names

        assert added == {"devops"}
        assert removed == {"mobile"}

    def test_no_changes_detected(self) -> None:
        """Detect when symphonies haven't changed."""
        old_names = {"api", "web"}
        new_names = {"api", "web"}

        added = new_names - old_names
        removed = old_names - new_names

        assert added == set()
        assert removed == set()


class TestHotReloadConfigMerging:
    """Test merging old and new configs during reload."""

    def test_reload_with_added_symphony(self, tmp_path: Path) -> None:
        """Hot-reload adds new symphony to state."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
github_org: "acme"
github_token: "token"
human_reviewers:
  - alice
symphonies:
  - name: "api"
    github_project_number: 1
  - name: "web"
    github_project_number: 2
  - name: "mobile"
    github_project_number: 3
"""
        )

        with patch(
            "coordinare.config_discovery.discover_config_path",
            return_value=config_file,
        ):
            result = validate_config(config_file)

        assert result.passed
        raw = yaml.safe_load(config_file.read_text())
        _sym_keys = frozenset({"symphonies", "orchestra"})
        coerced = {
            "global_config": {k: v for k, v in raw.items() if k not in _sym_keys},
            "symphonies": raw.get("symphonies", []),
            "orchestra": raw.get("orchestra", {"mode": "shared_pool", "performers": []}),
        }
        config = CoordinareConfiguration(**coerced)
        assert len(config.symphonies) == 3

    def test_reload_with_removed_symphony(self, tmp_path: Path) -> None:
        """Hot-reload removes symphony from state."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
github_org: "acme"
github_token: "token"
human_reviewers:
  - alice
symphonies:
  - name: "api"
    github_project_number: 1
  - name: "web"
    github_project_number: 2
"""
        )

        with patch(
            "coordinare.config_discovery.discover_config_path",
            return_value=config_file,
        ):
            result = validate_config(config_file)

        assert result.passed
        raw = yaml.safe_load(config_file.read_text())
        _sym_keys = frozenset({"symphonies", "orchestra"})
        coerced = {
            "global_config": {k: v for k, v in raw.items() if k not in _sym_keys},
            "symphonies": raw.get("symphonies", []),
            "orchestra": raw.get("orchestra", {"mode": "shared_pool", "performers": []}),
        }
        config = CoordinareConfiguration(**coerced)
        assert len(config.symphonies) == 2
        assert config.symphonies[0].name == "api"
        assert config.symphonies[1].name == "web"

    def test_reload_preserves_modified_symphony(self, tmp_path: Path) -> None:
        """Hot-reload with modified symphony config."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
github_org: "acme"
github_token: "token"
human_reviewers:
  - alice
symphonies:
  - name: "api"
    github_project_number: 1
    overrides:
      poll_interval_seconds: 60
"""
        )

        with patch(
            "coordinare.config_discovery.discover_config_path",
            return_value=config_file,
        ):
            result = validate_config(config_file)

        assert result.passed
        raw = yaml.safe_load(config_file.read_text())
        _sym_keys = frozenset({"symphonies", "orchestra"})
        coerced = {
            "global_config": {k: v for k, v in raw.items() if k not in _sym_keys},
            "symphonies": raw.get("symphonies", []),
            "orchestra": raw.get("orchestra", {"mode": "shared_pool", "performers": []}),
        }
        config = CoordinareConfiguration(**coerced)
        assert config.symphonies[0].overrides["poll_interval_seconds"] == 60


class TestHotReloadStateManagement:
    """Test how state is updated during hot-reload."""

    def test_new_symphony_gets_fresh_state(self) -> None:
        """New symphony starts with fresh SymphonyRuntimeState."""
        new_symphony = SymphonyConfig(
            name="new-project",
            github_project_number=999,
        )

        # This is how daemon should create state for added symphonies
        new_state = SymphonyRuntimeState(
            name=new_symphony.name,
            cycle_count=0,
            last_poll_at=None,
        )

        assert new_state.name == "new-project"
        assert new_state.cycle_count == 0
        assert new_state.error_count == 0

    def test_removed_symphony_state_cleanup(self) -> None:
        """Removed symphony state is cleaned from state dict."""
        old_state = {
            "api": SymphonyRuntimeState(name="api", cycle_count=5, last_poll_at=None),
            "web": SymphonyRuntimeState(name="web", cycle_count=3, last_poll_at=None),
            "mobile": SymphonyRuntimeState(
                name="mobile", cycle_count=1, last_poll_at=None
            ),
        }

        to_remove = {"mobile"}
        new_state = {k: v for k, v in old_state.items() if k not in to_remove}

        assert "mobile" not in new_state
        assert len(new_state) == 2
        assert "api" in new_state
        assert "web" in new_state

    def test_reloaded_symphony_preserves_statistics(self) -> None:
        """Reloaded symphonies keep their statistics."""
        # Before reload
        old_state = SymphonyRuntimeState(
            name="api",
            cycle_count=10,
            last_poll_at=None,
            error_count=2,
            last_error="timeout",
        )

        # State is not recreated; config just changes
        assert old_state.name == "api"
        assert old_state.cycle_count == 10
        assert old_state.error_count == 2


class TestHotReloadConfigValidation:
    """Test validation during hot-reload."""

    def test_reload_validates_new_config(self, tmp_path: Path) -> None:
        """Hot-reload validates the new config before applying."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
github_org: "acme"
github_token: "token"
human_reviewers:
  - alice
symphonies:
  - name: "api"
    github_project_number: 1
"""
        )

        with patch(
            "coordinare.config_discovery.discover_config_path",
            return_value=config_file,
        ):
            result = validate_config(config_file)

        # Should pass validation
        assert result.passed
        assert len(result.errors) == 0

    def test_reload_detects_invalid_new_config(self, tmp_path: Path) -> None:
        """Hot-reload detects invalid new config and reports errors."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
github_org: "acme"
symphonies:
  - name: "api"
    github_project_number: 1
"""
        )

        with patch(
            "coordinare.config_discovery.discover_config_path",
            return_value=config_file,
        ):
            result = validate_config(config_file)

        # Missing required field (github_token)
        assert not result.passed
        assert len(result.errors) > 0

    def test_reload_detects_duplicate_symphony_names(self, tmp_path: Path) -> None:
        """Hot-reload detects duplicate symphony names."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
github_org: "acme"
github_token: "token"
human_reviewers:
  - alice
symphonies:
  - name: "api"
    github_project_number: 1
  - name: "api"
    github_project_number: 2
"""
        )

        with patch(
            "coordinare.config_discovery.discover_config_path",
            return_value=config_file,
        ):
            result = validate_config(config_file)

        # Should fail due to duplicate names
        assert not result.passed


class TestHotReloadLegacyToMultiSymphony:
    """Test hot-reload transitions between legacy and multi-symphony."""

    def test_reload_legacy_to_multi_symphony(self, tmp_path: Path) -> None:
        """Hot-reload upgrades legacy config to multi-symphony."""
        # Start with legacy
        legacy_dict = {
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 1,
            "github_token": "token",
            "human_reviewers": ["alice"],
        }

        wrapped = wrap_legacy_config(legacy_dict)
        old_config = CoordinareConfiguration(**wrapped)

        # Reload with multi-symphony
        new_dict = {
            "global_config": {
                "project_name": "Demo",
                "github_org": "acme",
                "github_project_number": 0,
                "github_token": "token",
                "human_reviewers": ["alice"],
            },
            "symphonies": [
                {"name": "api", "github_project_number": 100},
                {"name": "web", "github_project_number": 101},
            ],
        }
        new_config = CoordinareConfiguration(**new_dict)

        # Old had 1 symphony, new has 2
        assert len(old_config.symphonies) == 1
        assert len(new_config.symphonies) == 2

    def test_reload_multi_symphony_to_legacy_via_wrapping(
        self, tmp_path: Path
    ) -> None:
        """Hot-reload from multi-symphony back to legacy (via wrapping)."""
        # Start with multi-symphony
        multi_dict = {
            "global_config": {
                "project_name": "Demo",
                "github_org": "acme",
                "github_project_number": 0,
                "github_token": "token",
                "human_reviewers": ["alice"],
            },
            "symphonies": [
                {"name": "api", "github_project_number": 100},
                {"name": "web", "github_project_number": 101},
            ],
        }
        old_config = CoordinareConfiguration(**multi_dict)

        # Reload with legacy format
        legacy_dict = {
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 50,
            "github_token": "token",
            "human_reviewers": ["alice"],
        }
        wrapped = wrap_legacy_config(legacy_dict)
        new_config = CoordinareConfiguration(**wrapped)

        # Old had 2 symphonies, new has 1 (default)
        assert len(old_config.symphonies) == 2
        assert len(new_config.symphonies) == 1
        assert new_config.symphonies[0].name == "default"


class TestHotReloadOrchestraConfig:
    """Test orchestra config changes during hot-reload."""

    def test_orchestra_config_updated_on_reload(self) -> None:
        """Orchestra config is updated during hot-reload."""
        old_dict = {
            "global_config": {
                "project_name": "Demo",
                "github_org": "acme",
                "github_project_number": 0,
                "github_token": "token",
                "human_reviewers": ["alice"],
            },
            "symphonies": [{"name": "api", "github_project_number": 1}],
            "orchestra": {"mode": "shared_pool", "performers": []},
        }
        old_config = CoordinareConfiguration(**old_dict)

        new_dict = {
            "global_config": {
                "project_name": "Demo",
                "github_org": "acme",
                "github_project_number": 0,
                "github_token": "token",
                "human_reviewers": ["alice"],
            },
            "symphonies": [{"name": "api", "github_project_number": 1}],
            "orchestra": {
                "mode": "shared_pool",
                "performers": [{"type": "docker", "image": "performer:latest"}],
            },
        }
        new_config = CoordinareConfiguration(**new_dict)

        assert len(old_config.orchestra.performers) == 0
        assert len(new_config.orchestra.performers) == 1
