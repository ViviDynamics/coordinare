from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.graph.nodes.monitor_agent import monitor_agent
from coordinare.graph.state import initial_state
from coordinare.services.github import PermanentGitHubError
from coordinare.transport.base import TransportError

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class _GitHub:
    """Tracks move_card calls."""

    def __init__(self) -> None:
        self.move_calls: list[tuple[str, str]] = []

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))


class _Agent:
    """Returns a configurable check_status response."""

    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str) -> dict:
        _ = session_id
        return self._response


class _AgentTransportError:
    """Raises TransportError from check_status."""

    async def check_status(self, session_id: str) -> dict:
        raise TransportError("SSH tunnel collapsed")


class _AgentPermanentGitHubError:
    """Raises PermanentGitHubError from check_status."""

    async def check_status(self, session_id: str) -> dict:
        raise PermanentGitHubError("Token revoked")


class _AgentUnexpectedError:
    """Raises an unexpected RuntimeError from check_status (not a known transport error)."""

    async def check_status(self, session_id: str) -> dict:
        raise RuntimeError("unexpected internal error")


class _GitHubMoveCardFails:
    """move_card always raises so we can verify the node handles it gracefully."""

    def __init__(self) -> None:
        self.move_calls: list[tuple[str, str]] = []

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))
        raise RuntimeError("GitHub API unavailable")


class _WorkspaceManager:
    """Stub WorkspaceManager that records teardown calls."""

    def __init__(self) -> None:
        self.teardown_calls: list[Path] = []

    async def teardown(self, path: Path) -> None:
        self.teardown_calls.append(path)


# ---------------------------------------------------------------------------
# 1. Normal status "working" → phase stays "monitoring_agent"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_agent_stays_monitoring_when_working() -> None:
    state = initial_state()
    state["agent_service"] = _Agent({"status": "working"})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "monitoring_agent"


# ---------------------------------------------------------------------------
# 2. Status "pr_opened" → phase="monitoring_pr", card updated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_agent_transitions_to_monitoring_pr_on_pr_opened() -> None:
    state = initial_state()
    state["agent_service"] = _Agent({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_node_id": "PR_NODE_1",
    })
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "monitoring_pr"
    assert result["current_card"]["pr_url"] == "https://github.com/org/repo/pull/1"
    assert result["current_card"]["pr_node_id"] == "PR_NODE_1"
    assert result["current_card"]["status"] == "IN_REVIEW"
    assert result["current_card"]["previous_status"] == "IN_PROGRESS"


# ---------------------------------------------------------------------------
# 3. Status "blocked" → phase="blocked"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_agent_marks_blocked_with_questions() -> None:
    state = initial_state()
    state["agent_service"] = _Agent({"status": "blocked", "questions": ["Need answer"]})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["Need answer"]


@pytest.mark.asyncio
async def test_monitor_agent_marks_blocked_on_error_status() -> None:
    state = initial_state()
    state["agent_service"] = _Agent({"status": "error", "questions": ["Crash dump"]})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["Crash dump"]


# ---------------------------------------------------------------------------
# 4. TransportError / PermanentGitHubError → card moved to BLOCKED (T019a)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_error_moves_card_to_blocked() -> None:
    """TransportError from check_status moves card to BLOCKED and sets phase='blocked' (T019a)."""
    gh = _GitHub()
    state = initial_state()
    state["agent_service"] = _AgentTransportError()
    state["current_card"] = {"id": "ITEM_1"}
    state["github_service"] = gh
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert ("ITEM_1", "BLOCKED") in gh.move_calls
    assert any("Permanent service failure" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_permanent_github_error_moves_card_to_blocked() -> None:
    """PermanentGitHubError from check_status moves card to BLOCKED and sets phase='blocked' (T019a)."""
    gh = _GitHub()
    state = initial_state()
    state["agent_service"] = _AgentPermanentGitHubError()
    state["current_card"] = {"id": "ITEM_2"}
    state["github_service"] = gh
    state["agent_dispatch"] = {"session_id": "s2"}

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert ("ITEM_2", "BLOCKED") in gh.move_calls
    assert any("Token revoked" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_transport_error_without_github_service_still_blocks() -> None:
    """When github_service is None, the node still sets phase='blocked' without crashing."""
    state = initial_state()
    state["agent_service"] = _AgentTransportError()
    state["current_card"] = {"id": "ITEM_1"}
    # No github_service set — stays None
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert any("Permanent service failure" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_transport_error_survives_move_card_failure() -> None:
    """If move_card itself fails, the node still sets phase='blocked' gracefully."""
    gh = _GitHubMoveCardFails()
    state = initial_state()
    state["agent_service"] = _AgentTransportError()
    state["current_card"] = {"id": "ITEM_1"}
    state["github_service"] = gh
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    # move_card was attempted even though it failed
    assert ("ITEM_1", "BLOCKED") in gh.move_calls
    assert any("Permanent service failure" in q for q in result["open_questions"])


# ---------------------------------------------------------------------------
# 5. No agent or card → phase="idle"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_agent_idle_when_no_agent() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1"}

    result = await monitor_agent(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_monitor_agent_idle_when_no_card() -> None:
    state = initial_state()
    state["agent_service"] = _Agent({"status": "working"})

    result = await monitor_agent(state)

    assert result["phase"] == "idle"


# ---------------------------------------------------------------------------
# 6. Workspace teardown tests (T018)
# ---------------------------------------------------------------------------

_FAKE_WS = Path("/tmp/fake-ws")


@pytest.mark.asyncio
async def test_monitor_agent_teardown_called_on_pr_opened() -> None:
    """workspace teardown is called and workspace_path cleared when PR opens."""
    wm = _WorkspaceManager()
    state = initial_state()
    state["agent_service"] = _Agent({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_node_id": "NODE_1",
    })
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["workspace_manager"] = wm
    state["workspace_path"] = _FAKE_WS

    result = await monitor_agent(state)

    assert result["phase"] == "monitoring_pr"
    assert wm.teardown_calls == [_FAKE_WS]
    assert result["workspace_path"] is None
    assert result["workspace_branch"] is None


@pytest.mark.asyncio
async def test_monitor_agent_teardown_called_on_error() -> None:
    """workspace teardown is called when agent reports error."""
    wm = _WorkspaceManager()
    state = initial_state()
    state["agent_service"] = _Agent({"status": "error", "questions": ["crash"]})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["workspace_manager"] = wm
    state["workspace_path"] = _FAKE_WS

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert wm.teardown_calls == [_FAKE_WS]
    assert result["workspace_path"] is None


@pytest.mark.asyncio
async def test_monitor_agent_teardown_called_on_blocked() -> None:
    """workspace teardown is called when agent reports blocked."""
    wm = _WorkspaceManager()
    state = initial_state()
    state["agent_service"] = _Agent({"status": "blocked", "questions": ["q1"]})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["workspace_manager"] = wm
    state["workspace_path"] = _FAKE_WS

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert wm.teardown_calls == [_FAKE_WS]
    assert result["workspace_path"] is None


@pytest.mark.asyncio
async def test_monitor_agent_teardown_called_on_session_expired() -> None:
    """workspace teardown is called when session expires."""
    wm = _WorkspaceManager()
    state = initial_state()
    state["agent_service"] = _Agent({"status": "session_expired"})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["workspace_manager"] = wm
    state["workspace_path"] = _FAKE_WS

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert wm.teardown_calls == [_FAKE_WS]
    assert result["workspace_path"] is None


@pytest.mark.asyncio
async def test_monitor_agent_teardown_called_on_transport_error() -> None:
    """workspace teardown is called even on TransportError (terminal state)."""
    wm = _WorkspaceManager()
    state = initial_state()
    state["agent_service"] = _AgentTransportError()
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["workspace_manager"] = wm
    state["workspace_path"] = _FAKE_WS

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert wm.teardown_calls == [_FAKE_WS]
    assert result["workspace_path"] is None


@pytest.mark.asyncio
async def test_monitor_agent_no_teardown_when_still_working() -> None:
    """No teardown when agent is still working (non-terminal state)."""
    wm = _WorkspaceManager()
    state = initial_state()
    state["agent_service"] = _Agent({"status": "working"})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["workspace_manager"] = wm
    state["workspace_path"] = _FAKE_WS

    result = await monitor_agent(state)

    assert result["phase"] == "monitoring_agent"
    assert wm.teardown_calls == []
    # workspace_path remains set while agent is still working
    assert result["workspace_path"] == _FAKE_WS


@pytest.mark.asyncio
async def test_monitor_agent_teardown_called_on_unexpected_exception() -> None:
    """Workspace teardown fires via finally even when an unexpected exception propagates."""
    wm = _WorkspaceManager()
    state = initial_state()
    state["agent_service"] = _AgentUnexpectedError()
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["workspace_manager"] = wm
    state["workspace_path"] = _FAKE_WS

    with pytest.raises(RuntimeError, match="unexpected internal error"):
        await monitor_agent(state)

    assert wm.teardown_calls == [_FAKE_WS]
    assert state["workspace_path"] is None
