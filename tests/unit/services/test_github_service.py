from __future__ import annotations

from typing import Any

import pytest

from coordinare.services.github import FIND_PROJECT_QUERY, GET_PROJECT_FIELDS_QUERY, GitHubService


class _FakeClient:
    def __init__(self, responses: list[dict[str, Any] | str], *, async_mode: bool) -> None:
        self._responses = responses
        self._async_mode = async_mode
        self.calls: list[tuple[object, dict[str, Any]]] = []

    async def _execute_async(self, query: object, variable_values: dict[str, Any]) -> Any:
        self.calls.append((query, variable_values))
        return self._responses.pop(0)

    def execute(self, query: object, variable_values: dict[str, Any]) -> Any:
        if self._async_mode:
            return self._execute_async(query, variable_values)
        self.calls.append((query, variable_values))
        return self._responses.pop(0)


class _TestGitHubService(GitHubService):
    def __init__(self, client: _FakeClient) -> None:
        super().__init__(token="tok", org="acme", project_number=1)
        self._fake_client = client

    def _build_client(self):  # type: ignore[override]
        return self._fake_client


@pytest.mark.asyncio
async def test_execute_handles_async_client() -> None:
    client = _FakeClient([{"data": {"ok": True}}], async_mode=True)
    service = _TestGitHubService(client)

    result = await service._execute("query X { __typename }", {"x": 1})

    assert result == {"data": {"ok": True}}
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_execute_handles_sync_client() -> None:
    client = _FakeClient([{"data": {"ok": True}}], async_mode=False)
    service = _TestGitHubService(client)

    result = await service._execute("query X { __typename }", {"x": 1})

    assert result == {"data": {"ok": True}}


@pytest.mark.asyncio
async def test_execute_rejects_non_object_response() -> None:
    client = _FakeClient(["bad-response"], async_mode=False)
    service = _TestGitHubService(client)

    with pytest.raises(ValueError, match="JSON object"):
        await service._execute("query X { __typename }", {"x": 1})


@pytest.mark.asyncio
async def test_initialize_populates_project_and_field_cache() -> None:
    client = _FakeClient(
        [
            {"organization": {"projectV2": {"id": "PVT_1", "title": "Board"}}},
            {
                "node": {
                    "fields": {
                        "nodes": [
                            {
                                "id": "FLD_1",
                                "name": "Status",
                                "options": [
                                    {"id": "OPT_1", "name": "Todo"},
                                    {"id": "OPT_2", "name": "In Progress"},
                                ],
                            }
                        ]
                    }
                }
            },
        ],
        async_mode=True,
    )
    service = _TestGitHubService(client)

    await service.initialize()

    assert service.project_id == "PVT_1"
    assert service.project_title == "Board"
    assert service.field_cache["status_field_id"] == "FLD_1"
    assert service.field_cache["status_option_ids"]["todo"] == "OPT_1"
    assert service.field_cache["status_option_ids"]["in progress"] == "OPT_2"


@pytest.mark.asyncio
async def test_initialize_raises_when_project_missing() -> None:
    client = _FakeClient([{"organization": {"projectV2": None}}], async_mode=True)
    service = _TestGitHubService(client)

    with pytest.raises(ValueError, match="Project not found"):
        await service.initialize()


@pytest.mark.asyncio
async def test_initialize_raises_when_fields_malformed() -> None:
    client = _FakeClient(
        [
            {"organization": {"projectV2": {"id": "PVT_1", "title": "Board"}}},
            {"node": {"fields": {"nodes": "bad"}}},
        ],
        async_mode=True,
    )
    service = _TestGitHubService(client)

    with pytest.raises(ValueError, match="malformed"):
        await service.initialize()


@pytest.mark.asyncio
async def test_initialize_raises_when_status_field_missing() -> None:
    client = _FakeClient(
        [
            {"organization": {"projectV2": {"id": "PVT_1", "title": "Board"}}},
            {"node": {"fields": {"nodes": [{"id": "FLD_X", "name": "Priority", "options": []}]}}},
        ],
        async_mode=True,
    )
    service = _TestGitHubService(client)

    with pytest.raises(ValueError, match="Status field not found"):
        await service.initialize()


@pytest.mark.asyncio
async def test_not_implemented_methods_raise() -> None:
    service = GitHubService(token="tok", org="acme", project_number=1)

    with pytest.raises(NotImplementedError):
        await service.poll_board()
    with pytest.raises(NotImplementedError):
        await service.get_issue_details("I_1")
    with pytest.raises(NotImplementedError):
        await service.move_card("ITEM_1", "TODO")


def test_query_constants_include_expected_operation_names() -> None:
    assert "FindProject" in FIND_PROJECT_QUERY
    assert "GetProjectFields" in GET_PROJECT_FIELDS_QUERY
