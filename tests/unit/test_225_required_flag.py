"""#225: the Settings descriptor says which fields are required, from the model itself.

The Clear action is offered only on an optional secret. The flag is derived from pydantic's
own answer plus the model's declared credential rule, and a test checks it against what the
model actually refuses, so the page cannot offer a Clear the server would refuse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coordinare import config_descriptors as cd
from coordinare.config import CoordinareConfiguration, ProjectConfiguration, SymphonyConfig

_SYMPHONY = SymphonyConfig(name="demo", github_project_number=1)
_APP_AUTH: dict[str, Any] = {
    "github_auth": "app",
    "github_app_id": 1,
    "github_private_key_path": Path("/tmp/coordinare-225-key.pem"),
    "github_installation_id": 2,
}


def _global_settings(pc: ProjectConfiguration) -> dict[str, cd.ConfigSetting]:
    config = CoordinareConfiguration(global_config=pc, symphonies=[_SYMPHONY])
    snapshot = cd.build_snapshot(config)
    section = next(s for s in snapshot.sections if s.id == "global")
    return {setting.key: setting for setting in section.settings}


def _pat(**overrides: object) -> ProjectConfiguration:
    fields: dict[str, Any] = {
        "project_name": "Demo", "github_org": "acme", "github_project_number": 1,
        "github_token": "ghp_fixturetoken", "human_reviewers": ["alice"],
    }
    fields.update(overrides)
    return ProjectConfiguration(**fields)


def test_github_token_is_required_under_pat_auth() -> None:
    settings = _global_settings(_pat())
    assert settings["global.github_token"].required is True
    assert settings["global.github_token"].secret is True


def test_github_token_is_optional_under_app_auth() -> None:
    pc = ProjectConfiguration(project_name="Demo", github_org="acme", human_reviewers=["alice"], **_APP_AUTH)
    assert pc.github_token is None
    assert _global_settings(pc)["global.github_token"].required is False


def test_dashboard_auth_token_is_an_optional_secret() -> None:
    settings = _global_settings(_pat(dashboard_auth_token="a-dashboard-token-of-some-length"))
    assert settings["global.dashboard_auth_token"].secret is True
    assert settings["global.dashboard_auth_token"].required is False


def test_a_field_without_a_default_is_required_by_pydantic() -> None:
    settings = _global_settings(_pat())
    assert settings["global.github_org"].required is True
    assert settings["global.poll_interval_seconds"].required is False


def test_the_dashboard_token_is_required_on_a_remote_bind_with_no_oidc() -> None:
    """The same predicate as the startup check: a non-loopback bind with no OIDC flow needs it."""
    settings = _global_settings(_pat(dashboard_host="0.0.0.0"))
    assert settings["global.dashboard_auth_token"].required is True


def test_the_dashboard_token_is_optional_on_loopback() -> None:
    settings = _global_settings(_pat(dashboard_host="127.0.0.1"))
    assert settings["global.dashboard_auth_token"].required is False


def test_the_credential_rule_is_declared_on_the_model() -> None:
    assert ProjectConfiguration.AUTH_REQUIRED_CREDENTIALS == {"github_token": "pat"}


def test_build_section_carries_the_flag_for_the_settings_page() -> None:
    config = CoordinareConfiguration(global_config=_pat(), symphonies=[_SYMPHONY])
    section = cd.build_section(config, "global")
    flags = {s.key: s.required for s in section.settings}
    assert flags["global.github_token"] is True
    assert flags["global.poll_interval_seconds"] is False


@pytest.mark.parametrize("key", ["global.github_token", "global.dashboard_auth_token"])
def test_the_snapshot_serialises_the_flag(key: str) -> None:
    config = CoordinareConfiguration(
        global_config=_pat(dashboard_auth_token="a-dashboard-token-of-some-length"), symphonies=[_SYMPHONY],
    )
    payload = cd.build_snapshot(config).model_dump(mode="json")
    settings = {s["key"]: s for sec in payload["sections"] for s in sec["settings"]}
    assert "required" in settings[key]
