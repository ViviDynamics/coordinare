from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.graph.nodes.dispatch_card import dispatch_card
from coordinare.graph.state import initial_state
from coordinare.transport.base import TransportError
from coordinare.workspace import WorkspaceInfo, WorkspaceSetupError

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class _GitHub:
    def __init__(self) -> None:
        self.move_calls: list[tuple[str, str]] = []

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))


class _Agent:
    def __init__(self) -> None:
        self.last_workspace_info: WorkspaceInfo | None = None

    async def check_health(self):
        return {"status": "accepted"}

    async def dispatch_card(self, card_context, workspace_info=None):
        self.last_workspace_info = workspace_info
        return {"status": "accepted", "session_id": "s1"}


class _AgentUnhealthy:
    async def check_health(self):
        return {"status": "error", "reason": "Agent is down"}

    async def dispatch_card(self, card_context, workspace_info=None):
        raise AssertionError("Should not dispatch when unhealthy")


class _AgentUnreachable:
    async def check_health(self):
        raise ConnectionError("Agent unreachable")

    async def dispatch_card(self, card_context, workspace_info=None):
        raise AssertionError("Should not dispatch when unreachable")


class _AgentTransportError:
    async def check_health(self):
        return {"status": "healthy"}

    async def dispatch_card(self, card_context, workspace_info=None):
        raise TransportError("Agent transport permanently failed")


class _WorkspaceManager:
    """Stub WorkspaceManager for unit tests."""

    def __init__(
        self,
        *,
        workspace_path: Path = Path("/tmp/fake-ws/repo"),
        raise_on_prepare: WorkspaceSetupError | None = None,
    ) -> None:
        self._path = workspace_path
        self._raise = raise_on_prepare
        self.prepare_calls: list[dict] = []
        self.teardown_calls: list[Path] = []

    async def prepare(self, card: dict) -> WorkspaceInfo:
        self.prepare_calls.append(card)
        if self._raise is not None:
            raise self._raise
        return WorkspaceInfo(
            path=self._path,
            branch="coordinare/ITEM_1/test-card",
            repo_url="https://github.com/acme/repo.git",
        )

    async def teardown(self, path: Path) -> None:
        self.teardown_calls.append(path)


# ---------------------------------------------------------------------------
# Existing tests — all inject a no-op workspace_manager for backward compat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_moves_and_dispatches() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = _Agent()
    state["workspace_manager"] = _WorkspaceManager()

    result = await dispatch_card(state)

    assert result["phase"] == "monitoring_agent"
    assert result["agent_dispatch"]["status"] == "accepted"
    assert result["agent_health_status"] == "accepted"


@pytest.mark.asyncio
async def test_dispatch_card_blocks_on_unhealthy_agent() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = _AgentUnhealthy()
    state["workspace_manager"] = _WorkspaceManager()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert result["agent_health_status"] == "error"
    assert any("health check failed" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_dispatch_card_blocks_on_unreachable_agent() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = _AgentUnreachable()
    state["workspace_manager"] = _WorkspaceManager()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert result["agent_health_status"] == "unreachable"


@pytest.mark.asyncio
async def test_dispatch_card_blocks_on_transport_error() -> None:
    """Permanent TransportError moves card to BLOCKED (T019a)."""
    gh = _GitHub()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = gh
    state["agent_service"] = _AgentTransportError()
    state["workspace_manager"] = _WorkspaceManager()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert any("Permanent service failure" in q for q in result["open_questions"])
    # Should have moved card to IN_PROGRESS first, then to BLOCKED on error
    assert ("ITEM_1", "IN_PROGRESS") in gh.move_calls
    assert ("ITEM_1", "BLOCKED") in gh.move_calls


# ---------------------------------------------------------------------------
# New workspace tests (T012)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_enriches_payload_with_workspace_fields() -> None:
    """Agent receives WorkspaceInfo with correct fields when workspace_manager is set."""
    agent = _Agent()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = agent
    state["workspace_manager"] = _WorkspaceManager(
        workspace_path=Path("/tmp/fake-ws/repo")
    )

    result = await dispatch_card(state)

    assert result["phase"] == "monitoring_agent"
    assert agent.last_workspace_info is not None
    assert agent.last_workspace_info.branch == "coordinare/ITEM_1/test-card"
    assert agent.last_workspace_info.repo_url == "https://github.com/acme/repo.git"
    assert agent.last_workspace_info.path == Path("/tmp/fake-ws/repo")

    # workspace_path stored in state
    assert result["workspace_path"] == Path("/tmp/fake-ws/repo")
    assert result["workspace_branch"] == "coordinare/ITEM_1/test-card"


@pytest.mark.asyncio
async def test_dispatch_blocks_card_on_workspace_setup_error() -> None:
    """WorkspaceSetupError → phase='blocked'; workspace_path not set in state."""
    gh = _GitHub()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = gh
    state["agent_service"] = _Agent()
    state["workspace_manager"] = _WorkspaceManager(
        raise_on_prepare=WorkspaceSetupError("git clone failed: connection refused")
    )

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert result.get("workspace_path") is None
    assert any("git clone failed" in q for q in result["open_questions"])
    assert ("ITEM_1", "BLOCKED") in gh.move_calls


@pytest.mark.asyncio
async def test_dispatch_no_workspace_manager_still_dispatches() -> None:
    """When workspace_manager is None (backward compat), dispatch proceeds without workspace."""
    agent = _Agent()
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = agent
    # workspace_manager stays None (not set)

    result = await dispatch_card(state)

    assert result["phase"] == "monitoring_agent"
    assert agent.last_workspace_info is None
