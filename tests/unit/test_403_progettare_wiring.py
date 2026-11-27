"""403: Progettare wired into startup, dispatch, monitor and doctor.

The acceptance path: an architect opted in to Progettare reaches implementation with the
implementer, QA and documenter briefs cut from Progettare's own record.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coordinare.__main__ import _build_stage_provider_services
from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration
from coordinare.doctor import Status, check_stage_providers
from coordinare.graph.nodes.dispatch_performer import _acquire_via_slot_manager, inject_briefs
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state
from coordinare.services.progettare import ProgettareProvider, blueprint_from_run
from coordinare.services.stage_contract import (
    JobState,
    StageJob,
    StageOutcome,
    StageVerdict,
    WorkItemStageService,
    work_item_digest,
)
from coordinare.services.stage_providers import github_work_item_reader
from tests.unit import fake_progettare

TITLE = "Export widgets as CSV"
BODY = "## Acceptance\n- [ ] a CSV download exists\n"
_PROGETTARE = {
    "kind": "progettare", "version": "2026.11.3", "runs_dir": "/var/lib/coordinare/progettare-runs",
    "gh_token_env": "GITHUB_TOKEN",
}
_SCRUTARE = {
    "kind": "scrutare", "url": "http://scrutare.test", "token_env": "SCRUTARE_API_TOKEN",
    "installation": 7, "review_login": "vivi-scrutare[bot]",
}


def _config(*, opted_in: bool) -> ProjectConfiguration:
    architect = PerformerRoleConfig(provider="progettare") if opted_in else PerformerRoleConfig()
    return ProjectConfiguration(
        project_name="widgets", github_org="Acme", github_project_number=1, github_token="token",
        human_reviewers=["alice"],
        performers=PerformersConfig(
            implementer=PerformerRoleConfig(), architect=architect, reviewer=PerformerRoleConfig(),
        ),
        stage_providers={"progettare": dict(_PROGETTARE), "scrutare": dict(_SCRUTARE)},
    )


class _GitHub:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def get_issue_details(self, issue_id: str) -> dict[str, Any]:
        self.asked.append(issue_id)
        return {"title": TITLE, "body": BODY, "state": "OPEN", "number": 12}


# -- startup ------------------------------------------------------------------------------


def test_startup_builds_a_planning_service_beside_the_reviewer() -> None:
    services = _build_stage_provider_services(_config(opted_in=True), {}, _GitHub())
    assert sorted(services) == ["stage-provider:progettare:architecting", "stage-provider:scrutare:reviewing"]
    planner = services["stage-provider:progettare:architecting"]
    assert isinstance(planner, WorkItemStageService)
    assert planner.stage == "architecting"


@pytest.mark.asyncio
async def test_issues_are_read_with_the_owners_github() -> None:
    default, other = _GitHub(), _GitHub()
    reader = github_work_item_reader(lambda owner: {"other": other}.get(owner.lower(), default))
    snapshot = await reader("Other/repo", "I_1")
    assert (snapshot.title, snapshot.body) == (TITLE, BODY)
    assert other.asked == ["I_1"]
    await reader("acme/widgets", "I_2")
    assert default.asked == ["I_2"]


# -- dispatch -----------------------------------------------------------------------------


class _FullSlots:
    def __init__(self) -> None:
        self.pools: dict[str, Any] = {}

    def acquire(self, stage: str, card_id: str, config: Any = None) -> None:
        raise AssertionError("a provider-backed stage takes no performer slot")


def _route_state(config: ProjectConfiguration) -> tuple[dict[str, Any], WorkItemStageService, object]:
    services = _build_stage_provider_services(config, {}, _GitHub())
    planner = services["stage-provider:progettare:architecting"]
    assert isinstance(planner, WorkItemStageService)
    performer = object()
    state: dict[str, Any] = {
        "config": config,
        "performer_services": {"architecting": performer},
        "performer_services_by_id": {planner.performer_id: planner},
        "slot_manager": _FullSlots(),
    }
    return state, planner, performer


@pytest.mark.asyncio
async def test_an_opted_in_architect_dispatches_to_progettare_without_a_slot() -> None:
    state, planner, _ = _route_state(_config(opted_in=True))
    ctx: dict[str, Any] = {
        "performer_stage": "architecting", "card_id": "CARD_1",
        "performer_services": state["performer_services"],
    }
    assert await _acquire_via_slot_manager(state, ctx) is None
    assert ctx["service"] is planner
    assert getattr(planner, "external_stage", False) is True  # no workspace, no env cache


@pytest.mark.asyncio
async def test_an_architect_that_does_not_opt_in_keeps_its_performer() -> None:
    state, _, performer = _route_state(_config(opted_in=False))
    state["slot_manager"] = None
    ctx: dict[str, Any] = {
        "performer_stage": "architecting", "card_id": "CARD_1",
        "performer_services": state["performer_services"],
    }
    assert await _acquire_via_slot_manager(state, ctx) is None
    assert ctx["service"] is performer


# -- the plan reaches the readers ---------------------------------------------------------


def _progettare_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A run directory exactly as the fake Progettare CLI writes it."""
    monkeypatch.setenv("FAKE_PROGETTARE_TITLE", TITLE)
    monkeypatch.setenv("FAKE_PROGETTARE_BODY", BODY)
    monkeypatch.delenv("FAKE_PROGETTARE_MODE", raising=False)
    checkout, runs = tmp_path / "checkout", tmp_path / "runs"
    checkout.mkdir()
    runs.mkdir()
    assert fake_progettare.main([
        "blueprint", "--issue", "acme/widgets#12", "--repo", str(checkout),
        "--config", str(checkout / "progettare.yaml"), "--runs-dir", str(runs),
    ]) == 0
    (run_dir,) = runs.iterdir()
    return run_dir


class _Planner:
    name = "progettare"

    def __init__(self, job: StageJob) -> None:
        self.job = job

    async def submit(self, request: Any, request_id: str) -> StageJob:
        return self.job

    async def poll(self, ref: str) -> StageJob:
        return self.job

    async def health(self) -> tuple[bool, str]:
        return True, "ok"


def _monitor_state(outcome: StageOutcome) -> dict[str, Any]:
    async def read(repository: str, node: str) -> Any:
        from coordinare.services.stage_contract import WorkItemSnapshot

        return WorkItemSnapshot(title=TITLE, body=BODY, state="OPEN")

    ref = "coordinare.architecting.0123456789abcdef." + "a" * 40 + ".1"
    planner = WorkItemStageService(
        _Planner(StageJob(ref=ref, state=JobState.DONE, outcome=outcome)),
        stage="architecting", work_item_reader=read,
    )
    state = initial_state()
    state["performer_services"] = {"architecting": planner}
    state["performer_stage"] = "architecting"
    state["lifecycle_sequence"] = ["architecting", "implementing", "qa"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": f"{ref}@acme/widgets#12@I_node12"}
    state["current_symphony"] = "widgets"
    return state


@pytest.mark.asyncio
async def test_progettares_briefs_reach_the_implementer_qa_and_documenter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _progettare_run(tmp_path, monkeypatch)
    intake = json.loads((run_dir / "intake.json").read_text())
    manifest = json.loads((run_dir / "run.json").read_text())
    blueprint = blueprint_from_run(run_dir, intake, manifest)
    outcome = StageOutcome(
        verdict=StageVerdict.PASSED, reviewed_head="a" * 40, input_digest=work_item_digest(TITLE, BODY),
        blueprint=blueprint, record_ref=str(run_dir),
    )
    result = await monitor_performer(_monitor_state(outcome))
    assert result["performer_stage"] == "implementing"
    assert result["blueprint"]["source"]["provider"] == "progettare"

    implementer: dict[str, Any] = {}
    inject_briefs(implementer, result, role="implementing")
    assert implementer["implementation_brief"]["milestones"][0]["goal"] == "CSV export"
    assert implementer["implementer_single_turn"] is True
    qa: dict[str, Any] = {}
    inject_briefs(qa, result, role="qa")
    assert [c["action"] for c in qa["verification_brief"]["criteria"]] == ["GET /widgets.csv returns every widget"]
    documenter: dict[str, Any] = {}
    inject_briefs(documenter, result, role="documenting")
    assert documenter["documentation_brief"]["docs"][0]["say"] == "the CSV export in the user guide"


@pytest.mark.asyncio
async def test_blocked_puts_progettares_questions_on_the_card() -> None:
    outcome = StageOutcome(
        verdict=StageVerdict.NEEDS_HUMAN, reviewed_head="a" * 40, input_digest=work_item_digest(TITLE, BODY),
        summary="Progettare blocked at intake", questions=["What are the acceptance criteria for this issue?"],
    )
    result = await monitor_performer(_monitor_state(outcome))
    assert result["phase"] == "blocked"
    assert "What are the acceptance criteria for this issue?" in result["open_questions"]
    assert not result.get("blueprint")


# -- doctor -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_doctor_checks_progettare_when_the_architect_uses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def healthy(self: ProgettareProvider) -> tuple[bool, str]:
        return True, "progettare 2026.11.3 at /usr/local/bin/progettare"

    monkeypatch.setattr(ProgettareProvider, "health", healthy)
    (result,) = await check_stage_providers(_config(opted_in=True))
    assert result.status is Status.OK
    assert result.name == "stage provider 'progettare' (progettare, architect)"
    assert result.detail == "progettare 2026.11.3 at /usr/local/bin/progettare"


@pytest.mark.asyncio
async def test_doctor_names_the_fix_for_progettare(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(self: ProgettareProvider) -> tuple[bool, str]:
        return False, "progettare --version reports 2026.10.0, but 2026.11.3 is pinned"

    monkeypatch.setattr(ProgettareProvider, "health", broken)
    (result,) = await check_stage_providers(_config(opted_in=True))
    assert result.status is Status.FAIL
    assert result.fix is not None
    assert "Install progettare 2026.11.3" in result.fix
    assert "GITHUB_TOKEN" in result.fix


@pytest.mark.asyncio
async def test_doctor_ignores_progettare_when_no_role_uses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(self: ProgettareProvider) -> tuple[bool, str]:
        raise AssertionError

    monkeypatch.setattr(ProgettareProvider, "health", never)
    assert await check_stage_providers(_config(opted_in=False)) == []


# -- copilot round ------------------------------------------------------------------------


def _planned_by_progettare(card: dict[str, Any], clarifications: list[dict[str, Any]]) -> dict[str, Any]:
    from coordinare.graph.nodes.dispatch_performer import stamp_blueprint_signature
    from coordinare.services.stage_contract import work_item_pin

    state = initial_state()
    state["current_card"] = dict(card)
    state["card_clarifications"] = list(clarifications)
    state["requirements_changed"] = False
    state["relay_feedback"] = []
    pin = work_item_pin(work_item_digest(card["title"], card["description"]), clarifications)
    state["blueprint"] = {"milestones": [{"goal": "CSV"}], "source": {"provider": "progettare", "pin": pin}}
    stamp_blueprint_signature(state)
    return state


def test_a_progettare_plan_is_reused_only_while_its_input_is_unchanged() -> None:
    from coordinare.graph.nodes.dispatch_performer import blueprint_reuse_allowed

    card = {"id": "PVTI_card1", "title": TITLE, "description": BODY}
    state = _planned_by_progettare(card, [])
    assert blueprint_reuse_allowed(state, "architecting") is True
    # The body was edited after the plan was lifted: the old plan is not reused.
    state["current_card"] = {**card, "description": BODY + "- [ ] and JSON\n"}
    assert blueprint_reuse_allowed(state, "architecting") is False
    # An answer recorded after the run read the issue: not reused either.
    state = _planned_by_progettare(card, [])
    state["card_clarifications"] = [{"questions": ["Which format?"], "answer": "CSV"}]
    state["blueprint_signature"] = None
    from coordinare.graph.nodes.dispatch_performer import stamp_blueprint_signature

    stamp_blueprint_signature(state)  # as if the lift happened after the answer
    assert blueprint_reuse_allowed(state, "architecting") is False


def test_an_issue_on_another_host_never_gets_the_token() -> None:
    import asyncio

    from coordinare.transport.http_transport import PerformerAuthError

    services = _build_stage_provider_services(_config(opted_in=True), {}, _GitHub())
    planner = services["stage-provider:progettare:architecting"]
    card = {"id": "C", "issue_url": "https://github.evil.example/acme/widgets/issues/12", "issue_id": "I_1"}
    with pytest.raises(PerformerAuthError, match="not the configured GitHub host"):
        asyncio.run(planner.dispatch_card(card))


@pytest.mark.parametrize(("git_base_url", "hosts"), [
    ("https://github.com", {"github.com"}),
    ("https://acme.ghe.com", {"acme.ghe.com"}),
    ("https://ghe.example:8443/", {"ghe.example:8443"}),
    ("https://ghe.example:443", {"ghe.example"}),
])
def test_the_github_host_is_the_one_coordinare_clones_from(git_base_url: str, hosts: set[str]) -> None:
    from coordinare.services.stage_providers import github_hosts

    config = _config(opted_in=True).model_copy(update={"git_base_url": git_base_url})
    assert github_hosts(config) == frozenset(hosts)


def test_issue_urls_compare_with_their_port() -> None:
    from coordinare.services.stage_contract import parse_issue_url

    assert parse_issue_url("https://ghe.example:8443/o/r/issues/1") == ("ghe.example:8443", "o/r", 1)
    assert parse_issue_url("https://GHE.example:443/o/r/issues/1") == ("ghe.example", "o/r", 1)


def test_the_clone_goes_to_the_configured_git_host_not_the_cards() -> None:
    from coordinare.services.progettare import ProgettareProvider
    from coordinare.services.stage_contract import WorkItemRequest
    from coordinare.stage_provider_config import ProgettareProviderConfig

    provider = ProgettareProvider(
        "progettare", ProgettareProviderConfig(**_PROGETTARE), git_base_url="https://ghe.example:8443",
    )
    request = WorkItemRequest(card_id="C", stage="architecting", repository="acme/widgets", number=1,
                              node_id="I_1", host="ghe.example:8443", pin="a" * 40)
    assert provider._clone_url(request) == "https://ghe.example:8443/acme/widgets.git"
    env = provider._child_env("t", request.host)
    assert env["GH_HOST"] == "ghe.example:8443"


def test_the_token_header_is_scoped_to_the_clone_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinare.services.progettare_runner import _git_env

    monkeypatch.setenv("GH_TOKEN", "t")
    env = _git_env("https://ghe.example:8443/acme/widgets.git")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://ghe.example:8443/.extraheader"
