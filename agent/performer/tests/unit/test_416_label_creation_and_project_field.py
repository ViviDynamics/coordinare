"""Issue 416: the label seam and the column seam.

* ``add_labels`` can create a missing label instead of raising 404 forever,
* ``list_open_issues`` carries ``created_at`` so the scan can advance
  oldest-first within a pagination budget,
* ``set_project_item_field`` writes the Status option the curator could only
  record on the outcome before,
* ``list_project_item_ids`` gives the production board its dedup set.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from performer.github import (
    GitHubAPIError,
    list_open_issues,
    list_project_item_ids,
    set_project_item_field,
)


class _Resp:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload, self.status_code = payload, status
        self.is_success = 200 <= status < 300
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    """Serves a queued response per POST and records every request body."""

    def __init__(self, responses: list[_Resp]) -> None:
        self._responses, self.requests = list(responses), []

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *a: object) -> bool:
        return False

    async def post(self, url: str, headers: dict | None = None, json: dict | None = None, **kw: object) -> _Resp:
        self.requests.append(json or {})
        return self._responses.pop(0)


def _install(monkeypatch: pytest.MonkeyPatch, responses: list[_Resp]) -> _Client:
    client = _Client(responses)
    monkeypatch.setattr("performer.github.httpx.AsyncClient", lambda *a, **k: client)
    return client


def _issue_page(nodes: list[dict], *, has_next: bool = False, cursor: str | None = None) -> _Resp:
    return _Resp({"data": {"repository": {"issues": {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }}}})


# ---------------------------------------------------------------- create_missing


@pytest.mark.asyncio
async def test_a_missing_label_is_created_by_ensure_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any repo without the advocate's labels pre-created would otherwise 404
    on every cycle."""
    client = _install(monkeypatch, [
        _Resp({"data": {"repository": {"label": None, "id": "R_1"}}}),
        _Resp({"data": {"createLabel": {"label": {"id": "LA_new"}}}}),
    ])
    from performer.github import ensure_label

    await ensure_label("o", "r", "advocate-handled", "tok")
    assert client.requests[1]["variables"]["repositoryId"] == "R_1"
    assert client.requests[1]["variables"]["name"] == "advocate-handled"


@pytest.mark.asyncio
async def test_ensure_label_is_a_noop_when_it_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install(monkeypatch, [
        _Resp({"data": {"repository": {"label": {"id": "LA_1"}, "id": "R_1"}}}),
    ])
    from performer.github import ensure_label

    await ensure_label("o", "r", "advocate-handled", "tok")
    assert len(client.requests) == 1, "an existing label is not created again"


@pytest.mark.asyncio
async def test_a_created_label_that_still_fails_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [
        _Resp({"data": {"repository": {"label": None, "id": "R_1"}}}),
        _Resp({"errors": [{"message": "forbidden"}]}),
    ])
    from performer.github import ensure_label

    with pytest.raises(GitHubAPIError):
        await ensure_label("o", "r", "advocate-handled", "tok")


@pytest.mark.asyncio
async def test_poster_label_creates_then_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Poster's contract: apply, and when the label is missing create it
    and apply again.  A failure on either attempt propagates."""
    import performer.github as gh
    from performer.workflows.advocate.act import Poster

    calls: list[str] = []

    async def add_labels(owner, repo, labelable_id, names, token):
        calls.append("apply")
        if len(calls) == 1:
            raise GitHubAPIError(404, "label does not exist")

    async def ensure_label(owner, repo, name, token):
        calls.append(f"ensure:{name}")

    monkeypatch.setattr(gh, "add_labels", add_labels)
    monkeypatch.setattr(gh, "ensure_label", ensure_label)
    await Poster("o", "r", "tok").label("I_1", "advocate-handled")
    assert calls == ["apply", "ensure:advocate-handled", "apply"]


# ---------------------------------------------------------------- created_at


@pytest.mark.asyncio
async def test_issues_carry_their_created_at(monkeypatch: pytest.MonkeyPatch) -> None:
    node = {
        "id": "I_1", "number": 1, "title": "t", "body": "b",
        "url": "https://github.com/o/r/issues/1", "createdAt": "2024-01-01T00:00:00Z",
        "labels": {"nodes": []},
    }
    _install(monkeypatch, [_issue_page([node])])
    issues = await list_open_issues("o", "r", "tok")
    assert issues[0]["created_at"] == "2024-01-01T00:00:00Z"


# ---------------------------------------------------------------- project field


@pytest.mark.asyncio
async def test_the_status_option_is_set_on_the_item(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch, [
        _Resp({"data": {"node": {"field": {
            "id": "PVTF_status",
            "options": [{"id": "OPT_backlog", "name": "Backlog"}],
        }}}}),
        _Resp({"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_1"}}}}),
    ])
    await set_project_item_field("PVT_1", "PVTI_1", "Backlog", "tok")
    assert client.requests[0]["variables"]["fieldName"] == "Status"
    assert client.requests[1]["variables"]["itemId"] == "PVTI_1"
    assert client.requests[1]["variables"]["fieldId"] == "PVTF_status"
    assert client.requests[1]["variables"]["optionId"] == "OPT_backlog"


@pytest.mark.asyncio
async def test_an_unknown_column_option_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting a column the board does not have must not pass silently: the
    item would land in the automation's default, which dispatches."""
    _install(monkeypatch, [
        _Resp({"data": {"node": {"field": {
            "id": "PVTF_status",
            "options": [{"id": "OPT_todo", "name": "Todo"}],
        }}}}),
    ])
    with pytest.raises(GitHubAPIError):
        await set_project_item_field("PVT_1", "PVTI_1", "Backlog", "tok")


# ---------------------------------------------------------------- board ids


@pytest.mark.asyncio
async def test_project_item_content_ids_are_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [
        _Resp({"data": {"node": {"items": {
            "nodes": [{"content": {"__typename": "Issue", "id": "I_1"}},
                      {"content": None}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}}),
    ])
    ids = await list_project_item_ids("PVT_1", "tok")
    assert ids == ["I_1"]


@pytest.mark.asyncio
async def test_board_listing_requires_a_project_id() -> None:
    with pytest.raises(GitHubAPIError):
        await list_project_item_ids("   ", "tok")
