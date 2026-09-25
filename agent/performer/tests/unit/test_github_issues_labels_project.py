"""Spec 173: the issue-listing, labelling and board capabilities the performer lacked.

Before this spec the performer could read pull requests and post comments, but it
could not enumerate issues at all (no `issues(` query existed in the module), so a
role built on inbound issues had nothing to read. It also could not label an issue
or add one to a project board.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from performer.github import (
    GitHubAPIError,
    add_item_to_project,
    add_labels,
    list_open_issues,
    list_project_item_ids,
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


def _node(n: int, *, labels: list[str] | None = None) -> dict:
    return {
        "id": f"I_{n}", "number": n, "title": f"t{n}", "body": f"b{n}",
        "url": f"https://github.com/o/r/issues/{n}",
        "labels": {"nodes": [{"name": x} for x in (labels or [])]},
    }


# ---------------------------------------------------------------- list_open_issues


@pytest.mark.asyncio
async def test_open_issues_are_returned_with_their_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_issue_page([_node(1, labels=["bug", "advocate-handled"])])])
    issues = await list_open_issues("o", "r", "tok")
    assert issues == [{
        "id": "I_1", "number": 1, "title": "t1", "body": "b1",
        "url": "https://github.com/o/r/issues/1", "created_at": "",
        "labels": ["bug", "advocate-handled"],
    }]


@pytest.mark.asyncio
async def test_a_second_page_is_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch, [
        _issue_page([_node(1)], has_next=True, cursor="c1"),
        _issue_page([_node(2)]),
    ])
    issues = await list_open_issues("o", "r", "tok")
    assert [i["number"] for i in issues] == [1, 2]
    assert client.requests[1]["variables"]["cursor"] == "c1", "the cursor must be carried forward"


@pytest.mark.asyncio
async def test_paging_stops_at_the_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repository with thousands of open issues must not be walked forever."""
    client = _install(monkeypatch, [_issue_page([_node(i)], has_next=True, cursor=f"c{i}") for i in range(1, 6)])
    issues = await list_open_issues("o", "r", "tok", max_pages=3)
    assert len(client.requests) == 3 and len(issues) == 3


@pytest.mark.asyncio
async def test_an_empty_repository_yields_no_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_issue_page([])])
    assert await list_open_issues("o", "r", "tok") == []


@pytest.mark.asyncio
async def test_a_graphql_error_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_Resp({"errors": [{"message": "bad"}]})])
    with pytest.raises(GitHubAPIError):
        await list_open_issues("o", "r", "tok")


@pytest.mark.asyncio
async def test_a_non_success_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_Resp({"message": "nope"}, status=502)])
    with pytest.raises(GitHubAPIError):
        await list_open_issues("o", "r", "tok")


@pytest.mark.asyncio
async def test_listing_requires_a_token() -> None:
    with pytest.raises(GitHubAPIError):
        await list_open_issues("o", "r", "   ")


# ---------------------------------------------------------------- add_labels


@pytest.mark.asyncio
async def test_labels_are_applied_by_node_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch, [
        _Resp({"data": {"repository": {"label": {"id": "LA_1"}}}}),
        _Resp({"data": {"addLabelsToLabelable": {"clientMutationId": None}}}),
    ])
    await add_labels("o", "r", "I_1", ["needs-human"], "tok")
    assert client.requests[1]["variables"]["labelIds"] == ["LA_1"]
    assert client.requests[1]["variables"]["labelableId"] == "I_1"


@pytest.mark.asyncio
async def test_an_unknown_label_raises_rather_than_passing_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_Resp({"data": {"repository": {"label": None}}})])
    with pytest.raises(GitHubAPIError):
        await add_labels("o", "r", "I_1", ["no-such-label"], "tok")


@pytest.mark.asyncio
async def test_labelling_nothing_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch, [])
    await add_labels("o", "r", "I_1", [], "tok")
    assert client.requests == []


@pytest.mark.asyncio
async def test_a_failed_label_mutation_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [
        _Resp({"data": {"repository": {"label": {"id": "LA_1"}}}}),
        _Resp({"errors": [{"message": "forbidden"}]}),
    ])
    with pytest.raises(GitHubAPIError):
        await add_labels("o", "r", "I_1", ["x"], "tok")


# ---------------------------------------------------------------- add_item_to_project


@pytest.mark.asyncio
async def test_an_issue_is_added_to_the_board(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch, [
        _Resp({"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_9"}}}}),
    ])
    assert await add_item_to_project("PVT_1", "I_1", "tok") == "PVTI_9"
    assert client.requests[0]["variables"] == {"projectId": "PVT_1", "contentId": "I_1"}


@pytest.mark.asyncio
async def test_adding_without_a_board_id_refuses_rather_than_no_opping(monkeypatch: pytest.MonkeyPatch) -> None:
    """The coordinare-side original returns None when the project id is unset, which
    reads as success. A run that cannot add must say so."""
    client = _install(monkeypatch, [])
    with pytest.raises(GitHubAPIError):
        await add_item_to_project("", "I_1", "tok")
    assert client.requests == [], "no request should be attempted without a board"


@pytest.mark.asyncio
async def test_a_board_add_returning_no_item_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_Resp({"data": {"addProjectV2ItemById": {"item": {}}}})])
    with pytest.raises(GitHubAPIError):
        await add_item_to_project("PVT_1", "I_1", "tok")


# ---------------------------------------------------------------- list_project_item_ids


def _items_page(nodes: list[dict]) -> _Resp:
    return _Resp({"data": {"node": {"items": {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }}}})


@pytest.mark.asyncio
async def test_board_item_ids_are_read_from_the_project_node(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_items_page([
        {"content": {"__typename": "Issue", "id": "I_7"}},
        {"content": {"__typename": "Issue", "id": "I_9"}},
        {"content": None},
    ])])
    assert await list_project_item_ids("PVT_1", "tok") == ["I_7", "I_9"]


@pytest.mark.asyncio
async def test_a_null_project_node_raises_instead_of_reading_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GraphQL answers `node: null` for an invalid or inaccessible project id
    with no errors body. Reading that as an empty board would let the
    fail-closed dedup proceed on exactly the boards it must not trust."""
    _install(monkeypatch, [_Resp({"data": {"node": None}})])
    with pytest.raises(GitHubAPIError):
        await list_project_item_ids("PVT_missing", "tok")


@pytest.mark.asyncio
async def test_a_node_without_an_items_connection_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_Resp({"data": {"node": {"id": "PVT_1"}}})])
    with pytest.raises(GitHubAPIError):
        await list_project_item_ids("PVT_1", "tok")
