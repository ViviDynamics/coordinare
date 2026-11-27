"""#365 (b): Done work items are archived on the GitHub project after a set time in Done.

Archiving is a GitHub Projects behaviour, so it lives in the GitHub Projects provider and
its GitHub service, not in the shared provider protocol. It runs only on the board the
daemon builds for a symphony's own cycle, which hands it the symphony's durable provider
state; GitHub keeps there when each item was first seen in Done. An item is archived only when Coordinare can prove it has been in Done
for the configured time: from GitHub's own time for the status change, else from that
record. The record is persisted in the state snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from coordinare.config import WorkItemsConfig
from coordinare.services import board_provider as bp
from coordinare.services.board_provider import (
    DEFAULT_ARCHIVE_DONE_AFTER_DAYS,
    GitHubProjectsBoardProvider,
    done_archive_days,
)
from coordinare.services.github import (
    ARCHIVE_PROJECT_ITEM_MUTATION,
    POLL_BOARD_QUERY,
    GitHubService,
)
from coordinare.services.work_item_providers import build_board_provider
from coordinare.state_store import CURRENT_SCHEMA_VERSION, WorkflowSnapshot

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def _ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


class _FakeGitHub:
    """A GitHub service with a fixed board and a record of every archive call."""

    def __init__(self, board: dict[str, Any], *, fail: bool = False) -> None:
        self.board = board
        self.archived: list[str] = []
        self.fail = fail

    async def poll_board(self) -> dict[str, Any]:
        return self.board

    async def archive_project_item(self, item_id: str) -> None:
        if self.fail:
            raise RuntimeError("GitHub said no")
        self.archived.append(item_id)


def _board(done: list[str], times: dict[str, str], todo: list[str] | None = None,
           open_issues: tuple[str, ...] = (), assignees: dict[str, list[str]] | None = None) -> dict[str, Any]:
    items = [*done, *(todo or [])]
    return {
        "snapshot": {"BACKLOG": [], "TODO": list(todo or []), "BLOCKED": [], "IN_PROGRESS": [], "IN_REVIEW": [],
                     "DONE": list(done)},
        "issue_numbers": {item: index + 1 for index, item in enumerate(items)},
        "issue_states": {item: "OPEN" if item in open_issues else "CLOSED" for item in items},
        "item_assignees": assignees or {item: [] for item in items},
        "status_updated_at": dict(times),
    }


def _provider(github: Any, days: int = 14, clock: dict[str, str] | None = None,
              ownership: Any = None) -> GitHubProjectsBoardProvider:
    state: dict[str, Any] = {} if clock is None else {bp.DONE_FIRST_SEEN_KEY: clock}
    provider = GitHubProjectsBoardProvider(
        github, archive_done_after_days=days, provider_state=state, ownership=ownership,
    )
    provider._now = lambda: NOW
    if clock is not None:
        # The provider keeps working on the very dict the test holds.
        assert provider.provider_state[bp.DONE_FIRST_SEEN_KEY] is clock
    return provider


# --- config -----------------------------------------------------------------------------------


def test_the_default_is_fourteen_days() -> None:
    assert DEFAULT_ARCHIVE_DONE_AFTER_DAYS == 14
    assert WorkItemsConfig().archive_done_after_days == 14
    assert done_archive_days(None) == 14


def test_zero_turns_archiving_off_and_is_accepted() -> None:
    assert WorkItemsConfig(archive_done_after_days=0).archive_done_after_days == 0
    assert done_archive_days(WorkItemsConfig(archive_done_after_days=0)) == 0


@pytest.mark.parametrize("value", [-1, 3651, "soon", 1.5])
def test_a_value_out_of_range_or_not_a_whole_number_is_refused(value: object) -> None:
    with pytest.raises(ValidationError):
        WorkItemsConfig(archive_done_after_days=value)


# --- the sweep --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_item_in_done_past_the_time_is_archived_once() -> None:
    github = _FakeGitHub(_board(["PVTI_old"], {"PVTI_old": _ago(15)}))
    provider = _provider(github)
    board = await provider.poll_board()
    assert github.archived == ["PVTI_old"]
    assert board["snapshot"]["DONE"] == ["PVTI_old"]  # the read itself is unchanged


@pytest.mark.asyncio
async def test_an_item_in_done_for_less_than_the_time_is_kept() -> None:
    github = _FakeGitHub(_board(["PVTI_new"], {"PVTI_new": _ago(13.9)}))
    await _provider(github).poll_board()
    assert github.archived == []


@pytest.mark.asyncio
async def test_an_item_not_in_done_is_never_archived_however_old() -> None:
    github = _FakeGitHub(_board([], {"PVTI_todo": _ago(400)}, todo=["PVTI_todo"]))
    await _provider(github).poll_board()
    assert github.archived == []


@pytest.mark.asyncio
async def test_zero_days_archives_nothing() -> None:
    github = _FakeGitHub(_board(["PVTI_old"], {"PVTI_old": _ago(400)}))
    await _provider(github, days=0).poll_board()
    assert github.archived == []


@pytest.mark.asyncio
async def test_a_board_built_without_provider_state_never_archives() -> None:
    """Start checks, restart reconciliation and the fallback board read; only the cycle archives."""
    github = _FakeGitHub(_board(["PVTI_old"], {"PVTI_old": _ago(400)}))
    await GitHubProjectsBoardProvider(github).poll_board()
    await build_board_provider(None, github=github).poll_board()
    assert github.archived == []


@pytest.mark.asyncio
async def test_without_githubs_time_the_first_sighting_starts_the_clock() -> None:
    github = _FakeGitHub(_board(["PVTI_x"], {}))
    clock: dict[str, str] = {}
    await _provider(github, clock=clock).poll_board()
    assert clock == {"PVTI_x": NOW.isoformat()}
    assert github.archived == []


@pytest.mark.asyncio
async def test_without_githubs_time_the_recorded_sighting_proves_the_age() -> None:
    github = _FakeGitHub(_board(["PVTI_x"], {}))
    clock = {"PVTI_x": _ago(14)}
    await _provider(github, clock=clock).poll_board()
    assert github.archived == ["PVTI_x"]


@pytest.mark.asyncio
async def test_an_item_that_leaves_done_starts_again_when_it_returns() -> None:
    clock = {"PVTI_back": _ago(30), "PVTI_still": _ago(1)}
    github = _FakeGitHub(_board(["PVTI_still"], {}, todo=["PVTI_back"]))
    await _provider(github, clock=clock).poll_board()
    assert clock == {"PVTI_still": _ago(1)}


@pytest.mark.asyncio
async def test_a_failed_archive_keeps_the_read_and_is_tried_again() -> None:
    github = _FakeGitHub(_board(["PVTI_old"], {"PVTI_old": _ago(20)}), fail=True)
    board = await _provider(github).poll_board()
    assert board["snapshot"]["DONE"] == ["PVTI_old"]
    github.fail = False
    await _provider(github).poll_board()
    assert github.archived == ["PVTI_old"]


@pytest.mark.asyncio
async def test_a_service_that_cannot_archive_is_left_alone() -> None:
    class _NoArchive:
        async def poll_board(self) -> dict[str, Any]:
            return _board(["PVTI_old"], {"PVTI_old": _ago(400)})

    board = await _provider(_NoArchive()).poll_board()
    assert board["snapshot"]["DONE"] == ["PVTI_old"]


@pytest.mark.asyncio
async def test_an_item_whose_issue_is_still_open_is_kept() -> None:
    """Review: archived, it leaves the board, and an off-board dependency on an open issue never clears."""
    github = _FakeGitHub(_board(["PVTI_open"], {"PVTI_open": _ago(30)}, open_issues=("PVTI_open",)))
    await _provider(github).poll_board()
    assert github.archived == []


@pytest.mark.asyncio
async def test_only_items_this_coordinare_owns_are_archived() -> None:
    """Review: on a shared board, someone else's Done item is not ours to archive."""
    from coordinare.services.card_ownership import OwnershipPolicy

    board = _board(["PVTI_mine", "PVTI_theirs", "PVTI_nobody"],
                   {item: _ago(30) for item in ("PVTI_mine", "PVTI_theirs", "PVTI_nobody")},
                   assignees={"PVTI_mine": ["bot"], "PVTI_theirs": ["alice"], "PVTI_nobody": []})
    github = _FakeGitHub(board)
    await _provider(github, ownership=OwnershipPolicy(login="bot")).poll_board()
    assert github.archived == ["PVTI_mine"]


@pytest.mark.asyncio
async def test_a_failing_archive_stops_the_pass_for_this_poll() -> None:
    """Review: a refused mutation (no write scope) costs one call a poll, not one per overdue item."""
    done = [f"PVTI_{n}" for n in range(5)]
    github = _FakeGitHub(_board(done, {item: _ago(30) for item in done}), fail=True)
    calls: list[str] = []
    original = github.archive_project_item

    async def counting(item_id: str) -> None:
        calls.append(item_id)
        await original(item_id)

    github.archive_project_item = counting
    await _provider(github).poll_board()
    assert calls == ["PVTI_0"]


@pytest.mark.asyncio
async def test_a_partial_or_malformed_read_keeps_the_record() -> None:
    """Review: an item missing from one read (a truncated board) has not left Done; only another column says so."""
    clock = {"PVTI_gone_from_read": _ago(10), "PVTI_moved": _ago(10)}
    github = _FakeGitHub(_board([], {}, todo=["PVTI_moved"]))
    await _provider(github, clock=clock).poll_board()
    assert clock == {"PVTI_gone_from_read": _ago(10)}
    broken = _FakeGitHub({"snapshot": {"TODO": []}})
    await _provider(broken, clock=clock).poll_board()
    assert clock == {"PVTI_gone_from_read": _ago(10)}


@pytest.mark.asyncio
async def test_a_board_with_nothing_to_record_leaves_the_state_empty() -> None:
    """Review: an idle daemon must not write a snapshot because an empty record appeared."""
    state: dict[str, Any] = {}
    github = _FakeGitHub(_board([], {}))
    await _provider(github, clock=None).poll_board()
    assert state == {}
    state = {}
    provider = GitHubProjectsBoardProvider(github, archive_done_after_days=14, provider_state=state)
    await provider.poll_board()
    assert state == {}


@pytest.mark.asyncio
async def test_one_poll_archives_a_bounded_number() -> None:
    done = [f"PVTI_{n}" for n in range(bp.MAX_ARCHIVES_PER_POLL + 5)]
    github = _FakeGitHub(_board(done, {item: _ago(30) for item in done}))
    await _provider(github).poll_board()
    assert len(github.archived) == bp.MAX_ARCHIVES_PER_POLL


@pytest.mark.asyncio
async def test_each_archive_is_logged_with_how_its_age_was_proven(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(bp.logger, "info", lambda event, **kw: events.append((event, kw)))
    github = _FakeGitHub(_board(["PVTI_a", "PVTI_b"], {"PVTI_a": _ago(20)}))
    await _provider(github, clock={"PVTI_b": _ago(15)}).poll_board()
    archived = [kw for event, kw in events if event == "board.done_item_archived"]
    assert [(kw["card_id"], kw["since_source"]) for kw in archived] == [
        ("PVTI_a", "github_status_time"), ("PVTI_b", "first_seen_in_done"),
    ]
    assert all(kw["after_days"] == 14 and kw["days_in_done"] >= 14 for kw in archived)


def test_the_factory_hands_the_configured_days_and_the_state_to_the_provider() -> None:
    state: dict[str, Any] = {}
    provider = build_board_provider(WorkItemsConfig(archive_done_after_days=30), github=object(), provider_state=state)
    assert isinstance(provider, GitHubProjectsBoardProvider)
    assert provider.archive_done_after_days == 30
    assert provider.provider_state is state


def test_a_builder_that_takes_no_state_is_still_built() -> None:
    """A provider registered with the two-argument builder (as #399's tests do) keeps working."""
    from coordinare.services import work_item_providers as wip

    built = object()
    original = dict(wip.WORK_ITEM_PROVIDERS)
    try:
        wip.WORK_ITEM_PROVIDERS["github_projects"] = lambda _block, _github: built
        assert build_board_provider(None, github=object(), provider_state={}) is built
    finally:
        wip.WORK_ITEM_PROVIDERS.clear()
        wip.WORK_ITEM_PROVIDERS.update(original)


# --- the GitHub service ------------------------------------------------------------------------


def test_the_poll_reads_when_the_status_last_changed_and_the_mutation_archives() -> None:
    assert "updatedAt" in POLL_BOARD_QUERY
    assert "\n              state\n" in POLL_BOARD_QUERY  # the issue's own state, beside its number
    assert "archiveProjectV2Item" in ARCHIVE_PROJECT_ITEM_MUTATION


@pytest.mark.asyncio
async def test_the_real_poll_reports_the_status_change_time(monkeypatch: pytest.MonkeyPatch) -> None:
    service = GitHubService.__new__(GitHubService)
    service.project_id = "PVT_test"
    service.field_cache = {"status_field_id": "status"}
    page = {"node": {"items": {
        "nodes": [
            # Copilot review: another single-select field with a "Done" option is read after the
            # Status field. Its time must not stand in for the status change.
            {"id": "PVTI_d", "fieldValues": {"nodes": [
                {"name": "Done", "updatedAt": "2026-11-01T00:00:00Z", "field": {"id": "status"}},
                {"name": "Done", "updatedAt": "2026-11-26T00:00:00Z", "field": {"id": "PVTSSF_review"}},
            ]},
             "content": {"id": "I_1", "number": 1, "title": "t", "body": "b", "state": "CLOSED"}},
            {"id": "PVTI_t", "fieldValues": {"nodes": [{"name": "Todo"}]},
             "content": {"id": "I_2", "number": 2, "title": "t", "body": "b", "state": "OPEN"}},
        ],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }}}

    async def execute(self: Any, query: str, variables: Any = None) -> Any:
        return page

    monkeypatch.setattr(GitHubService, "_ensure_initialized", lambda self: None)
    monkeypatch.setattr(GitHubService, "_guarded_execute", execute)
    board = await service.poll_board()
    assert board["status_updated_at"] == {"PVTI_d": "2026-11-01T00:00:00Z"}
    assert board["issue_states"] == {"PVTI_d": "CLOSED", "PVTI_t": "OPEN"}


@pytest.mark.asyncio
async def test_a_done_read_from_another_field_carries_no_status_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review: Status says In Progress, set long ago; a later field says Done. No Done age is claimed."""
    service = GitHubService.__new__(GitHubService)
    service.project_id = "PVT_test"
    service.field_cache = {"status_field_id": "status"}
    page = {"node": {"items": {
        "nodes": [{"id": "PVTI_x", "fieldValues": {"nodes": [
            {"name": "In Progress", "updatedAt": "2026-10-01T00:00:00Z", "field": {"id": "status"}},
            {"name": "Done", "updatedAt": "2026-11-26T00:00:00Z", "field": {"id": "PVTSSF_review"}},
        ]}, "content": {"id": "I_1", "number": 1, "title": "t", "body": "b", "state": "CLOSED"}}],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }}}

    async def execute(self: Any, query: str, variables: Any = None) -> Any:
        return page

    monkeypatch.setattr(GitHubService, "_ensure_initialized", lambda self: None)
    monkeypatch.setattr(GitHubService, "_guarded_execute", execute)
    board = await service.poll_board()
    assert board["status_updated_at"] == {}


@pytest.mark.asyncio
async def test_a_time_not_known_to_be_the_status_fields_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    service = GitHubService.__new__(GitHubService)
    service.project_id = "PVT_test"
    service.field_cache = {"status_field_id": "status"}
    page = {"node": {"items": {
        "nodes": [{"id": "PVTI_d", "fieldValues": {"nodes": [{"name": "Done", "updatedAt": "2026-11-01T00:00:00Z"}]},
                   "content": {"id": "I_1", "number": 1, "title": "t", "body": "b", "state": "CLOSED"}}],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }}}

    async def execute(self: Any, query: str, variables: Any = None) -> Any:
        return page

    monkeypatch.setattr(GitHubService, "_ensure_initialized", lambda self: None)
    monkeypatch.setattr(GitHubService, "_guarded_execute", execute)
    board = await service.poll_board()
    assert board["snapshot"]["DONE"] == ["PVTI_d"]
    assert board["status_updated_at"] == {}  # the first-seen record proves the age instead


@pytest.mark.asyncio
async def test_the_service_archives_an_item_in_its_own_project(monkeypatch: pytest.MonkeyPatch) -> None:
    service = GitHubService.__new__(GitHubService)
    service.project_id = "PVT_test"
    calls: list[tuple[str, Any]] = []

    async def execute(self: Any, query: str, variables: Any = None) -> Any:
        calls.append((query, variables))
        return {"archiveProjectV2Item": {"item": {"id": "PVTI_d"}}}

    monkeypatch.setattr(GitHubService, "_guarded_execute", execute)
    await service.archive_project_item("PVTI_d")
    assert calls == [(ARCHIVE_PROJECT_ITEM_MUTATION, {"projectId": "PVT_test", "itemId": "PVTI_d"})]


# --- the record of first sightings is persisted --------------------------------------------------


def test_the_snapshot_carries_provider_state_and_an_older_one_loads_without_it() -> None:
    assert "work_item_provider_state" in WorkflowSnapshot.model_fields
    old = WorkflowSnapshot.model_validate(
        {"schema_version": CURRENT_SCHEMA_VERSION - 1, "phase": "idle", "snapshot_at": NOW.isoformat()},
    )
    assert old.work_item_provider_state == {}
    state = {"alpha": {bp.DONE_FIRST_SEEN_KEY: {"PVTI_x": NOW.isoformat()}}}
    now = WorkflowSnapshot(phase="idle", snapshot_at=NOW, work_item_provider_state=state)
    assert WorkflowSnapshot.model_validate_json(now.model_dump_json()).work_item_provider_state == state


# --- the daemon hands the cycle's board its state, and persists it -------------------------------


def _daemon() -> Any:
    from unittest.mock import AsyncMock

    from coordinare.daemon import CoordinareDaemon
    from tests.unit.env_mount_stub import mount_config

    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state["coordinare_config"] = mount_config()
    return daemon


def test_each_symphony_keeps_one_state_dict_across_cycles() -> None:
    from coordinare.daemon import _provider_state_for

    state: dict[str, Any] = {}
    alpha = _provider_state_for(state, "alpha")
    alpha[bp.DONE_FIRST_SEEN_KEY] = {"PVTI_x": NOW.isoformat()}
    assert _provider_state_for(state, "alpha") is alpha
    assert _provider_state_for(state, "beta") == {}
    assert state["work_item_provider_state"] == {"alpha": alpha, "beta": {}}


def test_the_symphony_cycle_board_is_the_one_given_state() -> None:
    import inspect

    from coordinare import daemon as daemon_module

    source = inspect.getsource(daemon_module)
    assert source.count("provider_state=_provider_state_for(self._state, symphony_name)") == 1
    assert source.count("ownership=ownership_policy(") == 1
    # The startup and restore boards are built without it, so they never archive.
    assert source.count("build_board_provider(None, github=prev_github)") == 1


def test_provider_state_survives_a_restart() -> None:
    state = {"alpha": {bp.DONE_FIRST_SEEN_KEY: {"PVTI_x": NOW.isoformat()}}}
    first = _daemon()
    first._state["work_item_provider_state"] = state
    snapshot = first._build_snapshot()
    assert snapshot.work_item_provider_state == state
    second = _daemon()
    held = _provider_state_root_of(second)
    second._restore_from_snapshot(WorkflowSnapshot.model_validate_json(snapshot.model_dump_json()))
    assert second._state["work_item_provider_state"] is held
    assert held == state


def _provider_state_root_of(daemon: Any) -> dict[str, Any]:
    from coordinare.daemon import _provider_state_root

    return _provider_state_root(daemon._state)


def test_a_new_record_moves_the_save_signature_so_it_is_written() -> None:
    daemon = _daemon()
    before = daemon._lifecycle_signature()
    from coordinare.daemon import _provider_state_for

    _provider_state_for(daemon._state, "alpha")
    assert daemon._lifecycle_signature() == before, "an empty state is not a change"
    _provider_state_for(daemon._state, "alpha")[bp.DONE_FIRST_SEEN_KEY] = {"PVTI_x": NOW.isoformat()}
    assert daemon._lifecycle_signature() != before


def test_restore_fills_the_dicts_a_provider_already_holds() -> None:
    from coordinare.daemon import _provider_state_for

    daemon = _daemon()
    held = _provider_state_for(daemon._state, "alpha")
    state = {"alpha": {bp.DONE_FIRST_SEEN_KEY: {"PVTI_x": NOW.isoformat()}}}
    daemon._restore_from_snapshot(WorkflowSnapshot(phase="idle", snapshot_at=NOW, work_item_provider_state=state))
    assert _provider_state_for(daemon._state, "alpha") is held
    assert held == state["alpha"]


# --- the board says so ------------------------------------------------------------------------


def test_the_board_reads_each_symphonys_archive_days() -> None:
    from types import SimpleNamespace

    from coordinare.services.board_provider import symphony_done_archive_days

    assert symphony_done_archive_days(SimpleNamespace(work_items=None)) == 14
    assert symphony_done_archive_days(SimpleNamespace(work_items=WorkItemsConfig(archive_done_after_days=0))) == 0
    assert symphony_done_archive_days(SimpleNamespace(work_items=SimpleNamespace(provider="jira"))) is None
    # Review: a disabled symphony is not polled, so nothing of it is archived.
    assert symphony_done_archive_days(SimpleNamespace(work_items=None, enabled=False)) is None


def test_the_dashboard_snapshot_carries_the_days() -> None:
    import inspect

    from coordinare.dashboard import store

    assert '"done_archive_days": symphony_done_archive_days(sym_cfg)' in inspect.getsource(store)


def test_a_symphony_written_back_keeps_its_block_as_set() -> None:
    from coordinare.config import SymphonyConfig
    from coordinare.services.config_documents import symphony_documents

    unset = SymphonyConfig(name="a", github_project_number=1, work_items=WorkItemsConfig())
    chosen = SymphonyConfig(name="b", github_project_number=2, work_items=WorkItemsConfig(archive_done_after_days=0))
    docs = {doc["name"]: doc for doc in symphony_documents({"a": unset, "b": chosen}, {})}
    assert docs["a"]["work_items"] == {}
    assert docs["b"]["work_items"] == {"archive_done_after_days": 0}
