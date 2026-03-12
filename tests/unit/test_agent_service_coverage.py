"""Tests for AgentService.dispatch_card with workspace_info (agent_service.py lines 33-36)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.services.agent_service import AgentService


class _WorkspaceInfo:
    def __init__(self, repo_url: str, branch: str, path: Path | None = None, github_token: str = "") -> None:
        self.repo_url = repo_url
        self.branch = branch
        self.path = path
        self.github_token = github_token


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


@pytest.mark.asyncio
async def test_dispatch_card_includes_issue_url() -> None:
    transport = _mock_transport()
    svc = AgentService(transport)
    await svc.dispatch_card({"title": "T", "id": "1", "issue_url": "https://github.com/o/r/issues/5"})
    assert transport.send.call_args[0][0].payload["issue_url"] == "https://github.com/o/r/issues/5"


@pytest.mark.asyncio
async def test_dispatch_card_includes_issue_number() -> None:
    transport = _mock_transport()
    svc = AgentService(transport)
    await svc.dispatch_card({"title": "T", "id": "1", "issue_number": 42})
    assert transport.send.call_args[0][0].payload["issue_number"] == 42


@pytest.mark.asyncio
async def test_dispatch_card_includes_clarifications() -> None:
    transport = _mock_transport()
    svc = AgentService(transport)
    clarifications = [{"questions": ["Q?"], "answer": "A"}]
    await svc.dispatch_card({"title": "T", "id": "1", "clarifications": clarifications})
    payload = transport.send.call_args[0][0].payload
    assert payload["clarifications"] == [{"questions": ["Q?"], "answer": "A"}]


@pytest.mark.asyncio
async def test_dispatch_card_includes_github_token() -> None:
    transport = _mock_transport()
    svc = AgentService(transport)
    info = _WorkspaceInfo(repo_url="https://github.com/o/r", branch="main", github_token="ghp_abc")
    await svc.dispatch_card({"title": "T", "id": "1"}, workspace_info=info)
    assert transport.send.call_args[0][0].payload["github_token"] == "ghp_abc"


def test_get_agent_logs_no_attribute_returns_empty() -> None:
    from unittest.mock import MagicMock
    transport = MagicMock(spec=[])  # no agent_logs attribute
    svc = AgentService(transport)
    assert svc.get_agent_logs() == []


def test_get_agent_logs_non_callable_returns_list() -> None:
    from collections import deque
    from unittest.mock import MagicMock
    transport = MagicMock()
    transport.agent_logs = deque(["line1", "line2"])
    svc = AgentService(transport)
    assert svc.get_agent_logs() == ["line1", "line2"]


def test_get_agent_logs_callable_is_called() -> None:
    from unittest.mock import MagicMock
    transport = MagicMock()
    transport.agent_logs = lambda: ["a", "b"]
    svc = AgentService(transport)
    assert svc.get_agent_logs() == ["a", "b"]
