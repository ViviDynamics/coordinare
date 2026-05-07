"""058 — backend must be inherited correctly through effective_config."""
from __future__ import annotations

from coordinare.config import (
    PerformerRoleConfig,
    PerformersConfig,
    ProjectConfiguration,
    SymphonyConfig,
)


def _base_global_config(backend: str = "codex") -> ProjectConfiguration:
    return ProjectConfiguration(
        project_name="test-project",
        github_org="acme",
        github_project_number=1,
        github_token="token",
        human_reviewers=["alice"],
        performers=PerformersConfig(
            default=PerformerRoleConfig(backend=backend, model="gpt-5.4"),
        ),
    )


def _symphony(overrides: dict | None = None) -> SymphonyConfig:
    return SymphonyConfig(
        name="test-symphony",
        github_project_number=99,
        overrides=overrides,
    )


class TestBackendInheritanceLegacy:
    """resolved_role() on a plain ProjectConfiguration (no symphony path)."""

    def test_role_without_backend_inherits_default(self):
        cfg = ProjectConfiguration(
            project_name="p",
            github_org="acme",
            github_project_number=1,
            github_token="token",
            human_reviewers=["alice"],
            performers=PerformersConfig(
                default=PerformerRoleConfig(backend="codex"),
                assessor=PerformerRoleConfig(model="gpt-4-mini"),
            ),
        )
        resolved = cfg.performers.resolved_role("assessor")
        assert resolved is not None
        assert resolved.backend == "codex"

    def test_role_with_explicit_backend_overrides_default(self):
        cfg = ProjectConfiguration(
            project_name="p",
            github_org="acme",
            github_project_number=1,
            github_token="token",
            human_reviewers=["alice"],
            performers=PerformersConfig(
                default=PerformerRoleConfig(backend="codex"),
                assessor=PerformerRoleConfig(backend="opencode", model="gpt-4-mini"),
            ),
        )
        resolved = cfg.performers.resolved_role("assessor")
        assert resolved is not None
        assert resolved.backend == "opencode"


class TestBackendInheritanceThroughEffectiveConfig:
    """resolved_role() on the config produced by SymphonyConfig.effective_config()."""

    def test_role_without_backend_inherits_default_via_effective_config(self):
        global_cfg = ProjectConfiguration(
            project_name="p",
            github_org="acme",
            github_project_number=1,
            github_token="token",
            human_reviewers=["alice"],
            performers=PerformersConfig(
                default=PerformerRoleConfig(backend="codex"),
                assessor=PerformerRoleConfig(model="gpt-4-mini"),
            ),
        )
        effective = _symphony().effective_config(global_cfg)
        resolved = effective.performers.resolved_role("assessor")
        assert resolved is not None
        assert resolved.backend == "codex", (
            "assessor should inherit backend=codex from default, "
            f"but got backend={resolved.backend!r}"
        )

    def test_role_with_explicit_backend_overrides_through_effective_config(self):
        global_cfg = ProjectConfiguration(
            project_name="p",
            github_org="acme",
            github_project_number=1,
            github_token="token",
            human_reviewers=["alice"],
            performers=PerformersConfig(
                default=PerformerRoleConfig(backend="codex"),
                assessor=PerformerRoleConfig(backend="opencode", model="gpt-4-mini"),
            ),
        )
        effective = _symphony().effective_config(global_cfg)
        resolved = effective.performers.resolved_role("assessor")
        assert resolved is not None
        assert resolved.backend == "opencode"

    def test_all_roles_without_backend_inherit_default(self):
        roles_without_backend = {
            "architect": PerformerRoleConfig(model="gpt-5.4"),
            "implementer": PerformerRoleConfig(model="gpt-5.4"),
            "reviewer": PerformerRoleConfig(model="gpt-5.4"),
        }
        global_cfg = ProjectConfiguration(
            project_name="p",
            github_org="acme",
            github_project_number=1,
            github_token="token",
            human_reviewers=["alice"],
            performers=PerformersConfig(
                default=PerformerRoleConfig(backend="codex"),
                **roles_without_backend,
            ),
        )
        effective = _symphony().effective_config(global_cfg)
        for role_name in roles_without_backend:
            resolved = effective.performers.resolved_role(role_name)
            assert resolved is not None
            assert resolved.backend == "codex", (
                f"{role_name}.backend should be 'codex' (inherited from default), "
                f"got {resolved.backend!r}"
            )

    def test_symphony_overrides_do_not_break_backend_inheritance(self):
        """Symphony-level overrides (e.g. max_concurrent_cards) must not affect backend inheritance."""
        global_cfg = ProjectConfiguration(
            project_name="p",
            github_org="acme",
            github_project_number=1,
            github_token="token",
            human_reviewers=["alice"],
            performers=PerformersConfig(
                default=PerformerRoleConfig(backend="codex"),
                assessor=PerformerRoleConfig(model="gpt-4-mini"),
            ),
        )
        effective = _symphony(overrides={"max_concurrent_cards": 3}).effective_config(global_cfg)
        assert effective.max_concurrent_cards == 3
        resolved = effective.performers.resolved_role("assessor")
        assert resolved is not None
        assert resolved.backend == "codex"

    def test_legacy_and_symphony_paths_consistent(self):
        """Same config, both paths must produce the same resolved backend."""
        global_cfg = ProjectConfiguration(
            project_name="p",
            github_org="acme",
            github_project_number=1,
            github_token="token",
            human_reviewers=["alice"],
            performers=PerformersConfig(
                default=PerformerRoleConfig(backend="codex"),
                assessor=PerformerRoleConfig(model="gpt-4-mini"),
            ),
        )
        # Legacy path
        legacy_resolved = global_cfg.performers.resolved_role("assessor")
        # Symphony path
        effective = _symphony().effective_config(global_cfg)
        symphony_resolved = effective.performers.resolved_role("assessor")

        assert legacy_resolved is not None
        assert symphony_resolved is not None
        assert legacy_resolved.backend == symphony_resolved.backend, (
            f"Legacy path gives backend={legacy_resolved.backend!r} "
            f"but symphony path gives backend={symphony_resolved.backend!r}"
        )
