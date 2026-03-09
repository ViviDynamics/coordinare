"""Tests for AgentService.dispatch_card with workspace_info (agent_service.py lines 33-36)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.services.agent_service import AgentService


class _WorkspaceInfo:
    def __init__(self, repo_url: str, branch: str, path: Path | None = None) -> None:
        self.repo_url = repo_url
        self.branch = branch
        self.path = path


def _mock_transport(status: str = "accepted") -> MagicMock:
    transport = MagicMock()
    response = MagicMock()
    response.model_dump.return_value = {"status": status, "session_id": "s1"}
    transport.send = AsyncMock(return_value=response)
    return transport


@pytest.mark.asyncio
async def test_dispatch_card_includes_workspace_repo_url() -> None:
    transport = _mock_transport()
    svc = AgentService(transport)
    info = _WorkspaceInfo(repo_url="https://github.com/org/repo", branch="feat/x")
    await svc.dispatch_card({"title": "T", "id": "1"}, workspace_info=info)
    sent_msg = transport.send.call_args[0][0]
    assert sent_msg.payload["repo_url"] == "https://github.com/org/repo"
    assert sent_msg.payload["branch"] == "feat/x"


@pytest.mark.asyncio
async def test_dispatch_card_includes_workspace_path_when_set() -> None:
    transport = _mock_transport()
    svc = AgentService(transport)
    info = _WorkspaceInfo(
        repo_url="https://github.com/org/repo",
        branch="feat/x",
        path=Path("/tmp/workspace"),
    )
    await svc.dispatch_card({"title": "T", "id": "1"}, workspace_info=info)
    sent_msg = transport.send.call_args[0][0]
    assert sent_msg.payload["workspace_path"] == "/tmp/workspace"


@pytest.mark.asyncio
async def test_dispatch_card_no_workspace_path_when_path_is_none() -> None:
    transport = _mock_transport()
    svc = AgentService(transport)
    info = _WorkspaceInfo(
        repo_url="https://github.com/org/repo",
        branch="feat/x",
        path=None,
    )
    await svc.dispatch_card({"title": "T", "id": "1"}, workspace_info=info)
    sent_msg = transport.send.call_args[0][0]
    assert "workspace_path" not in sent_msg.payload


@pytest.mark.asyncio
async def test_dispatch_card_without_workspace_info() -> None:
    transport = _mock_transport()
    svc = AgentService(transport)
    await svc.dispatch_card({"title": "T", "id": "1"})
    sent_msg = transport.send.call_args[0][0]
    assert "repo_url" not in sent_msg.payload
    assert "branch" not in sent_msg.payload
    assert "workspace_path" not in sent_msg.payload
