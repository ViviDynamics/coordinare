"""The priority sort reaches work items listed from GitHub Projects.

``check_board`` sorts To Do candidates by the field ``config.priority.field_name`` names,
reading ``WorkItem.fields`` (393). The GitHub Projects listing never filled those fields:
``GitHubService.poll_board`` read every item's single-select values only to find its
column, and dropped the rest, so the sort saw no priority on any item and kept board
order. These tests run the real ``poll_board`` over a canned GraphQL page, through the
production ``GitHubProjectsBoardProvider``, into the sort.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog

from coordinare.config import ProjectConfiguration
from coordinare.graph.nodes.check_board import _select_eligible_todo
from coordinare.services.board_provider import GitHubProjectsBoardProvider
from coordinare.services.card_ownership import ownership_policy
from coordinare.services.github import POLL_BOARD_QUERY, GitHubService
from coordinare.services.work_items import work_items_of

STATUS = "PVTSSF_status"
PRIORITY = "PVTSSF_priority"


def _value(name: str, field_id: str, field_name: str) -> dict[str, Any]:
    return {"name": name, "updatedAt": "2026-11-01T00:00:00Z", "field": {"id": field_id, "name": field_name}}


def _item(n: int, priority: str | None, *, more_values: bool = False, extra: list[Any] | None = None) -> dict[str, Any]:
    values: list[Any] = [_value("Todo", STATUS, "Status")]
    if priority is not None:
        values.append(_value(priority, PRIORITY, "Priority"))
    values.extend(extra or [])
    field_values: dict[str, Any] = {"nodes": values}
    if more_values:
        field_values["pageInfo"] = {"hasNextPage": True}
    return {
        "id": f"PVTI_{n}",
        "fieldValues": field_values,
        "content": {"id": f"I_{n}", "number": n, "title": f"Item {n}", "body": "", "state": "OPEN"},
    }


class _Service:
    """A real GitHubService whose GraphQL answers with one canned page."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, items: list[dict[str, Any]]) -> None:
        self.calls: list[str] = []
        self.items = items
        service = GitHubService.__new__(GitHubService)
        service.project_id = "PVT_test"
        service.field_cache = {"status_field_id": STATUS, "status_option_ids": {}}
        self.service = service
        calls = self.calls

        async def execute(_self: Any, query: str, variables: Any = None) -> Any:
            calls.append(query)
            return {"node": {"items": {"nodes": self.items, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}

        monkeypatch.setattr(GitHubService, "_ensure_initialized", lambda self: None)
        monkeypatch.setattr(GitHubService, "_guarded_execute", execute)

    async def listing(self) -> dict[str, Any]:
        board: dict[str, Any] = await GitHubProjectsBoardProvider(self.service).poll_board()
        return board


def _config(**priority: Any) -> ProjectConfiguration:
    return ProjectConfiguration(
        github_org="acme", project_name="demo", human_reviewers=["reviewer1"], github_token="fake-token",
        **({"priority": priority} if priority else {}),
    )


def _sorted(listing: dict[str, Any], config: ProjectConfiguration) -> list[str]:
    todo = list(listing["snapshot"]["TODO"])
    state: Any = {"config": config}
    return _select_eligible_todo(state, listing, todo, ownership_policy(config), {})


#: Board order: P2, no priority, P0, P1.
BOARD = [(1, "P2"), (2, None), (3, "P0"), (4, "P1")]


@pytest.mark.asyncio
async def test_work_items_from_github_start_in_priority_order(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = await _Service(monkeypatch, [_item(n, p) for n, p in BOARD]).listing()

    eligible = _sorted(listing, _config(field_name="Priority", priority_order=["P0", "P1", "P2"]))

    assert eligible == ["PVTI_3", "PVTI_4", "PVTI_1", "PVTI_2"]


@pytest.mark.asyncio
async def test_with_no_priority_order_values_sort_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = await _Service(monkeypatch, [_item(n, p) for n, p in BOARD]).listing()

    assert _sorted(listing, _config(field_name="Priority")) == ["PVTI_3", "PVTI_4", "PVTI_1", "PVTI_2"]


@pytest.mark.asyncio
async def test_each_single_select_value_reaches_the_record_by_field_name(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = await _Service(monkeypatch, [_item(3, "P0"), _item(2, None)]).listing()

    records = work_items_of(listing)
    assert records["PVTI_3"].fields == {"Status": "Todo", "Priority": "P0"}
    assert records["PVTI_2"].fields == {"Status": "Todo"}


@pytest.mark.asyncio
async def test_with_no_priority_configured_board_order_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = await _Service(monkeypatch, [_item(n, p) for n, p in BOARD]).listing()

    assert _sorted(listing, _config()) == ["PVTI_1", "PVTI_2", "PVTI_3", "PVTI_4"]


@pytest.mark.asyncio
async def test_a_project_without_the_priority_field_keeps_board_order(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = await _Service(monkeypatch, [_item(n, None) for n, _ in BOARD]).listing()

    eligible = _sorted(listing, _config(field_name="Priority", priority_order=["P0", "P1", "P2"]))

    assert eligible == ["PVTI_1", "PVTI_2", "PVTI_3", "PVTI_4"]


@pytest.mark.asyncio
async def test_the_values_come_from_the_listing_query_with_no_query_per_item(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service(monkeypatch, [_item(n, p) for n, p in BOARD])
    await service.listing()

    assert service.calls == [POLL_BOARD_QUERY]
    field_fragment = POLL_BOARD_QUERY.split("fieldValues(first: 20)", 1)[1].split("content {", 1)[0]
    assert "name" in field_fragment.split("field {", 1)[1]
    assert "fieldValueByName" not in POLL_BOARD_QUERY


@pytest.mark.asyncio
async def test_values_that_name_no_field_are_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Older fixtures build values with no field, and a non single-select value comes back
    as an empty object because the query has no fragment for it. Neither is a field value."""
    extra = [{"name": "P0"}, {"name": "P0", "field": {"id": PRIORITY}}, {}, "junk"]
    listing = await _Service(monkeypatch, [_item(1, None, extra=extra)]).listing()

    assert work_items_of(listing)["PVTI_1"].fields == {"Status": "Todo"}


@pytest.mark.asyncio
async def test_an_item_with_more_values_than_the_page_is_warned_about_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value past the 20 read is not seen, so a priority there reads as unset. That is said
    once per item, not on every poll."""
    service = _Service(monkeypatch, [_item(1, "P1", more_values=True), _item(2, "P0")])
    with structlog.testing.capture_logs() as logs:
        first = await service.listing()
        await service.listing()

    truncated = [e for e in logs if e["event"] == "poll_board.field_values_truncated"]
    assert [e["item_id"] for e in truncated] == ["PVTI_1"]
    assert work_items_of(first)["PVTI_1"].fields == {"Status": "Todo", "Priority": "P1"}
    assert "pageInfo" in POLL_BOARD_QUERY.split("fieldValues(first: 20)", 1)[1].split("content {", 1)[0]
