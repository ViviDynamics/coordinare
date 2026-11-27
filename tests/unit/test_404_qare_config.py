"""404: QAre in the stage_providers catalog, routing the QA stage, doctor, and startup."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from coordinare.__main__ import _build_stage_provider_services
from coordinare.config import (
    PerformerRoleConfig,
    PerformersConfig,
    ProjectConfiguration,
    SymphonyConfig,
)
from coordinare.config_descriptors import _is_scalar_field
from coordinare.doctor import Status, check_stage_providers
from coordinare.graph.nodes.dispatch_performer import _acquire_via_slot_manager
from coordinare.services.qare import QAreProvider
from coordinare.services.stage_contract import PullRequestSnapshot
from coordinare.services.stage_providers import (
    build_stage_provider_services,
    provider_review_logins,
    role_provider,
    stage_provider_for,
)
from coordinare.stage_provider_config import QAreProviderConfig
from tests.unit import fake_qare
from tests.unit.qare_fixtures import FAKE_QARE

_QARE: dict[str, Any] = {"kind": "qare", "version": fake_qare.VERSION, "command": FAKE_QARE}
_SCRUTARE = {
    "kind": "scrutare", "url": "http://scrutare:8080", "token_env": "SCRUTARE_API_TOKEN",
    "installation": 7, "review_login": "vivi-scrutare[bot]",
}


def _config(*, qa: dict[str, Any] | None = None, providers: dict[str, Any] | None = None, **kw: Any) -> ProjectConfiguration:
    performers = PerformersConfig(implementer=PerformerRoleConfig(), qa=PerformerRoleConfig(**(qa or {})))
    base: dict[str, Any] = {
        "project_name": "widgets", "github_org": "acme", "github_project_number": 1,
        "github_token": "token", "human_reviewers": ["alice"], "performers": performers,
        "stage_providers": providers if providers is not None else {"qare": dict(_QARE)},
    }
    base.update(kw)
    return ProjectConfiguration(**base)


async def _no_pr(repository: str, node: str) -> PullRequestSnapshot:
    return PullRequestSnapshot(head_sha="a" * 40)


# -- config ------------------------------------------------------------------------------------


def test_qa_is_unchanged_until_it_names_qare() -> None:
    assert role_provider(_config(), "qa") is None
    assert role_provider(_config(providers={}), "qa") is None


def test_qa_can_opt_in_to_qare() -> None:
    config = _config(qa={"provider": "qare"})
    assert role_provider(config, "qa") == "qare"
    provider = config.stage_providers["qare"]
    assert isinstance(provider, QAreProviderConfig)
    assert provider.profile == ".qa"
    assert provider.command == FAKE_QARE
    # QAre posts nothing under a login of its own, so monitor_pr has nothing to skip.
    assert provider_review_logins(config) == set()


def test_qare_fills_qa_only() -> None:
    performers = PerformersConfig(implementer=PerformerRoleConfig(), reviewer=PerformerRoleConfig(provider="qare"))
    with pytest.raises(ValidationError, match="a qare provider can fill qa, not reviewer"):
        _config(performers=performers)


def test_scrutare_and_qare_live_in_one_catalog() -> None:
    config = _config(qa={"provider": "qare"}, providers={"qare": dict(_QARE), "scrutare": dict(_SCRUTARE)})
    services = build_stage_provider_services(config, _no_pr)
    assert sorted(services) == ["stage-provider:qare:qa", "stage-provider:scrutare:reviewing"]


@pytest.mark.parametrize("version", ["", "  ", "latest please", "v1;rm"])
def test_qare_needs_an_exact_pinned_version(version: str) -> None:
    with pytest.raises(ValidationError, match="version must be the exact QAre release"):
        _config(providers={"qare": {**_QARE, "version": version}})


def test_env_passthrough_takes_names_never_values() -> None:
    secret = "sk-live-0123456789abcdef"
    with pytest.raises(ValidationError) as caught:
        _config(providers={"qare": {**_QARE, "env_passthrough": ["OPENAI_API_KEY", secret]}})
    assert "environment variable NAMES" in str(caught.value)
    assert secret not in str(caught.value)


@pytest.mark.parametrize("profile", ["/etc/qa", "../qa", "a/../../b", "", "  "])
def test_the_profile_stays_inside_the_repository(profile: str) -> None:
    with pytest.raises(ValidationError, match="profile must be a relative path"):
        _config(providers={"qare": {**_QARE, "profile": profile}})


def test_unknown_qare_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        _config(providers={"qare": {**_QARE, "post": "pr"}})


def test_the_catalog_stays_off_the_global_settings_form() -> None:
    assert not _is_scalar_field(ProjectConfiguration.model_fields["stage_providers"])


def test_runs_live_beside_the_state_file_unless_configured(tmp_path: Path) -> None:
    config = _config(qa={"provider": "qare"}, state_file_path=tmp_path / "state" / "coordinare.state.json")
    service = build_stage_provider_services(config, _no_pr)["stage-provider:qare:qa"]
    provider = service._provider
    assert isinstance(provider, QAreProvider)
    assert provider.work_dir == tmp_path / "state" / "stage-providers" / "qare"
    configured = _config(providers={"qare": {**_QARE, "work_dir": str(tmp_path / "runs")}})
    other = build_stage_provider_services(configured, _no_pr)["stage-provider:qare:qa"]._provider
    assert isinstance(other, QAreProvider)
    assert other.work_dir == tmp_path / "runs"


def test_a_symphony_opts_in_and_the_global_default_is_unchanged() -> None:
    base = _config()
    symphony = SymphonyConfig(
        name="widgets", github_project_number=2,
        overrides={"performers": {"implementer": {}, "qa": {"provider": "qare"}}},
    )
    assert role_provider(symphony.effective_config(base), "qa") == "qare"
    assert role_provider(base, "qa") is None


# -- routing --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_routes_qa_to_qare_for_an_opted_in_config() -> None:
    config = _config(qa={"provider": "qare"})
    service = build_stage_provider_services(config, _no_pr)["stage-provider:qare:qa"]
    performer = object()
    state: dict[str, Any] = {
        "config": config, "performer_services": {"qa": performer},
        "performer_services_by_id": {service.performer_id: service},
    }
    assert stage_provider_for(state, "qa") == (service, None)
    ctx: dict[str, Any] = {"performer_stage": "qa", "card_id": "CARD_1", "performer_services": state["performer_services"]}
    assert await _acquire_via_slot_manager(state, ctx) is None
    assert ctx["service"] is service
    state["config"] = _config()
    ctx = {"performer_stage": "qa", "card_id": "CARD_1", "performer_services": state["performer_services"]}
    assert await _acquire_via_slot_manager(state, ctx) is None
    assert ctx["service"] is performer


def test_startup_gives_qare_github_access() -> None:
    class _GitHub:
        async def current_token(self) -> str:
            return "t"

        def rest_api_base(self) -> str:
            return "https://ghe.example.com/api/v3"

    config = _config(qa={"provider": "qare"})
    services = _build_stage_provider_services(config, {"acme": _GitHub()}, _GitHub())
    provider = services["stage-provider:qare:qa"]._provider
    assert isinstance(provider, QAreProvider)
    assert provider._github is not None
    assert provider._github.web_base("acme/widgets") == "https://ghe.example.com"


# -- doctor ---------------------------------------------------------------------------------


def _patch_profile_probe(monkeypatch: pytest.MonkeyPatch, status: int) -> list[str]:
    seen: list[str] = []

    class _Auth:
        async def get_token(self) -> str:
            return "t"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(status, json=[{"name": "config.yml"}] if status == 200 else {})

    real = httpx.AsyncClient
    monkeypatch.setattr("coordinare.auth.build_auth", lambda config: _Auth())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


@pytest.mark.asyncio
async def test_doctor_passes_qare_at_its_pin_with_a_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_profile_probe(monkeypatch, 200)
    config = _config(qa={"provider": "qare"}, providers={"qare": {**_QARE, "work_dir": str(tmp_path)}})
    version, profile = await check_stage_providers(config)
    assert version.status is Status.OK, version.detail
    assert "qare" in version.name
    assert "qa" in version.name
    assert profile.status is Status.OK
    assert seen == ["https://api.github.com/repos/acme/widgets/contents/.qa"]


@pytest.mark.asyncio
async def test_doctor_fails_another_version_and_a_missing_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_profile_probe(monkeypatch, 404)
    config = _config(qa={"provider": "qare"}, providers={"qare": {**_QARE, "version": "2026.1.1", "work_dir": str(tmp_path)}})
    version, profile = await check_stage_providers(config)
    assert version.status is Status.FAIL
    assert version.fix is not None
    assert "2026.1.1" in version.fix
    assert profile.status is Status.FAIL
    assert profile.fix is not None
    assert "qare init" in profile.fix


@pytest.mark.asyncio
async def test_doctor_skips_qare_when_qa_does_not_use_it() -> None:
    assert await check_stage_providers(_config()) == []
