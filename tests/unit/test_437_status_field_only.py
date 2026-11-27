"""The board column comes only from the project's Status field.

A GitHub project can have other single-select fields (a review state, a release
phase) with options named like a board column. Their value must not decide which
column a work item is in.
"""

from __future__ import annotations

from typing import Any

import pytest

from coordinare.services.github import GitHubService

STATUS = "PVTSSF_status"
OTHER = "PVTSSF_phase"


async def _poll(monkeypatch: pytest.MonkeyPatch, *field_values: dict[str, Any]) -> dict[str, Any]:
    service = GitHubService.__new__(GitHubService)
    service.project_id = "PVT_test"
    service.field_cache = {"status_field_id": STATUS, "status_option_ids": {}}
    page = {"node": {"items": {
        "nodes": [{
            "id": "PVTI_1",
            "fieldValues": {"nodes": list(field_values)},
            "content": {"id": "I_1", "number": 1, "title": "t", "body": "b", "state": "OPEN"},
        }],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }}}

    async def execute(self: Any, query: str, variables: Any = None) -> Any:
        return page

    monkeypatch.setattr(GitHubService, "_ensure_initialized", lambda self: None)
    monkeypatch.setattr(GitHubService, "_guarded_execute", execute)
    return await service.poll_board()


def _columns(board: dict[str, Any]) -> dict[str, list[str]]:
    return {column: items for column, items in board["snapshot"].items() if items}


def _value(name: str, field_id: str, updated_at: str = "2026-11-01T00:00:00Z") -> dict[str, Any]:
    return {"name": name, "updatedAt": updated_at, "field": {"id": field_id}}


@pytest.mark.asyncio
async def test_another_field_read_after_status_does_not_move_the_item(monkeypatch: pytest.MonkeyPatch) -> None:
    board = await _poll(monkeypatch, _value("Todo", STATUS), _value("Done", OTHER))
    assert _columns(board) == {"TODO": ["PVTI_1"]}


@pytest.mark.asyncio
async def test_another_field_read_before_status_does_not_move_the_item(monkeypatch: pytest.MonkeyPatch) -> None:
    board = await _poll(monkeypatch, _value("Done", OTHER), _value("In Progress", STATUS))
    assert _columns(board) == {"IN_PROGRESS": ["PVTI_1"]}


@pytest.mark.asyncio
async def test_a_status_with_no_column_is_not_filled_in_from_another_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Status is set to an option Coordinare has no column for, so the item is on no column."""
    board = await _poll(monkeypatch, _value("Triage", STATUS), _value("In Progress", OTHER))
    assert _columns(board) == {}


@pytest.mark.asyncio
async def test_no_status_value_means_no_column(monkeypatch: pytest.MonkeyPatch) -> None:
    board = await _poll(monkeypatch, _value("Todo", OTHER))
    assert _columns(board) == {}


@pytest.mark.asyncio
async def test_the_status_change_time_follows_the_status_field(monkeypatch: pytest.MonkeyPatch) -> None:
    board = await _poll(
        monkeypatch,
        _value("Done", STATUS, "2026-11-01T00:00:00Z"),
        _value("Todo", OTHER, "2026-11-26T00:00:00Z"),
    )
    assert _columns(board) == {"DONE": ["PVTI_1"]}
    assert board["status_updated_at"] == {"PVTI_1": "2026-11-01T00:00:00Z"}
