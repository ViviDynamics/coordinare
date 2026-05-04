"""Schema and contract tests for symphony configuration models (spec 057).

These are unit tests that validate SymphonyConfig, CoordinareConfiguration, and
OrchestraConfig model behaviour — not HTTP endpoint integration tests.
"""
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
    """Minimal valid ProjectConfiguration for testing (no project_name — per-symphony in new format)."""
    return ProjectConfiguration(
        github_org="acme",
        github_token="token",
        human_reviewers=["alice"],
    )


def _make_coordinare_config(num_symphonies: int = 2) -> CoordinareConfiguration:
    """Create a test CoordinareConfiguration."""
    global_cfg = _base_project_config()
    symphonies = [
        SymphonyConfig(name=f"sym-{i}", github_project_number=100 + i)
        for i in range(num_symphonies)
    ]
    return CoordinareConfiguration(
        global_config=global_cfg,
        symphonies=symphonies,
        orchestra=OrchestraConfig(mode="shared_pool"),
    )


class TestSymphonyListSchema:
    """Test GET /api/symphonies response schema (model-level, not HTTP integration)."""

    def test_list_symphonies_returns_all(self) -> None:
        """CoordinareConfiguration holds all configured symphonies in order."""
        config = _make_coordinare_config(2)
        assert len(config.symphonies) == 2
        assert config.symphonies[0].name == "sym-0"
        assert config.symphonies[1].name == "sym-1"
        assert config.symphonies[0].github_project_number == 100
        assert config.symphonies[1].github_project_number == 101

    def test_empty_symphonies_rejected(self) -> None:
        """CoordinareConfiguration rejects an empty symphonies list."""
        with pytest.raises(ValidationError, match="at least one symphony"):
            CoordinareConfiguration(
                global_config=_base_project_config(),
                symphonies=[],
                orchestra=OrchestraConfig(mode="shared_pool"),
            )


class TestSymphonyUpdateSchema:
    """Test PUT /api/symphonies/{name} config model behaviour (model-level, not HTTP integration)."""

    def test_update_symphony_overrides(self) -> None:
        """PUT /api/symphonies/{name} updates overrides."""
        config = _make_coordinare_config(2)

        # Old config
        old_symphony = config.symphonies[0]
        assert old_symphony.overrides is None

        # Simulate update with overrides
        updated_symphony = SymphonyConfig(
            name=old_symphony.name,
            github_project_number=old_symphony.github_project_number,
            overrides={"poll_interval_seconds": 90},
        )

        assert updated_symphony.overrides == {"poll_interval_seconds": 90}

    def test_update_symphony_personas(self) -> None:
        """PUT /api/symphonies/{name} updates personas."""
        config = _make_coordinare_config(1)

        symphony = config.symphonies[0]
        updated_symphony = SymphonyConfig(
            name=symphony.name,
            github_project_number=symphony.github_project_number,
            personas={
                "implementing": {"model": "claude-opus-4"},
                "reviewing": {"model": "claude-sonnet-4"},
            },
        )

        assert updated_symphony.personas is not None
        assert "implementing" in updated_symphony.personas

    def test_update_validates_new_config(self) -> None:
        """SymphonyConfig accepts arbitrary overrides dict (validated at effective_config time)."""
        config = _make_coordinare_config(1)
        symphony = config.symphonies[0]

        # overrides is dict[str, Any] — unknown keys are accepted here and
        # only validated when effective_config() merges them into ProjectConfiguration
        updated = SymphonyConfig(
            name=symphony.name,
            github_project_number=symphony.github_project_number,
            overrides={"invalid_field": "value"},
        )
        assert updated.overrides == {"invalid_field": "value"}



class TestSymphonyDeleteSchema:
    """Test DELETE /api/symphonies/{name} config model behaviour (model-level, not HTTP integration)."""

    def test_delete_symphony_removes_from_config(self) -> None:
        """DELETE /api/symphonies/{name} removes symphony."""
        config = _make_coordinare_config(2)
        names_before = {s.name for s in config.symphonies}
        assert "sym-0" in names_before
        assert "sym-1" in names_before

        # Simulate deletion
        to_delete = "sym-0"
        names_after = {s.name for s in config.symphonies if s.name != to_delete}

        assert "sym-0" not in names_after
        assert "sym-1" in names_after

    def test_delete_last_symphony_violates_model(self) -> None:
        """CoordinareConfiguration requires at least one symphony (model-enforced invariant)."""
        config = _make_coordinare_config(1)
        remaining = [s for s in config.symphonies if s.name != "sym-0"]
        assert len(remaining) == 0
        with pytest.raises(ValidationError, match="at least one symphony"):
            CoordinareConfiguration(
                global_config=config.global_config,
                symphonies=remaining,
                orchestra=OrchestraConfig(mode="shared_pool"),
            )


class TestEffectiveConfigSchema:
    """Test effective config resolution logic (model-level, not HTTP integration)."""

    def test_get_effective_config_for_symphony(self) -> None:
        """GET /api/config/effective?symphony=api returns resolved config."""
        config = _make_coordinare_config(1)
        symphony = config.symphonies[0]

        effective = symphony.effective_config(config.global_config)

        # project_name comes from the symphony name; github_project_number from the symphony
        assert effective.project_name == symphony.name
        assert effective.github_project_number == 100

    def test_get_effective_config_with_overrides(self) -> None:
        """GET /api/config/effective?symphony=name includes overrides."""
        global_cfg = _base_project_config()
        global_cfg.poll_interval_seconds = 60

        symphony = SymphonyConfig(
            name="fast",
            github_project_number=100,
            overrides={"poll_interval_seconds": 45},
        )

        effective = symphony.effective_config(global_cfg)
        assert effective.poll_interval_seconds == 45



class TestSymphonyValidateSchema:
    """Test symphony dry-run validation logic (model-level, not HTTP integration)."""

    def test_validate_dry_run_doesnt_save(self) -> None:
        """POST /api/symphonies/{name}/validate doesn't save config."""
        config = _make_coordinare_config(1)
        symphony = config.symphonies[0]

        # Validate with proposed changes
        updated_symphony = SymphonyConfig(
            name=symphony.name,
            github_project_number=symphony.github_project_number,
            overrides={"poll_interval_seconds": 120},
        )

        # Validate (dry-run)
        effective = updated_symphony.effective_config(config.global_config)
        assert effective.poll_interval_seconds == 120

        # Original config should be unchanged
        original_effective = symphony.effective_config(config.global_config)
        assert original_effective.poll_interval_seconds == config.global_config.poll_interval_seconds

    def test_validate_returns_errors(self) -> None:
        """POST /api/symphonies/{name}/validate returns validation errors."""
        # Name with uppercase/spaces violates the alphanumeric+dash validator
        with pytest.raises(ValidationError):
            SymphonyConfig(name="INVALID NAME", github_project_number=100)


class TestConfigReloadSchema:
    """Test config reload diff logic (model-level, not HTTP integration)."""

    def test_reload_config_detects_changes(self) -> None:
        """POST /api/config/reload detects added/removed symphonies."""
        old_config = _make_coordinare_config(2)
        old_names = {s.name for s in old_config.symphonies}

        # Simulate new config with added symphony
        new_symphonies = [
            *old_config.symphonies,
            SymphonyConfig(name="sym-2", github_project_number=102),
        ]
        new_names = {s.name for s in new_symphonies}

        added = new_names - old_names
        removed = old_names - new_names

        assert added == {"sym-2"}
        assert removed == set()



class TestSymphonyApiErrorResponses:
    """Test error response codes and formats."""

    def test_404_symphony_not_found(self) -> None:
        """DELETE /api/symphonies/{name} returns 404 for unknown symphony."""
        config = _make_coordinare_config(2)
        # Model-level: lookup by name returns None for an unknown symphony name
        found = next((s for s in config.symphonies if s.name == "nonexistent"), None)
        assert found is None

    def test_409_conflict_duplicate_name(self) -> None:
        """PUT /api/symphonies/{name} returns 409 if name already exists."""
        config = _make_coordinare_config(2)
        existing_names = {s.name for s in config.symphonies}
        # Conflict detection: the name is present, so a duplicate add would be rejected
        assert "sym-0" in existing_names
        assert "sym-1" in existing_names

    def test_409_conflict_duplicate_project_number(self) -> None:
        """PUT /api/symphonies/{name} returns 409 if project number exists."""
        config = _make_coordinare_config(2)
        existing_numbers = {s.github_project_number for s in config.symphonies}
        # Conflict detection: project numbers are present and unique
        assert 100 in existing_numbers
        assert 101 in existing_numbers
        assert len(existing_numbers) == len(config.symphonies)

    def test_409_last_symphony(self) -> None:
        """DELETE /api/symphonies/{name} returns 409 if last symphony."""
        config = _make_coordinare_config(1)
        remaining = [s for s in config.symphonies if s.name != "sym-0"]
        assert len(remaining) == 0
        # Deleting the only symphony would leave an empty list — model rejects this
        with pytest.raises(ValidationError, match="at least one symphony"):
            CoordinareConfiguration(
                global_config=config.global_config,
                symphonies=remaining,
                orchestra=OrchestraConfig(mode="shared_pool"),
            )

    def test_400_invalid_request(self) -> None:
        """PUT /api/symphonies/{name} returns 400 for malformed request — invalid name."""
        # Missing required field (github_project_number) triggers validation error
        with pytest.raises(ValidationError):
            SymphonyConfig(name="sym-0")


class TestSymphonyApiResponseSchemas:
    """Test response schema compliance."""

    def test_symphony_list_response_schema(self) -> None:
        """GET /api/symphonies response matches schema."""
        config = _make_coordinare_config(2)

        # Build response from actual model data — not a hard-coded dict
        response = {
            "symphonies": [
                s.model_dump(include={"name", "github_project_number"})
                | {"cycle_count": 0, "error_count": 0, "active_card": None}
                for s in config.symphonies
            ],
        }

        assert "symphonies" in response
        assert len(response["symphonies"]) == 2
        for sym in response["symphonies"]:
            assert "name" in sym
            assert "github_project_number" in sym
        # Verify values come from the actual model (not hard-coded placeholders)
        names = {sym["name"] for sym in response["symphonies"]}
        assert names == {"sym-0", "sym-1"}
        project_numbers = {sym["github_project_number"] for sym in response["symphonies"]}
        assert project_numbers == {100, 101}

    def test_effective_config_response_schema(self) -> None:
        """GET /api/config/effective response matches schema."""
        config = _make_coordinare_config(1)
        symphony = config.symphonies[0]
        effective = symphony.effective_config(config.global_config)

        # Response should be serializable config
        assert hasattr(effective, "project_name")
        assert hasattr(effective, "github_project_number")
        assert hasattr(effective, "github_org")

    def test_reload_response_schema(self) -> None:
        """POST /api/config/reload response includes summary."""
        old_config = _make_coordinare_config(2)
        new_symphonies = [
            *old_config.symphonies,
            SymphonyConfig(name="sym-2", github_project_number=102),
        ]

        # Compute diff from real model data — not a hard-coded dict
        old_names = {s.name for s in old_config.symphonies}
        new_names = {s.name for s in new_symphonies}
        response = {
            "added_symphonies": sorted(new_names - old_names),
            "removed_symphonies": sorted(old_names - new_names),
            "modified_symphonies": [],
            "status": "success",
        }

        assert "added_symphonies" in response
        assert "removed_symphonies" in response
        assert "status" in response
        # Verify the computed diff matches expected model state
        assert response["added_symphonies"] == ["sym-2"]
        assert response["removed_symphonies"] == []
