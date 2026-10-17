"""Complete pinned source inventory is mandatory for coverage discovery."""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from coordinare.services.github import GitHubError, GitHubService


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"truncated": True, "tree": []},
    {"tree": []},
    {"truncated": False, "tree": [{"type": "blob", "path": "../escape"}]},
    {"truncated": False, "tree": [{"type": "blob"}]},
    {"truncated": False, "tree": [{"type": "commit", "path": "submodule"}]},
])
async def test_incomplete_inventory_refuses(monkeypatch, payload):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    real_client = httpx.AsyncClient
    monkeypatch.setattr("coordinare.services.github.httpx.AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    service = GitHubService(org="org", project_number=1, token="token")
    service._current_token = AsyncMock(return_value="token")
    with pytest.raises(GitHubError):
        await service.get_repository_paths("org", "repo", ref="abc123")


@pytest.mark.asyncio
async def test_inventory_is_pinned_and_contains_all_blob_paths(monkeypatch):
    requests = []

    def respond(req):
        requests.append(req)
        return httpx.Response(200, json={"truncated": False, "tree": [
            {"type": "tree", "path": "arbitrary"},
            {"type": "blob", "path": "arbitrary/instructions.zog"},
            {"type": "blob", "path": "entry"},
        ]})

    transport = httpx.MockTransport(respond)
    real_client = httpx.AsyncClient
    monkeypatch.setattr("coordinare.services.github.httpx.AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    service = GitHubService(org="org", project_number=1, token="token")
    service._current_token = AsyncMock(return_value="token")
    assert await service.get_repository_paths("org", "repo", ref="abc123") == ["arbitrary/instructions.zog", "entry"]
    assert requests[0].url.path.endswith("/git/trees/abc123")
    assert requests[0].url.params["recursive"] == "1"


@pytest.mark.asyncio
async def test_inventory_honors_configured_entry_limit(monkeypatch):
    payload = {"truncated": False, "tree": [
        {"type": "blob", "path": "one"}, {"type": "blob", "path": "two"},
    ]}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    real_client = httpx.AsyncClient
    monkeypatch.setattr("coordinare.services.github.httpx.AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    service = GitHubService(org="org", project_number=1, token="token")
    service._current_token = AsyncMock(return_value="token")
    with pytest.raises(GitHubError, match="limit"):
        await service.get_repository_paths("org", "repo", ref="abc123", max_entries=1)
    assert await service.get_repository_paths("org", "repo", ref="abc123", max_entries=2) == ["one", "two"]
