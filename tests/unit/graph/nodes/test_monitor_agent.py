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

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        return self._response


class _AgentTransportError:
    """Raises TransportError from check_status."""

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        raise TransportError("SSH tunnel collapsed")


class _AgentPermanentGitHubError:
    """Raises PermanentGitHubError from check_status."""

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        raise PermanentGitHubError("Token revoked")


class _AgentUnexpectedError:
    """Raises an unexpected RuntimeError from check_status (not a known transport error)."""

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        raise RuntimeError("unexpected internal error")



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

    assert result["phase"] == "monitoring_performer"


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
    state["agent_service"] = _Agent({"status": "error", "reason": "Crash dump"})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    # 019: error status now sets phase="blocked" (FR-006) instead of system_error
    assert result["phase"] == "blocked"


# ---------------------------------------------------------------------------
# 4. TransportError / PermanentGitHubError → card moved to BLOCKED (T019a)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_error_routes_to_system_error() -> None:
    """TransportError from check_status → system_error phase for retry (not immediate BLOCKED)."""
    gh = _GitHub()
    state = initial_state()
    state["agent_service"] = _AgentTransportError()
    state["current_card"] = {"id": "ITEM_1"}
    state["github_service"] = gh
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "system_error"
    assert result["system_error_count"] == 1
    assert result["system_error_reason"] is not None
    assert ("ITEM_1", "BLOCKED") not in gh.move_calls


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
async def test_transport_error_without_github_service_routes_to_system_error() -> None:
    """When github_service is None, TransportError still routes to system_error without crashing."""
    state = initial_state()
    state["agent_service"] = _AgentTransportError()
    state["current_card"] = {"id": "ITEM_1"}
    # No github_service set — stays None
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "system_error"
    assert result["system_error_count"] == 1


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
    state["agent_service"] = _Agent({"status": "error", "reason": "crash"})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["workspace_manager"] = wm
    state["workspace_path"] = _FAKE_WS

    result = await monitor_agent(state)

    # 019: error status now sets phase="blocked" (FR-006)
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
    """workspace teardown is called when session expires; card is auto-requeued, not blocked."""
    wm = _WorkspaceManager()
    state = initial_state()
    state["agent_service"] = _Agent({"status": "session_expired"})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["workspace_manager"] = wm
    state["workspace_path"] = _FAKE_WS

    result = await monitor_agent(state)

    # session_expired is transient — auto-requeue without blocking the card
    assert result["phase"] == "idle"
    assert result["agent_dispatch"] == {}
    assert result["open_questions"] == []
    assert wm.teardown_calls == [_FAKE_WS]
    assert result["workspace_path"] is None


@pytest.mark.asyncio
async def test_monitor_agent_teardown_called_on_transport_error() -> None:
    """workspace teardown is called even on TransportError (terminal state → system_error)."""
    wm = _WorkspaceManager()
    state = initial_state()
    state["agent_service"] = _AgentTransportError()
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["workspace_manager"] = wm
    state["workspace_path"] = _FAKE_WS

    result = await monitor_agent(state)

    assert result["phase"] == "system_error"
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

    assert result["phase"] == "monitoring_performer"
    assert wm.teardown_calls == []
    # workspace_path remains set while agent is still working
    assert result["workspace_path"] == _FAKE_WS


@pytest.mark.asyncio
async def test_permanent_github_error_without_github_service_still_blocks() -> None:
    """When github_service is None, PermanentGitHubError still sets phase='blocked'."""
    state = initial_state()
    state["agent_service"] = _AgentPermanentGitHubError()
    state["current_card"] = {"id": "ITEM_1"}
    # No github_service set — stays None
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert any("Token revoked" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_permanent_github_error_move_card_fails_gracefully() -> None:
    """If move_card itself fails during PermanentGitHubError handling, phase is still 'blocked'."""
    class _GitHubRaises:
        def __init__(self) -> None:
            self.move_calls: list[tuple[str, str]] = []

        async def move_card(self, item_id: str, status: str) -> None:
            self.move_calls.append((item_id, status))
            raise RuntimeError("GitHub API unavailable")

    gh = _GitHubRaises()
    state = initial_state()
    state["agent_service"] = _AgentPermanentGitHubError()
    state["current_card"] = {"id": "ITEM_1"}
    state["github_service"] = gh
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert ("ITEM_1", "BLOCKED") in gh.move_calls


@pytest.mark.asyncio
async def test_monitor_agent_accumulates_events_with_existing() -> None:
    """New events are appended to existing performer_events (capped at 100)."""
    state = initial_state()
    state["agent_service"] = _Agent({"status": "working", "events": [{"type": "new"}]})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["performer_events"] = [{"type": "old"}]

    result = await monitor_agent(state)

    assert result["performer_events"] == [{"type": "old"}, {"type": "new"}]


@pytest.mark.asyncio
async def test_monitor_agent_stores_metrics() -> None:
    """performer_metrics is updated from status response."""
    state = initial_state()
    state["agent_service"] = _Agent({"status": "working", "metrics": {"pid": 1234, "memory_bytes": 512}})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["performer_metrics"] == {"pid": 1234, "memory_bytes": 512}


@pytest.mark.asyncio
async def test_monitor_agent_session_expired_move_card_fails_gracefully() -> None:
    """If move_card fails during session_expired handling, phase is still 'idle'."""
    class _GitHubRaises:
        async def move_card(self, item_id: str, status: str) -> None:
            raise RuntimeError("GitHub API unavailable")

    state = initial_state()
    state["agent_service"] = _Agent({"status": "session_expired"})
    state["current_card"] = {"id": "ITEM_1"}
    state["github_service"] = _GitHubRaises()
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "idle"
    assert result["agent_dispatch"] == {}


@pytest.mark.asyncio
async def test_monitor_agent_blocked_with_no_questions() -> None:
    """Blocked status with no questions list sets open_questions to empty list."""
    state = initial_state()
    state["agent_service"] = _Agent({"status": "blocked"})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == []


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


@pytest.mark.asyncio
async def test_transport_error_resets_stale_system_error_state_from_previous_card() -> None:
    """If system_error_notified=True from a previous card's exhausted retry cycle,
    TransportError resets count and notified so the new card gets its full retry budget."""
    state = initial_state()
    state["agent_service"] = _AgentTransportError()
    state["current_card"] = {"id": "ITEM_NEW"}
    state["agent_dispatch"] = {"session_id": "s-stale"}
    # Simulate stale state inherited from a previous card that maxed out retries
    state["system_error_count"] = 3
    state["system_error_notified"] = True

    result = await monitor_agent(state)

    assert result["phase"] == "system_error"
    # Count was reset from 3 → 0 then incremented to 1 (not 4)
    assert result["system_error_count"] == 1
    # Notification flag cleared so operator gets alerted if this card also max-retries
    assert result["system_error_notified"] is False
    # Stale dispatch metadata cleared
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None


# ---------------------------------------------------------------------------
# US1: pr_opened -> github.move_card("IN_REVIEW") called (T002-T005)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_agent_calls_move_card_in_review_on_pr_opened() -> None:
    """move_card("IN_REVIEW") is called when pr_opened received with valid fields."""
    gh = _GitHub()
    state = initial_state()
    state["agent_service"] = _Agent({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_node_id": "PR_NODE_1",
    })
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = gh

    result = await monitor_agent(state)

    assert ("ITEM_1", "IN_REVIEW") in gh.move_calls
    assert result["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_monitor_agent_pr_opened_missing_pr_url_does_not_move_card() -> None:
    """pr_opened with pr_url=None must not move card; routes to system_error instead."""
    gh = _GitHub()
    state = initial_state()
    state["agent_service"] = _Agent({
        "status": "pr_opened",
        "pr_url": None,
        "pr_node_id": "PR_NODE_1",
    })
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = gh

    result = await monitor_agent(state)

    assert ("ITEM_1", "IN_REVIEW") not in gh.move_calls
    assert result["phase"] == "system_error"
    assert result["current_card"]["status"] == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_monitor_agent_pr_opened_missing_pr_node_id_does_not_move_card() -> None:
    """pr_opened with pr_node_id=None must not move card; routes to system_error instead."""
    gh = _GitHub()
    state = initial_state()
    state["agent_service"] = _Agent({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_node_id": None,
    })
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = gh

    result = await monitor_agent(state)

    assert ("ITEM_1", "IN_REVIEW") not in gh.move_calls
    assert result["phase"] == "system_error"


@pytest.mark.asyncio
async def test_monitor_agent_pr_opened_move_card_fails_gracefully() -> None:
    """If move_card raises during pr_opened handling, phase is still 'monitoring_pr'."""
    class _GitHubRaises:
        def __init__(self) -> None:
            self.move_calls: list[tuple[str, str]] = []

        async def move_card(self, item_id: str, status: str) -> None:
            self.move_calls.append((item_id, status))
            raise RuntimeError("GitHub API unavailable")

    gh = _GitHubRaises()
    state = initial_state()
    state["agent_service"] = _Agent({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/2",
        "pr_node_id": "PR_NODE_2",
    })
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = gh

    result = await monitor_agent(state)

    assert result["phase"] == "monitoring_pr"
    assert result["current_card"]["status"] == "IN_REVIEW"


# ---------------------------------------------------------------------------
# US2: session_expired with pr_node_id -> route to monitoring_pr (T008-T010)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_agent_session_expired_with_pr_routes_to_monitoring_pr() -> None:
    """session_expired with pr_node_id set → transitions to monitoring_pr, not idle."""
    gh = _GitHub()
    state = initial_state()
    state["agent_service"] = _Agent({"status": "session_expired"})
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_NODE_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = gh

    result = await monitor_agent(state)

    assert result["phase"] == "monitoring_pr"
    assert ("ITEM_1", "TODO") not in gh.move_calls


@pytest.mark.asyncio
async def test_monitor_agent_session_expired_with_pr_clears_agent_dispatch() -> None:
    """session_expired with pr_node_id set → agent_dispatch cleared to prevent relay_feedback
    from contacting the dead performer and creating a monitoring_pr→session_expired loop."""
    state = initial_state()
    state["agent_service"] = _Agent({"status": "session_expired"})
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_NODE_1"}
    state["agent_dispatch"] = {"session_id": "s-expired"}
    state["agent_dispatch_at"] = "2026-01-01T00:00:00Z"

    result = await monitor_agent(state)

    assert result["phase"] == "monitoring_pr"
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None


@pytest.mark.asyncio
async def test_monitor_agent_session_expired_without_pr_still_requeues_to_todo() -> None:
    """session_expired without pr_node_id → existing requeue behaviour preserved."""
    gh = _GitHub()
    state = initial_state()
    state["agent_service"] = _Agent({"status": "session_expired"})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = gh

    result = await monitor_agent(state)

    assert result["phase"] == "idle"
    assert ("ITEM_1", "TODO") in gh.move_calls


# ---------------------------------------------------------------------------
# US3: session_expired preserves open_questions in card_clarifications (T018-T021)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_agent_session_expired_saves_open_questions_to_clarifications() -> None:
    """open_questions saved to card_clarifications with empty answer before clearing."""
    state = initial_state()
    state["agent_service"] = _Agent({"status": "session_expired"})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["open_questions"] = ["What API?", "Which region?"]

    result = await monitor_agent(state)

    assert result["open_questions"] == []
    clarifications = result["card_clarifications"]
    assert len(clarifications) == 1
    assert set(clarifications[0]["questions"]) == {"What API?", "Which region?"}
    assert clarifications[0]["answer"] == ""


@pytest.mark.asyncio
async def test_monitor_agent_session_expired_appends_to_existing_clarifications() -> None:
    """Existing card_clarifications entries are preserved; new entry is appended."""
    state = initial_state()
    state["agent_service"] = _Agent({"status": "session_expired"})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["open_questions"] = ["New Q"]
    state["card_clarifications"] = [{"questions": ["Old Q"], "answer": "Old A"}]

    result = await monitor_agent(state)

    assert len(result["card_clarifications"]) == 2
    assert result["card_clarifications"][0]["questions"] == ["Old Q"]
    assert result["card_clarifications"][1]["questions"] == ["New Q"]
    assert result["card_clarifications"][1]["answer"] == ""


@pytest.mark.asyncio
async def test_monitor_agent_session_expired_no_questions_skips_clarification_save() -> None:
    """When open_questions is empty, no entry is appended to card_clarifications."""
    state = initial_state()
    state["agent_service"] = _Agent({"status": "session_expired"})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["open_questions"] = []
    state["card_clarifications"] = []

    result = await monitor_agent(state)

    assert result["card_clarifications"] == []


@pytest.mark.asyncio
async def test_monitor_agent_session_expired_with_pr_and_open_questions_saves_before_routing() -> None:
    """open_questions are saved to card_clarifications even when routing to monitoring_pr."""
    state = initial_state()
    state["agent_service"] = _Agent({"status": "session_expired"})
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_NODE_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["open_questions"] = ["Unanswered question?"]

    result = await monitor_agent(state)

    assert result["phase"] == "monitoring_pr"
    assert result["open_questions"] == []
    assert len(result["card_clarifications"]) == 1
    assert result["card_clarifications"][0]["questions"] == ["Unanswered question?"]


# ---------------------------------------------------------------------------
# Copilot review fixes - stale system_error_notified reset on pr_opened error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_agent_pr_opened_missing_fields_resets_stale_notified() -> None:
    """Stale system_error_notified=True is cleared before incrementing on pr_opened error."""
    state = initial_state()
    state["agent_service"] = _Agent({"status": "pr_opened", "pr_url": None, "pr_node_id": None})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    # Simulate stale state inherited from a previous card
    state["system_error_notified"] = True
    state["system_error_count"] = 5

    result = await monitor_agent(state)

    assert result["phase"] == "system_error"
    # Count was reset to 0 then incremented once - so result is 1
    assert result["system_error_count"] == 1
    assert result["system_error_notified"] is False


@pytest.mark.asyncio
async def test_monitor_agent_pr_opened_missing_fields_no_stale_state_increments_normally() -> None:
    """Without stale system_error_notified, count increments from current value."""
    state = initial_state()
    state["agent_service"] = _Agent({"status": "pr_opened", "pr_url": None, "pr_node_id": None})
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["system_error_notified"] = False
    state["system_error_count"] = 2

    result = await monitor_agent(state)

    assert result["phase"] == "system_error"
    assert result["system_error_count"] == 3
