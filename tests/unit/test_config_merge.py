from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.config import (
    CoordinareConfiguration,
    OrchestraConfig,
    ProjectConfiguration,
    SymphonyConfig,
)


def _base_project_config() -> ProjectConfiguration:
    """Minimal valid ProjectConfiguration for testing."""
    return ProjectConfiguration(
        project_name="BaseProject",
        github_org="acme",
        github_project_number=1,
        github_token="token",
        human_reviewers=["alice"],
    )


class TestSymphonyConfigMerging:
    """Test SymphonyConfig.effective_config() merging logic."""

    def test_effective_config_no_overrides(self) -> None:
        """Without overrides, effective config equals global config."""
        global_cfg = _base_project_config()
        symphony = SymphonyConfig(
            name="project-a",
            github_project_number=100,
        )
        effective = symphony.effective_config(global_cfg)

        # All fields match global except project_number
        assert effective.project_name == global_cfg.project_name
        assert effective.github_org == global_cfg.github_org
        assert effective.github_project_number == 100  # Symphony overrides project number
        assert effective.poll_interval_seconds == global_cfg.poll_interval_seconds

    def test_effective_config_with_scalar_override(self) -> None:
        """Overrides merge with global config."""
        global_cfg = _base_project_config()
        symphony = SymphonyConfig(
            name="project-b",
            github_project_number=101,
            overrides={"poll_interval_seconds": 60},
        )
        effective = symphony.effective_config(global_cfg)

        assert effective.github_project_number == 101
        assert effective.poll_interval_seconds == 60
        assert effective.project_name == global_cfg.project_name

    def test_effective_config_overrides_all_fields(self) -> None:
        """Overrides can replace any ProjectConfiguration field."""
        global_cfg = _base_project_config()
        symphony = SymphonyConfig(
            name="project-c",
            github_project_number=102,
            overrides={
                "max_concurrent_cards": 10,
                "max_feedback_cycles": 8,
                "poll_interval_seconds": 120,
            },
        )
        effective = symphony.effective_config(global_cfg)

        assert effective.github_project_number == 102
        assert effective.max_concurrent_cards == 10
        assert effective.max_feedback_cycles == 8
        assert effective.poll_interval_seconds == 120

    def test_github_project_number_in_overrides_raises(self) -> None:
        """github_project_number is a reserved per-symphony field; overrides must not include it."""
        global_cfg = _base_project_config()
        symphony = SymphonyConfig(
            name="project-d",
            github_project_number=103,
            overrides={"github_project_number": 999},
        )
        with pytest.raises(ValueError, match="reserved per-symphony fields"):
            symphony.effective_config(global_cfg)

    def test_empty_overrides_dict(self) -> None:
        """Empty overrides dict has no effect."""
        global_cfg = _base_project_config()
        symphony = SymphonyConfig(
            name="project-e",
            github_project_number=104,
            overrides={},
        )
        effective = symphony.effective_config(global_cfg)

        assert effective.github_project_number == 104
        assert effective.project_name == global_cfg.project_name

    def test_none_overrides(self) -> None:
        """None overrides (default) is safe."""
        global_cfg = _base_project_config()
        symphony = SymphonyConfig(
            name="project-f",
            github_project_number=105,
            overrides=None,
        )
        effective = symphony.effective_config(global_cfg)

        assert effective.github_project_number == 105
        assert effective.project_name == global_cfg.project_name


class TestSymphonyConfigNameValidation:
    """Test SymphonyConfig.name validation regex."""

    def test_valid_names(self) -> None:
        """Valid symphony names pass validation."""
        valid_names = [
            "default",
            "project-a",
            "api-backend",
            "v1-main",
            "db2",
        ]
        for name in valid_names:
            symphony = SymphonyConfig(
                name=name,
                github_project_number=1,
            )
            assert symphony.name == name

    def test_invalid_names(self) -> None:
        """Invalid names are rejected."""
        invalid_names = [
            "-leading-dash",  # Starts with dash
            "trailing-dash-",  # Ends with dash
            "with space",  # Contains space
            "with_underscore",  # Contains underscore
            "",  # Empty
            "CamelCase",  # Uppercase
            "-",  # Just dash
        ]
        for name in invalid_names:
            with pytest.raises(ValidationError) as exc_info:
                SymphonyConfig(
                    name=name,
                    github_project_number=1,
                )
            # Verify the error mentions name validation
            assert "name" in str(exc_info.value).lower()


class TestCoordinareConfigurationValidation:
    """Test CoordinareConfiguration validators."""

    def test_unique_symphony_names(self) -> None:
        """Symphony names must be unique."""
        global_cfg = _base_project_config()
        with pytest.raises(ValidationError) as exc_info:
            CoordinareConfiguration(
                global_config=global_cfg,
                symphonies=[
                    SymphonyConfig(name="project-a", github_project_number=1),
                    SymphonyConfig(name="project-a", github_project_number=2),
                ],
            )
        assert "unique" in str(exc_info.value).lower()

    def test_unique_project_numbers(self) -> None:
        """Project numbers must be unique."""
        global_cfg = _base_project_config()
        with pytest.raises(ValidationError) as exc_info:
            CoordinareConfiguration(
                global_config=global_cfg,
                symphonies=[
                    SymphonyConfig(name="project-a", github_project_number=100),
                    SymphonyConfig(name="project-b", github_project_number=100),
                ],
            )
        assert "unique" in str(exc_info.value).lower()

    def test_valid_multi_symphony_config(self) -> None:
        """Valid CoordinareConfiguration with 3 symphonies."""
        global_cfg = _base_project_config()
        config = CoordinareConfiguration(
            global_config=global_cfg,
            symphonies=[
                SymphonyConfig(name="api", github_project_number=1),
                SymphonyConfig(name="web", github_project_number=2),
                SymphonyConfig(name="mobile", github_project_number=3),
            ],
            orchestra=OrchestraConfig(mode="shared_pool"),
        )
        assert len(config.symphonies) == 3
        assert config.symphonies[0].name == "api"
        assert config.symphonies[1].name == "web"
        assert config.symphonies[2].name == "mobile"


class TestConfigMergingEdgeCases:
    """Test edge cases in config merging."""

    def test_override_partial_fields(self) -> None:
        """Partial overrides don't affect unspecified fields."""
        global_cfg = ProjectConfiguration(
            project_name="Global",
            github_org="acme",
            github_project_number=1,
            github_token="token",
            human_reviewers=["alice", "bob"],
            poll_interval_seconds=30,
            max_concurrent_cards=5,
        )
        symphony = SymphonyConfig(
            name="symphony-1",
            github_project_number=100,
            overrides={"poll_interval_seconds": 60},
        )
        effective = symphony.effective_config(global_cfg)

        # Overridden fields
        assert effective.poll_interval_seconds == 60
        assert effective.github_project_number == 100

        # Unchanged fields
        assert effective.project_name == "Global"
        assert effective.human_reviewers == ["alice", "bob"]
        assert effective.max_concurrent_cards == 5

    def test_multiple_symphonies_independent(self) -> None:
        """Different symphonies can have different effective configs."""
        global_cfg = _base_project_config()

        sym1 = SymphonyConfig(
            name="fast",
            github_project_number=100,
            overrides={"poll_interval_seconds": 60},
        )
        sym2 = SymphonyConfig(
            name="slow",
            github_project_number=101,
            overrides={"poll_interval_seconds": 300},
        )

        eff1 = sym1.effective_config(global_cfg)
        eff2 = sym2.effective_config(global_cfg)

        assert eff1.poll_interval_seconds == 60
        assert eff2.poll_interval_seconds == 300
        assert eff1.github_project_number == 100
        assert eff2.github_project_number == 101

    def test_personas_in_symphony_config(self) -> None:
        """Symphony can store personas for per-symphony role assignment."""
        symphony = SymphonyConfig(
            name="test",
            github_project_number=100,
            personas={
                "implementing": {"model": "claude-opus-4"},
                "reviewing": {"model": "claude-sonnet-4"},
            },
        )
        # personas field is stored but not merged into effective_config
        # (merging is done at daemon level)
        assert symphony.personas is not None
        assert symphony.personas["implementing"]["model"] == "claude-opus-4"
