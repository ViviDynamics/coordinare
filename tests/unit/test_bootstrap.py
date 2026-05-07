"""Unit tests for performer lifecycle bootstrap (019, T007)."""
from __future__ import annotations

from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration

# ---------------------------------------------------------------------------
# _build_lifecycle_sequence
# ---------------------------------------------------------------------------


def _make_config(**performers_kwargs: PerformerRoleConfig | None) -> ProjectConfiguration:
    """Build a minimal ProjectConfiguration with the given performer roles."""
    performers = PerformersConfig(**performers_kwargs)
    return ProjectConfiguration(
        project_name="Test",
        github_org="acme",
        github_project_number=1,
        github_token="tok",
        human_reviewers=["alice"],
        performers=performers,
    )


class TestBuildLifecycleSequence:
    """Tests for _build_lifecycle_sequence."""

    def test_canonical_ordering_respected(self) -> None:
        from coordinare.__main__ import _build_lifecycle_sequence

        config = _make_config(
            qa=PerformerRoleConfig(),
            implementer=PerformerRoleConfig(),
            reviewer=PerformerRoleConfig(),
        )
        seq = _build_lifecycle_sequence(config)
        assert seq == ["implementing", "reviewing", "qa"]

    def test_all_roles_configured(self) -> None:
        from coordinare.__main__ import _build_lifecycle_sequence

        config = _make_config(
            advocate=PerformerRoleConfig(),
            assessor=PerformerRoleConfig(),
            architect=PerformerRoleConfig(),
            implementer=PerformerRoleConfig(),
            reviewer=PerformerRoleConfig(),
            security=PerformerRoleConfig(),
            qa=PerformerRoleConfig(),
            tech_writer=PerformerRoleConfig(),
            closer=PerformerRoleConfig(),
        )
        seq = _build_lifecycle_sequence(config)
        assert seq == [
            "advocate", "assessing", "architecting", "implementing",
            "reviewing", "security", "qa", "documenting", "closing_review",
        ]

    def test_closer_runs_last_when_configured(self) -> None:
        """042: The closer must always be the final stage when configured —
        its job is the final sweep after every other performer."""
        from coordinare.__main__ import _build_lifecycle_sequence

        config = _make_config(
            implementer=PerformerRoleConfig(),
            qa=PerformerRoleConfig(),
            closer=PerformerRoleConfig(),
        )
        seq = _build_lifecycle_sequence(config)
        assert seq[-1] == "closing_review"
        assert seq == ["implementing", "qa", "closing_review"]

    def test_roles_absent_from_config_are_excluded(self) -> None:
        from coordinare.__main__ import _build_lifecycle_sequence

        config = _make_config(
            implementer=PerformerRoleConfig(),
            tech_writer=PerformerRoleConfig(),
        )
        seq = _build_lifecycle_sequence(config)
        assert seq == ["implementing", "documenting"]
        assert "reviewing" not in seq

    def test_empty_config_with_legacy_transport_falls_back(self) -> None:
        from coordinare.__main__ import _build_lifecycle_sequence

        # No performers configured, but legacy agent_transport exists
        config = _make_config()
        # agent_transport defaults to "subprocess" in ProjectConfiguration
        seq = _build_lifecycle_sequence(config)
        assert seq == ["implementing"]

    def test_single_role_configured(self) -> None:
        from coordinare.__main__ import _build_lifecycle_sequence

        config = _make_config(security=PerformerRoleConfig())
        seq = _build_lifecycle_sequence(config)
        assert seq == ["security"]


class TestBuildPerformerServices:
    """Tests for _build_performer_services."""

    def test_returns_empty_when_no_roles_configured(self) -> None:
        from coordinare.__main__ import _build_circuit_breakers, _build_performer_services

        config = _make_config()
        cbs = _build_circuit_breakers(config)
        services = _build_performer_services(config, cbs)
        assert services == {}

    def test_returns_services_for_configured_roles(self) -> None:
        from coordinare.__main__ import _build_circuit_breakers, _build_performer_services

        config = _make_config(
            implementer=PerformerRoleConfig(backend="opencode", executable="fake-binary"),
            reviewer=PerformerRoleConfig(backend="claude-code", executable="fake-binary"),
        )
        cbs = _build_circuit_breakers(config)
        services = _build_performer_services(config, cbs)
        assert "implementing" in services
        assert "reviewing" in services
        assert "security" not in services

    def test_distinct_services_for_different_backends(self) -> None:
        from coordinare.__main__ import _build_circuit_breakers, _build_performer_services

        config = _make_config(
            implementer=PerformerRoleConfig(backend="opencode", executable="fake-binary"),
            security=PerformerRoleConfig(backend="claude-code", executable="fake-binary"),
        )
        cbs = _build_circuit_breakers(config)
        services = _build_performer_services(config, cbs)
        # Different roles should produce distinct service instances
        assert services["implementing"] is not services["security"]
