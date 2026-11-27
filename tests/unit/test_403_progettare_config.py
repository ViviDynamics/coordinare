"""403: Progettare as a stage provider kind, chosen for the architect role."""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from coordinare.config import (
    PerformerRoleConfig,
    PerformersConfig,
    ProjectConfiguration,
    SymphonyConfig,
)
from coordinare.services.stage_providers import provider_review_logins, role_provider
from coordinare.stage_provider_config import ProgettareProviderConfig

PROGETTARE: dict[str, Any] = {
    "kind": "progettare",
    "version": "2026.11.3",
    "runs_dir": "/var/lib/coordinare/progettare-runs",
    "gh_token_env": "GITHUB_TOKEN",
}


def _config(*, architect: dict[str, Any] | None = None, providers: dict[str, Any] | None = None, **kw: Any) -> ProjectConfiguration:
    performers = PerformersConfig(
        implementer=PerformerRoleConfig(),
        architect=PerformerRoleConfig(**(architect or {})),
    )
    base: dict[str, Any] = {
        "project_name": "widgets", "github_org": "acme", "github_project_number": 1,
        "github_token": "token", "human_reviewers": ["alice"], "performers": performers,
        "stage_providers": providers if providers is not None else {"progettare": dict(PROGETTARE)},
    }
    base.update(kw)
    return ProjectConfiguration(**base)


def test_an_architect_can_opt_in_to_progettare() -> None:
    config = _config(architect={"provider": "progettare"})
    assert role_provider(config, "architecting") == "progettare"
    entry = config.stage_providers["progettare"]
    assert isinstance(entry, ProgettareProviderConfig)
    assert entry.command == "progettare"
    assert entry.config_path is None
    # Progettare posts no reviews, so monitor_pr has no login to skip for it.
    assert provider_review_logins(config) == set()


def test_declaring_progettare_changes_nothing_until_a_role_names_it() -> None:
    config = _config()
    assert role_provider(config, "architecting") is None


def test_progettare_cannot_fill_another_role() -> None:
    performers = PerformersConfig(
        implementer=PerformerRoleConfig(), reviewer=PerformerRoleConfig(provider="progettare"),
    )
    with pytest.raises(ValidationError, match="a progettare provider can fill architect, not reviewer"):
        _config(performers=performers)


def test_the_version_is_pinned() -> None:
    entry = {k: v for k, v in PROGETTARE.items() if k != "version"}
    with pytest.raises(ValidationError, match="version"):
        _config(providers={"progettare": entry})
    with pytest.raises(ValidationError, match="version must be a release"):
        _config(providers={"progettare": {**PROGETTARE, "version": "latest"}})


def test_a_token_pasted_where_its_variable_name_belongs_is_refused_without_echoing_it() -> None:
    secret = "ghp_s3cr3t-token-value"
    with pytest.raises(ValidationError) as caught:
        _config(providers={"progettare": {**PROGETTARE, "gh_token_env": secret}})
    assert "must be the NAME of an environment variable" in str(caught.value)
    assert secret not in str(caught.value)
    with pytest.raises(ValidationError) as caught:
        _config(providers={"progettare": {**PROGETTARE, "pass_env": ["OPENAI_API_KEY", secret]}})
    assert secret not in str(caught.value)


@pytest.mark.parametrize("field", ["runs_dir", "config_path"])
def test_paths_must_be_absolute(field: str) -> None:
    with pytest.raises(ValidationError, match=f"{field} must be an absolute path"):
        _config(providers={"progettare": {**PROGETTARE, field: "relative/dir"}})


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        _config(providers={"progettare": {**PROGETTARE, "url": "http://progettare"}})


def test_scrutare_and_progettare_share_one_catalog() -> None:
    scrutare = {
        "kind": "scrutare", "url": "http://scrutare:8080", "token_env": "SCRUTARE_API_TOKEN",
        "installation": 1, "review_login": "vivi-scrutare[bot]",
    }
    performers = PerformersConfig(
        implementer=PerformerRoleConfig(),
        architect=PerformerRoleConfig(provider="progettare"),
        reviewer=PerformerRoleConfig(provider="scrutare"),
    )
    config = _config(performers=performers, providers={"progettare": dict(PROGETTARE), "scrutare": scrutare})
    assert role_provider(config, "architecting") == "progettare"
    assert role_provider(config, "reviewing") == "scrutare"
    assert provider_review_logins(config) == {"vivi-scrutare"}


def test_a_symphony_opts_in_through_its_overrides() -> None:
    config = _config()
    symphony = SymphonyConfig(
        name="widgets", github_project_number=1,
        overrides={"performers": {"implementer": {}, "architect": {"provider": "progettare"}}},
    )
    effective = symphony.effective_config(config)
    assert role_provider(effective, "architecting") == "progettare"
    assert role_provider(config, "architecting") is None


def test_the_catalog_with_both_kinds_stays_off_the_global_settings_form() -> None:
    from coordinare.config_descriptors import scalar_field_names

    assert "stage_providers" not in scalar_field_names("global")
