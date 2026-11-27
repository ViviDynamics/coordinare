"""Issue #399: the start checks for work items run through the provider.

``launch_preflight`` used to ask GitHub Projects for its Status columns itself, so
``init``, ``validate`` and ``doctor`` could only ever say whether a GitHub board was
ready. The check now belongs to the provider: GitHub Projects returns today's column
checks under the same names, and another provider returns its own, with its own
message and fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import coordinare.launch_preflight as preflight
from coordinare.config import (
    CoordinareConfiguration,
    ProjectConfiguration,
    SymphonyConfig,
    WorkItemsConfig,
)
from coordinare.doctor import CheckResult, DoctorReport, Status
from coordinare.services import work_item_providers
from coordinare.services.board_provider import (
    OPTIONAL_STATES,
    REQUIRED_STATES,
    GitHubProjectsBoardProvider,
    ProviderPreflight,
)
from coordinare.services.in_memory_board import InMemoryBoardProvider
from coordinare.setup_reports import doctor_checks

if TYPE_CHECKING:
    from pathlib import Path

ALL_COLUMNS = {"to do": "1", "in progress": "2", "in review": "3", "blocked": "4", "done": "5"}


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ProjectConfiguration:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "test-token")
    return ProjectConfiguration(
        preset="solo", harness="codex", github_org="example", github_repo="demo",
        github_project_number=1, human_reviewers=["operator"], state_file_path=tmp_path / "state.json",
    )


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.return_value = httpx.Response(
        httpx.codes.OK, headers={"X-OAuth-Scopes": "repo, project, read:org"},
        json={"permissions": {"push": True}},
    )
    service = MagicMock()
    service.initialize = AsyncMock()
    service.aclose = AsyncMock()
    service.field_cache = {"status_option_ids": dict(ALL_COLUMNS)}
    monkeypatch.setattr(preflight.httpx, "Client", MagicMock(return_value=client))
    monkeypatch.setattr(preflight, "GitHubService", MagicMock(return_value=service))
    return service


def _named(results: list[CheckResult], name: str) -> CheckResult:
    return next(r for r in results if r.name == name)


# --- GitHub Projects: today's checks, under the same names and messages ---------------------


@pytest.mark.asyncio
async def test_github_projects_reports_a_ready_board_as_before(config: Any, github: MagicMock) -> None:
    results = await preflight.check_github(config)

    board = _named(results, "GitHub board")
    assert board.status is Status.OK
    assert board.detail == "Board is reachable and all lifecycle Status columns exist."
    assert "board columns" not in {r.name for r in results}
    github.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_github_projects_reports_missing_columns_as_before(config: Any, github: MagicMock) -> None:
    github.field_cache = {"status_option_ids": {"to do": "1", "done": "5"}}

    results = await preflight.check_github(config)

    columns = _named(results, "board columns")
    assert columns.status is Status.FAIL
    assert columns.detail == "The board is missing columns: BLOCKED, IN_PROGRESS, IN_REVIEW."
    assert columns.fix == (
        "In the board's Status field add To Do (or Ready), In Progress, In Review, Blocked and Done options."
    )


@pytest.mark.asyncio
async def test_github_projects_reports_an_unreadable_board_as_before(config: Any, github: MagicMock) -> None:
    github.initialize.side_effect = ValueError("private board secret")

    results = await preflight.check_github(config)

    board = _named(results, "GitHub board")
    assert board.status is Status.FAIL
    assert board.detail == "Could not read the project board and its Status field after bounded retries."
    assert "github_project_number" in (board.fix or "")
    assert "private board secret" not in str(results)
    github.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_doctor_json_ids_and_messages_are_unchanged(config: Any, github: MagicMock) -> None:
    github.field_cache = {"status_option_ids": {"to do": "1"}}
    missing = doctor_checks(DoctorReport(results=await preflight.check_github(config, explicit=True)))
    github.field_cache = {"status_option_ids": dict(ALL_COLUMNS)}
    ready = doctor_checks(DoctorReport(results=await preflight.check_github(config, explicit=True)))

    (columns,) = [c for c in missing if c["id"] == "doctor.board_columns"]
    assert columns["message"] == "Required board Status columns."
    assert columns["fix"] == "Add To Do or Ready, In Progress, In Review, Blocked and Done to the board Status field."
    (board,) = [c for c in ready if c["id"] == "doctor.github_board"]
    assert board["message"] == "Project board access and lifecycle columns."
    assert board["status"] == "ok"


@pytest.mark.asyncio
async def test_the_column_check_is_the_github_providers_own() -> None:
    service = MagicMock(initialize=AsyncMock(), field_cache={"status_option_ids": dict(ALL_COLUMNS)})
    provider = GitHubProjectsBoardProvider(service)

    assert isinstance(provider, ProviderPreflight)
    (result,) = await provider.preflight()

    assert result.name == "GitHub board"
    assert result.status is Status.OK
    service.initialize.assert_awaited_once()


# --- another provider: its own message and fix -----------------------------------------------


class _Tracker:
    """A provider whose project is not set up, and that says how to fix it."""

    def __init__(self, *, fault: Exception | None = None) -> None:
        self.fault = fault
        self.calls = 0

    async def preflight(self) -> list[CheckResult]:
        self.calls += 1
        if self.fault is not None:
            raise self.fault
        return [CheckResult(
            "work items",
            Status.FAIL,
            "Tracker project PAY has no workflow state for In Progress.",
            "In PAY's workflow settings add a state and map it to In Progress.",
        )]


def _register(monkeypatch: pytest.MonkeyPatch, provider: Any) -> WorkItemsConfig:
    monkeypatch.setitem(work_item_providers.WORK_ITEM_PROVIDERS, "tracker", lambda _block, _github: provider)
    return WorkItemsConfig.model_construct(provider="tracker")


@pytest.mark.asyncio
async def test_a_provider_fails_preflight_with_its_own_message_and_fix(
    config: Any, github: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = _register(monkeypatch, _Tracker())

    results = await preflight.check_github(config, explicit=True, work_items=block)

    failure = _named(results, "work items")
    assert failure.status is Status.FAIL
    assert failure.detail == "Tracker project PAY has no workflow state for In Progress."
    assert failure.fix == "In PAY's workflow settings add a state and map it to In Progress."
    assert not {"GitHub board", "board columns"} & {r.name for r in results}, "GitHub's board is not this symphony's"
    # GitHub stays the code host: its credential is still checked.
    assert _named(results, "GitHub token").status is Status.OK

    rendered = DoctorReport(results=results).render()  # what init and doctor print
    assert "[FAIL] work items" in rendered
    assert "Tracker project PAY has no workflow state for In Progress." in rendered
    assert "fix: In PAY's workflow settings add a state and map it to In Progress." in rendered

    (as_json,) = [c for c in doctor_checks(DoctorReport(results=results)) if c["id"] == "doctor.work_items"]
    assert as_json["message"] == failure.detail
    assert as_json["fix"] == failure.fix


@pytest.mark.asyncio
async def test_a_provider_that_cannot_be_reached_is_reported_not_raised(
    config: Any, github: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _Tracker(fault=ValueError("tracker secret detail"))
    block = _register(monkeypatch, tracker)

    results = await preflight.check_github(config, explicit=True, work_items=block)

    fault = _named(results, "work items")
    assert fault.status is Status.FAIL
    assert "tracker" in fault.detail
    assert fault.fix
    assert "tracker secret detail" not in str(results)


@pytest.mark.asyncio
async def test_a_provider_with_no_start_checks_is_a_warning(
    config: Any, github: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = _register(monkeypatch, object())

    results = await preflight.check_github(config, explicit=False, work_items=block)

    warning = _named(results, "work items")
    assert warning.status is Status.WARN
    assert "tracker" in warning.detail


@pytest.mark.asyncio
async def test_run_preflight_checks_each_symphony_with_its_own_provider(
    config: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    async def check_github(effective: Any, *, explicit: bool = False, work_items: Any = None) -> list[CheckResult]:
        seen.append(work_items)
        return []

    monkeypatch.setattr(preflight, "check_github", check_github)
    monkeypatch.setattr(preflight, "check_directories", lambda _config: [])
    monkeypatch.setattr(preflight, "check_port", lambda _config: CheckResult("port", Status.OK, "free"))
    monkeypatch.setattr(preflight, "run_checks", lambda _config: DoctorReport())
    monkeypatch.setattr(preflight, "check_stage_providers", AsyncMock(return_value=[]))
    monkeypatch.setattr(preflight, "check_harnesses", AsyncMock(return_value=[]))
    monkeypatch.setattr(preflight, "check_native_models", lambda _config: [])
    monkeypatch.setattr(preflight, "validate_start_time", lambda *_args: None)
    block = WorkItemsConfig(provider="github_projects")
    root = CoordinareConfiguration(
        global_config=config,
        symphonies=[
            SymphonyConfig(name="plain", github_project_number=2),
            SymphonyConfig(name="named", github_project_number=3, work_items=block),
        ],
    )

    await preflight.run_preflight(config, root)

    assert seen == [None, block]


# --- which states a provider may leave unmapped ---------------------------------------------------


def test_the_state_policy() -> None:
    assert set(REQUIRED_STATES) == {"TODO", "IN_PROGRESS", "DONE"}
    assert set(OPTIONAL_STATES) == {"IN_REVIEW", "BLOCKED"}


@pytest.mark.asyncio
async def test_the_in_memory_provider_passes_with_every_state_mapped() -> None:
    results = await InMemoryBoardProvider().preflight()

    assert [r.status for r in results] == [Status.OK]
    assert results[0].name == "work item states"


@pytest.mark.asyncio
async def test_unmapped_optional_states_warn_and_say_what_coordinare_does() -> None:
    provider = InMemoryBoardProvider(states={"To Do": "TODO", "Doing": "IN_PROGRESS", "Done": "DONE"})

    results = await provider.preflight()

    assert all(r.status is Status.WARN for r in results)
    review, blocked = sorted(results, key=lambda r: "Blocked" in r.detail)
    assert "In Review" in review.detail and "In Progress" in review.detail
    assert "Blocked" in blocked.detail
    assert all(r.fix for r in results)


@pytest.mark.asyncio
async def test_an_unmapped_required_state_fails_with_a_fix() -> None:
    provider = InMemoryBoardProvider(states={"To Do": "TODO", "Done": "DONE"})

    results = await provider.preflight()

    failure = next(r for r in results if r.status is Status.FAIL)
    assert "In Progress" in failure.detail
    assert failure.fix


def test_an_item_left_in_progress_through_review_is_not_reset_by_the_monitor() -> None:
    """What the In Review warning promises: the review goes on with the item in In Progress."""
    from coordinare.graph.nodes.monitor.board import _reconcile_board_mismatch
    from coordinare.graph.state import initial_state

    state = initial_state()
    state["phase"] = "monitoring_pr"

    reset = _reconcile_board_mismatch(state, "PAY-1", "IN_REVIEW", "IN_PROGRESS")

    assert reset is False
    assert state["phase"] == "monitoring_pr"


def test_an_item_left_in_progress_through_review_keeps_its_pin() -> None:
    from coordinare.services.reconciliation import reconcile_board_state

    state: dict[str, Any] = {"active_card": {"id": "PAY-1", "status": "IN_REVIEW"}, "active_sessions": {}}

    outcome = reconcile_board_state(state, {"IN_PROGRESS": ["PAY-1"]})

    assert outcome["action"] == "deferred"
    assert state["active_card"] == {"id": "PAY-1", "status": "IN_REVIEW"}
