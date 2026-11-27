"""402: stage provider config, routing by the effective config, and the review identity."""
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
from coordinare.graph.nodes.dispatch_performer import _acquire_via_slot_manager
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state
from coordinare.services.stage_contract import ExternalStageService, PullRequestSnapshot
from coordinare.services.stage_providers import (
    build_stage_provider_services,
    provider_review_logins,
    role_provider,
    stage_provider_for,
)
from tests.unit.fake_scrutare import LOGIN

_HEAD = "a" * 40
_SCRUTARE = {
    "kind": "scrutare", "url": "http://scrutare:8080",
    "token_env": "SCRUTARE_API_TOKEN", "installation": 169458182, "review_login": LOGIN,
}


def _config(*, reviewer: dict[str, Any] | None = None, providers: dict[str, Any] | None = None, **kw: Any) -> ProjectConfiguration:
    performers = PerformersConfig(
        implementer=PerformerRoleConfig(),
        reviewer=PerformerRoleConfig(**(reviewer or {})),
    )
    base: dict[str, Any] = {
        "project_name": "widgets", "github_org": "acme", "github_project_number": 1,
        "github_token": "token", "human_reviewers": ["alice"], "performers": performers,
        "stage_providers": providers if providers is not None else {"scrutare": dict(_SCRUTARE)},
    }
    base.update(kw)
    return ProjectConfiguration(**base)


async def _no_pr(repository: str, node: str) -> PullRequestSnapshot:
    return PullRequestSnapshot(head_sha=_HEAD)


# -- config ------------------------------------------------------------------------------


def test_the_default_names_no_provider() -> None:
    config = _config(providers={})
    assert config.stage_providers == {}
    assert role_provider(config, "reviewing") is None


def test_a_reviewer_can_opt_in_to_scrutare() -> None:
    config = _config(reviewer={"provider": "scrutare"})
    assert role_provider(config, "reviewing") == "scrutare"
    assert config.stage_providers["scrutare"].installation == 169458182
    assert provider_review_logins(config) == {"vivi-scrutare"}


def test_an_unknown_provider_is_named() -> None:
    with pytest.raises(ValidationError, match="names 'scrutar', which is not in stage_providers"):
        _config(reviewer={"provider": "scrutar"})


def test_scrutare_cannot_fill_another_role() -> None:
    performers = PerformersConfig(
        implementer=PerformerRoleConfig(provider="scrutare"), reviewer=PerformerRoleConfig(),
    )
    with pytest.raises(ValidationError, match="a scrutare provider can fill reviewer, not implementer"):
        _config(performers=performers)


def test_a_token_pasted_where_its_variable_name_belongs_is_refused_without_echoing_it() -> None:
    secret = "ghs_s3cr3t-token-value"
    with pytest.raises(ValidationError) as caught:
        _config(providers={"scrutare": {**_SCRUTARE, "token_env": secret}})
    assert "token_env must be the NAME of an environment variable" in str(caught.value)
    assert secret not in str(caught.value)


@pytest.mark.parametrize("url", [
    "scrutare.local", "ftp://scrutare.local", "http://user:pw@scrutare.local", "http://scrutare.local/?token=x",
])
def test_a_bad_url_is_refused(url: str) -> None:
    with pytest.raises(ValidationError, match="url must"):
        _config(providers={"scrutare": {**_SCRUTARE, "url": url}})


def test_unknown_provider_fields_and_kinds_are_refused() -> None:
    with pytest.raises(ValidationError):
        _config(providers={"scrutare": {**_SCRUTARE, "token": "x"}})
    with pytest.raises(ValidationError):
        _config(providers={"scrutare": {**_SCRUTARE, "kind": "other"}})


def test_a_provider_login_can_never_be_a_human_reviewer() -> None:
    with pytest.raises(ValidationError, match="must never count as a human approval"):
        _config(human_reviewers=["alice", LOGIN.upper()])


def test_a_provider_name_must_be_a_plain_key() -> None:
    with pytest.raises(ValidationError, match="lower-case letters"):
        _config(providers={"Scrutare Prod": dict(_SCRUTARE)})


def test_a_symphony_opts_in_through_its_overrides_and_the_default_is_unchanged() -> None:
    base = _config()
    assert role_provider(base, "reviewing") is None
    symphony = SymphonyConfig(
        name="widgets", github_project_number=2,
        overrides={"performers": {"implementer": {}, "reviewer": {"provider": "scrutare"}}},
    )
    assert role_provider(symphony.effective_config(base), "reviewing") == "scrutare"
    assert role_provider(base, "reviewing") is None


# -- services and routing ------------------------------------------------------------------


def test_one_service_per_provider_and_stage() -> None:
    services = build_stage_provider_services(_config(), _no_pr)
    assert list(services) == ["stage-provider:scrutare:reviewing"]
    service = services["stage-provider:scrutare:reviewing"]
    assert service.stage == "reviewing"
    assert service.provider_name == "scrutare"


def _route_state(config: ProjectConfiguration, performer: object | None) -> tuple[dict[str, Any], ExternalStageService]:
    service = build_stage_provider_services(config, _no_pr)["stage-provider:scrutare:reviewing"]
    state: dict[str, Any] = {
        "config": config,
        "performer_services": {"reviewing": performer} if performer is not None else {"reviewing": service},
        "performer_services_by_id": {service.performer_id: service},
    }
    return state, service


def test_routing_follows_the_effective_config() -> None:
    performer = object()
    state, service = _route_state(_config(reviewer={"provider": "scrutare"}), performer)
    assert stage_provider_for(state, "reviewing") == (service, None)
    state["config"] = _config()
    assert stage_provider_for(state, "reviewing") == (None, None)
    # Other stages are never routed.
    assert stage_provider_for(state, "implementing") == (None, None)


async def test_a_symphony_that_opts_out_never_reaches_the_global_provider() -> None:
    state, _ = _route_state(_config(reviewer={"provider": "scrutare"}), None)
    state["config"] = _config()
    ctx: dict[str, Any] = {
        "performer_stage": "reviewing", "card_id": "CARD_1",
        "performer_services": state["performer_services"],
    }
    result = await _acquire_via_slot_manager(state, ctx)
    assert result is not None
    assert result["phase"] == "blocked"
    assert "no performer serves it" in result["open_questions"][0]
    assert "service" not in ctx


def test_a_provider_without_a_running_service_blocks_with_the_reason() -> None:
    config = _config(reviewer={"provider": "scrutare"})
    state = {"config": config, "performer_services_by_id": {}}
    routed, reason = stage_provider_for(state, "reviewing")
    assert routed is None
    assert reason is not None
    assert "declare it there" in reason


def test_a_symphony_cannot_override_the_provider_catalog() -> None:
    symphony = SymphonyConfig(
        name="widgets", github_project_number=2,
        overrides={"stage_providers": {"scrutare": {**_SCRUTARE, "installation": 1}}},
    )
    with pytest.raises(ValueError, match="read from the global config only"):
        symphony.effective_config(_config())


class _FullSlots:
    """A slot manager with no free performer slot, which a provider dispatch must not need."""

    def __init__(self) -> None:
        self.pools: dict[str, Any] = {}

    def acquire(self, stage: str, card_id: str, config: Any = None) -> None:
        raise AssertionError("a provider-backed stage takes no performer slot")

    def is_at_capacity(self, stage: str) -> bool:
        return True


async def test_dispatch_acquires_the_provider_for_an_opted_in_symphony() -> None:
    performer = object()
    state, service = _route_state(_config(reviewer={"provider": "scrutare"}), performer)
    state["slot_manager"] = _FullSlots()
    ctx: dict[str, Any] = {
        "performer_stage": "reviewing", "card_id": "CARD_1",
        "performer_services": state["performer_services"],
    }
    assert await _acquire_via_slot_manager(state, ctx) is None
    assert ctx["service"] is service
    assert ctx["slot_manager"] is None  # nothing to release on an error path


async def test_dispatch_keeps_the_performer_when_nothing_opts_in() -> None:
    performer = object()
    state, _ = _route_state(_config(), performer)
    ctx: dict[str, Any] = {
        "performer_stage": "reviewing", "card_id": "CARD_1",
        "performer_services": state["performer_services"],
    }
    assert await _acquire_via_slot_manager(state, ctx) is None
    assert ctx["service"] is performer


async def test_dispatch_blocks_a_card_whose_provider_is_missing() -> None:
    config = _config(reviewer={"provider": "scrutare"})
    state: dict[str, Any] = {"config": config, "performer_services": {"reviewing": object()}, "performer_services_by_id": {}}
    ctx: dict[str, Any] = {
        "performer_stage": "reviewing", "card_id": "CARD_1",
        "performer_services": state["performer_services"],
    }
    result = await _acquire_via_slot_manager(state, ctx)
    assert result is not None
    assert result["phase"] == "blocked"
    assert "scrutare" in result["open_questions"][0]


# -- monitor_pr: the provider's review is the stage's own output -------------------------------


class _GitHub:
    def __init__(self, reviews: list[dict[str, Any]], decision: str) -> None:
        self._ctx = {"reviews": reviews, "review_threads": [], "head_oid": _HEAD,
                     "review_decision": decision, "state": "OPEN"}

    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]:
        return self._ctx


def _scrutare_review(state: str) -> dict[str, Any]:
    return {"id": "RVW_S", "author_login": LOGIN, "state": state, "commit_oid": _HEAD,
            "submitted_at": "2026-11-27T10:00:00Z", "body": "Scrutare says so",
            "comments": [{"body": "Off by one.", "path": "a.py", "line": 1}]}


def _monitor_state(reviews: list[dict[str, Any]], decision: str) -> dict[str, Any]:
    state = initial_state()
    state["config"] = _config(reviewer={"provider": "scrutare"})
    state["current_card"] = {"pr_node_id": "PR_1", "id": "CARD_402", "title": "t", "pr_url": ""}
    # Even an operator who lists the App's login as trusted, or as a human, does not make
    # its review outside feedback or a human approval.
    state["human_reviewers"] = ["alice", LOGIN]
    state["trusted_bot_reviewers"] = [LOGIN]
    state["github_service"] = _GitHub(reviews, decision)
    state["phase"] = "monitoring_pr"
    return state


async def test_a_scrutare_approval_never_merges() -> None:
    result = await monitor_pr(_monitor_state([_scrutare_review("APPROVED")], "APPROVED"))
    assert result["phase"] == "monitoring_pr"


async def test_a_scrutare_change_request_is_not_routed_a_second_time() -> None:
    result = await monitor_pr(_monitor_state([_scrutare_review("CHANGES_REQUESTED")], "CHANGES_REQUESTED"))
    assert result["phase"] == "monitoring_pr"
    assert not result.get("pending_reviews")


async def test_the_graphql_form_of_the_bot_login_is_recognised() -> None:
    review = {**_scrutare_review("APPROVED"), "author_login": LOGIN.removesuffix("[bot]")}
    state = _monitor_state([review], "APPROVED")
    state["human_reviewers"] = ["alice", LOGIN.removesuffix("[bot]")]
    result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"


def test_only_providers_a_role_uses_are_skipped() -> None:
    """A symphony on its performer reviewer treats Scrutare's webhook reviews like any bot's."""
    assert provider_review_logins(_config()) == set()
    assert provider_review_logins(_config(reviewer={"provider": "scrutare"})) == {"vivi-scrutare"}


async def test_a_human_approval_beside_scrutare_still_merges() -> None:
    human = {"id": "RVW_H", "author_login": "alice", "state": "APPROVED", "commit_oid": _HEAD,
             "submitted_at": "2026-11-27T11:00:00Z", "comments": []}
    state = _monitor_state([_scrutare_review("CHANGES_REQUESTED"), human], "APPROVED")
    result = await monitor_pr(state)
    assert result["phase"] == "merging"


def test_the_dashboard_config_page_says_a_provider_ignores_the_backend() -> None:
    from coordinare.config import CoordinareConfiguration
    from coordinare.config_descriptors import build_snapshot

    config = CoordinareConfiguration(
        global_config=_config(reviewer={"provider": "scrutare"}),
        symphonies=[SymphonyConfig(name="widgets", github_project_number=1)],
    )
    snapshot = build_snapshot(config).model_dump()
    settings = [s for section in snapshot["sections"] for s in section.get("settings", [])]
    (setting,) = [s for s in settings if s["key"] == "global.stage_providers"]
    assert "ignores its backend, mode and workflow" in setting["help"]
