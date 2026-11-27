"""402: doctor checks the providers a role uses, and startup wires their services."""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.__main__ import _build_stage_provider_services, _role_provider
from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration
from coordinare.doctor import Status, check_stage_providers
from coordinare.services.scrutare import ScrutareProvider
from coordinare.services.stage_providers import github_pr_reader
from coordinare.transport.base import TransportError

_SCRUTARE = {
    "kind": "scrutare", "url": "http://scrutare.test", "token_env": "SCRUTARE_API_TOKEN",
    "installation": 7,
}


def _config(*, opted_in: bool) -> ProjectConfiguration:
    reviewer = PerformerRoleConfig(provider="scrutare") if opted_in else PerformerRoleConfig()
    return ProjectConfiguration(
        project_name="widgets", github_org="Acme", github_project_number=1, github_token="token",
        human_reviewers=["alice"],
        performers=PerformersConfig(implementer=PerformerRoleConfig(), reviewer=reviewer),
        stage_providers={"scrutare": dict(_SCRUTARE)},
    )


async def test_doctor_passes_a_reachable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    async def healthy(self: ScrutareProvider) -> tuple[bool, str]:
        return True, "reachable, token accepted"

    monkeypatch.setattr(ScrutareProvider, "health", healthy)
    (result,) = await check_stage_providers(_config(opted_in=True))
    assert result.status is Status.OK
    assert "scrutare" in result.name
    assert "reviewer" in result.name


@pytest.mark.parametrize("failure", ["refused", "unreachable"])
async def test_doctor_fails_an_unusable_provider_and_names_the_fix(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    async def broken(self: ScrutareProvider) -> tuple[bool, str]:
        if failure == "unreachable":
            msg = "Scrutare at http://scrutare.test is unreachable: ConnectError"
            raise TransportError(msg)
        return False, "Scrutare refused the token in SCRUTARE_API_TOKEN (401)"

    monkeypatch.setattr(ScrutareProvider, "health", broken)
    (result,) = await check_stage_providers(_config(opted_in=True))
    assert result.status is Status.FAIL
    assert result.fix is not None
    assert "SCRUTARE_API_TOKEN" in result.fix


async def test_doctor_ignores_a_provider_no_role_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(self: ScrutareProvider) -> tuple[bool, str]:
        raise AssertionError

    monkeypatch.setattr(ScrutareProvider, "health", never)
    assert await check_stage_providers(_config(opted_in=False)) == []


class _GitHub:
    def __init__(self, head: str) -> None:
        self.head = head
        self.asked: list[str] = []

    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]:
        self.asked.append(pr_id)
        return {"head_oid": self.head, "reviews": [{"id": "R1"}]}


async def test_startup_builds_the_service_and_reads_prs_with_the_owners_github() -> None:
    default, symphony = _GitHub("a" * 40), _GitHub("b" * 40)
    services = _build_stage_provider_services(_config(opted_in=True), {"other": symphony}, default)
    assert list(services) == ["stage-provider:scrutare:reviewing"]
    assert _role_provider(_config(opted_in=True), "reviewing") == "scrutare"
    assert _role_provider(_config(opted_in=False), "reviewing") is None

    reader = github_pr_reader(lambda owner: {"other": symphony}.get(owner.lower(), default))
    snapshot = await reader("Other/repo", "PR_1")
    assert snapshot.head_sha == "b" * 40
    assert snapshot.reviews == [{"id": "R1"}]
    assert symphony.asked == ["PR_1"]
    assert (await reader("acme/widgets", "PR_2")).head_sha == "a" * 40
