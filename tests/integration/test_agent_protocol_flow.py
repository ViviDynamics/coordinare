from __future__ import annotations

import sys
import time

import pytest

from coordinare.services.agent_service import AgentService
from coordinare.transport.subprocess_transport import SubprocessTransport

MOCK_AGENT = str(
    __import__("pathlib").Path(__file__).resolve().parent.parent / "fixtures" / "mock_agent.py"
)


def _make_transport(tmp_path, scenario: str = "happy_path") -> SubprocessTransport:
    """Create a SubprocessTransport targeting the mock agent."""
    wrapper = tmp_path / "agent_wrapper.sh"
    wrapper.write_text(
        f"#!/bin/sh\nexec {sys.executable} {MOCK_AGENT}\n"
    )
    wrapper.chmod(0o755)
    return SubprocessTransport(str(wrapper), timeout=30)


@pytest.fixture
def agent_env(monkeypatch, tmp_path):
    """Set up mock agent environment."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("MOCK_AGENT_STATE_DIR", str(state_dir))
    return tmp_path


class TestHappyPathCycle:
    @pytest.mark.asyncio
    async def test_dispatch_accepted(self, agent_env, monkeypatch) -> None:
        monkeypatch.setenv("MOCK_AGENT_SCENARIO", "happy_path")
        transport = _make_transport(agent_env)
        service = AgentService(transport)

        result = await service.dispatch_card({
            "id": "ITEM_1",
            "title": "Fix bug",
            "description": "Details",
            "acceptance_criteria": ["Tests pass"],
            "status": "TODO",
        })

        assert result["status"] == "accepted"
        assert result["session_id"] == "mock-session-1"

    @pytest.mark.asyncio
    async def test_status_working(self, agent_env, monkeypatch) -> None:
        monkeypatch.setenv("MOCK_AGENT_SCENARIO", "happy_path")
        transport = _make_transport(agent_env)
        service = AgentService(transport)

        # First call: dispatch (call_count=0 → accepted)
        await service.dispatch_card({"id": "ITEM_1", "title": "T", "description": "D"})
        # Second call: status (call_count=1 → working)
        result = await service.check_status("mock-session-1")

        assert result["status"] == "working"
        assert result["progress"] == "Building and testing"

    @pytest.mark.asyncio
    async def test_status_pr_opened(self, agent_env, monkeypatch) -> None:
        monkeypatch.setenv("MOCK_AGENT_SCENARIO", "happy_path")
        transport = _make_transport(agent_env)
        service = AgentService(transport)

        await service.dispatch_card({"id": "ITEM_1", "title": "T", "description": "D"})
        await service.check_status("mock-session-1")
        result = await service.check_status("mock-session-1")

        assert result["status"] == "pr_opened"
        assert result["pr_url"] == "https://github.com/org/repo/pull/42"
        assert result["pr_node_id"] == "PR_kwDOTest42"

    @pytest.mark.asyncio
    async def test_full_dispatch_working_pr_opened_cycle(self, agent_env, monkeypatch) -> None:
        """Full happy_path cycle: dispatch → working → pr_opened."""
        monkeypatch.setenv("MOCK_AGENT_SCENARIO", "happy_path")
        transport = _make_transport(agent_env)
        service = AgentService(transport)

        r1 = await service.dispatch_card({
            "id": "ITEM_1", "title": "T", "description": "D",
            "acceptance_criteria": [], "status": "TODO",
        })
        assert r1["status"] == "accepted"

        r2 = await service.check_status("mock-session-1")
        assert r2["status"] == "working"

        r3 = await service.check_status("mock-session-1")
        assert r3["status"] == "pr_opened"


class TestErrorScenarios:
    @pytest.mark.asyncio
    async def test_blocked_scenario(self, agent_env, monkeypatch) -> None:
        monkeypatch.setenv("MOCK_AGENT_SCENARIO", "blocked")
        transport = _make_transport(agent_env)
        service = AgentService(transport)

        result = await service.dispatch_card({"id": "ITEM_1", "title": "T", "description": "D"})

        assert result["status"] == "blocked"
        assert len(result["questions"]) == 2

    @pytest.mark.asyncio
    async def test_error_scenario(self, agent_env, monkeypatch) -> None:
        monkeypatch.setenv("MOCK_AGENT_SCENARIO", "error")
        transport = _make_transport(agent_env)
        service = AgentService(transport)

        result = await service.dispatch_card({"id": "ITEM_1", "title": "T", "description": "D"})

        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_busy_scenario(self, agent_env, monkeypatch) -> None:
        monkeypatch.setenv("MOCK_AGENT_SCENARIO", "busy")
        transport = _make_transport(agent_env)
        service = AgentService(transport)

        result = await service.dispatch_card({"id": "ITEM_1", "title": "T", "description": "D"})

        assert result["status"] == "busy"

    @pytest.mark.asyncio
    async def test_session_expired_scenario(self, agent_env, monkeypatch) -> None:
        monkeypatch.setenv("MOCK_AGENT_SCENARIO", "session_expired")
        transport = _make_transport(agent_env)
        service = AgentService(transport)

        result = await service.dispatch_card({"id": "ITEM_1", "title": "T", "description": "D"})

        assert result["status"] == "session_expired"

    @pytest.mark.asyncio
    async def test_nonexistent_executable_returns_error(self) -> None:
        transport = SubprocessTransport("/nonexistent/agent", timeout=5)
        service = AgentService(transport)

        result = await service.dispatch_card({"id": "ITEM_1", "title": "T", "description": "D"})

        assert result["status"] == "error"


class TestAllStatusTypesExercised:
    @pytest.mark.asyncio
    async def test_all_nine_status_types(self, agent_env, monkeypatch) -> None:
        """Assert all 9 StatusType values are exercised across scenarios."""
        observed_statuses: set[str] = set()

        scenarios_to_statuses = {
            "happy_path": ["accepted", "working", "pr_opened"],
            "blocked": ["blocked"],
            "error": ["error"],
            "busy": ["busy"],
            "session_expired": ["session_expired"],
            "acknowledged": ["acknowledged"],
            "unknown": ["unknown"],
        }

        for scenario, expected_statuses in scenarios_to_statuses.items():
            # Reset state for each scenario
            state_dir = agent_env / f"state_{scenario}"
            state_dir.mkdir(exist_ok=True)
            monkeypatch.setenv("MOCK_AGENT_STATE_DIR", str(state_dir))
            monkeypatch.setenv("MOCK_AGENT_SCENARIO", scenario)
            transport = _make_transport(agent_env)
            service = AgentService(transport)

            for _ in expected_statuses:
                result = await service.dispatch_card({
                    "id": "ITEM_1", "title": "T", "description": "D",
                })
                observed_statuses.add(result["status"])

        expected = {
            "accepted", "working", "pr_opened", "blocked", "error",
            "unknown", "busy", "acknowledged", "session_expired",
        }
        assert observed_statuses == expected


class TestPerformance:
    @pytest.mark.asyncio
    async def test_full_cycle_within_60s(self, agent_env, monkeypatch) -> None:
        """Full happy_path cycle completes within 60s wall time."""
        monkeypatch.setenv("MOCK_AGENT_SCENARIO", "happy_path")
        transport = _make_transport(agent_env)
        service = AgentService(transport)

        start = time.monotonic()

        await service.dispatch_card({
            "id": "ITEM_1", "title": "T", "description": "D",
            "acceptance_criteria": [], "status": "TODO",
        })
        await service.check_status("mock-session-1")
        await service.check_status("mock-session-1")

        elapsed = time.monotonic() - start
        assert elapsed < 60, f"Full cycle took {elapsed:.1f}s, expected < 60s"


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_via_mock_agent(self, agent_env, monkeypatch) -> None:
        monkeypatch.setenv("MOCK_AGENT_SCENARIO", "happy_path")
        transport = _make_transport(agent_env)
        service = AgentService(transport)

        result = await service.check_health()

        assert result["status"] == "accepted"
