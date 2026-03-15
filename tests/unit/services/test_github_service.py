from __future__ import annotations

from typing import Any

import aiohttp
import pytest

from coordinare.services.github import (
    FIND_PROJECT_QUERY,
    GET_PROJECT_FIELDS_QUERY,
    GitHubService,
    PermanentGitHubError,
    RateLimitedGitHubError,
    TransientGitHubError,
)


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

    def _build_client(self, token: str = ""):
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

    with pytest.raises(PermanentGitHubError, match="JSON object"):
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


class _RateLimitClient:
    """Fake client that raises aiohttp.ClientResponseError with 429."""

    def __init__(self, retry_after: str = "5") -> None:
        self._retry_after = retry_after

    def execute(self, query: object, variable_values: dict[str, Any]) -> Any:
        return self._execute_async(query, variable_values)

    async def _execute_async(self, query: object, variable_values: dict[str, Any]) -> Any:
        headers = {"Retry-After": self._retry_after}
        raise aiohttp.ClientResponseError(
            request_info=aiohttp.RequestInfo(
                url="https://api.github.com/graphql",
                method="POST",
                headers={},
                real_url="https://api.github.com/graphql",
            ),
            history=(),
            status=429,
            headers=headers,
        )


class _ServerErrorClient:
    """Fake client that raises aiohttp.ClientResponseError with 500."""

    def execute(self, query: object, variable_values: dict[str, Any]) -> Any:
        return self._execute_async(query, variable_values)

    async def _execute_async(self, query: object, variable_values: dict[str, Any]) -> Any:
        raise aiohttp.ClientResponseError(
            request_info=aiohttp.RequestInfo(
                url="https://api.github.com/graphql",
                method="POST",
                headers={},
                real_url="https://api.github.com/graphql",
            ),
            history=(),
            status=500,
        )


@pytest.mark.asyncio
async def test_execute_429_raises_rate_limited_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-005: HTTP 429 with Retry-After header -> RateLimitedGitHubError."""
    # Patch asyncio.sleep to avoid real delay
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("coordinare.services.github.asyncio.sleep", fake_sleep)

    service = GitHubService(token="tok", org="acme", project_number=1)
    service._client = _RateLimitClient(retry_after="5")
    service._last_token = "tok"  # prevent _execute from rebuilding the client

    with pytest.raises(RateLimitedGitHubError) as exc_info:
        await service._execute("query { __typename }", {})

    assert exc_info.value.retry_after == 5.0
    assert slept == [5.0]


@pytest.mark.asyncio
async def test_execute_500_raises_transient_error() -> None:
    """HTTP 500 -> TransientGitHubError (not PermanentGitHubError)."""
    service = GitHubService(token="tok", org="acme", project_number=1)
    service._client = _ServerErrorClient()
    service._last_token = "tok"  # prevent _execute from rebuilding the client

    with pytest.raises(TransientGitHubError):
        await service._execute("query { __typename }", {})


def test_query_constants_include_expected_operation_names() -> None:
    assert "FindProject" in FIND_PROJECT_QUERY
    assert "GetProjectFields" in GET_PROJECT_FIELDS_QUERY
