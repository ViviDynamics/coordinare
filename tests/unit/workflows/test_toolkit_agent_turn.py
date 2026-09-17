"""Tests for Toolkit.run_agent_turn (spec 167 T006, T007, T014)."""
import asyncio

import pytest
from performer.workflows.base import WorkflowMetrics
from performer.workflows.toolkit import Toolkit


@pytest.fixture
def metrics():
    """Fresh metrics for each test."""
    return WorkflowMetrics()


@pytest.fixture
def event_log():
    """Collect emitted events."""
    return []


@pytest.fixture
def toolkit(metrics, event_log):
    """Toolkit with an event sink."""
    def _sink(event):
        event_log.append(event)

    return Toolkit(
        metrics=metrics,
        event_sink=_sink,
    )


@pytest.fixture
def fake_runner():
    """Fake agent_turn_runner that completes successfully."""
    async def _runner(brief: dict, *, timeout_s: float) -> dict:
        await asyncio.sleep(0.01)  # simulate work
        return {
            "exit_state": "done",
            "output_tail": "test output",
            "changed_paths": {"test_file.py": "added"},
            "wall_ms": 10,
            "harness_commits": [],
        }
    return _runner


class TestRunAgentTurn:
    """Test Toolkit.run_agent_turn."""

    async def test_run_agent_turn_success(self, toolkit, fake_runner, metrics, event_log):
        """T006(a): toolkit.run_agent_turn(brief, fake_runner) completes with TurnResult dict."""
        toolkit._agent_turn_runner = fake_runner
        brief = {
            "kind": "tests",
            "milestone_goal": "Test login flow",
            "scope": "auth/",
            "done_when": "login tests pass",
            "forbidden_paths": ["docs/"],
            "milestone_index": 0,
        }
        result = await toolkit.run_agent_turn(brief, timeout_s=5.0)

        assert result["exit_state"] == "done"
        assert result["output_tail"] == "test output"
        assert result["changed_paths"] == {"test_file.py": "added"}
        assert isinstance(result["wall_ms"], int)
        assert result["harness_commits"] == []

    async def test_run_agent_turn_increments_metrics(
        self, toolkit, fake_runner, metrics, event_log,
    ):
        """T006: metrics.agent_turns incremented."""
        toolkit._agent_turn_runner = fake_runner
        brief = {"kind": "tests", "milestone_goal": "goal", "scope": "scope",
                 "done_when": "done", "forbidden_paths": [], "milestone_index": 0}

        assert metrics.agent_turns == 0
        await toolkit.run_agent_turn(brief, timeout_s=5.0)
        assert metrics.agent_turns == 1

    async def test_run_agent_turn_appends_duration(
        self, toolkit, fake_runner, metrics, event_log,
    ):
        """T006: metrics.turn_durations_ms appended with wall_ms."""
        toolkit._agent_turn_runner = fake_runner
        brief = {"kind": "tests", "milestone_goal": "goal", "scope": "scope",
                 "done_when": "done", "forbidden_paths": [], "milestone_index": 0}

        assert metrics.turn_durations_ms == []
        await toolkit.run_agent_turn(brief, timeout_s=5.0)
        assert len(metrics.turn_durations_ms) == 1
        assert metrics.turn_durations_ms[0] == 10

    async def test_run_agent_turn_emits_start_end_events(
        self, toolkit, fake_runner, metrics, event_log,
    ):
        """T007: emits turn.start and turn.end events."""
        toolkit._agent_turn_runner = fake_runner
        brief = {
            "kind": "tests",
            "milestone_goal": "Test login",
            "scope": "auth/",
            "done_when": "done",
            "forbidden_paths": [],
            "milestone_index": 0,
        }

        await toolkit.run_agent_turn(brief, timeout_s=5.0)

        # Check for start and end events
        event_texts = [getattr(e, 'text', '') for e in event_log]
        assert any("turn.start" in t for t in event_texts)
        assert any("turn.end" in t for t in event_texts)

    async def test_run_agent_turn_timeout_cancels_runner(
        self, toolkit, metrics, event_log,
    ):
        """T006(f): timeout enforcement via asyncio.wait_for cancels the turn."""
        call_was_cancelled = False

        async def _slow_runner(brief: dict, *, timeout_s: float) -> dict:
            nonlocal call_was_cancelled
            try:
                await asyncio.sleep(10)  # sleep longer than timeout
            except asyncio.CancelledError:
                call_was_cancelled = True
                raise
            return {"exit_state": "done", "output_tail": "", "changed_paths": {},
                   "wall_ms": 10000, "harness_commits": []}

        toolkit._agent_turn_runner = _slow_runner
        brief = {"kind": "tests", "milestone_goal": "goal", "scope": "scope",
                 "done_when": "done", "forbidden_paths": [], "milestone_index": 0}

        result = await toolkit.run_agent_turn(brief, timeout_s=0.1)

        assert result["exit_state"] == "timeout"
        assert call_was_cancelled

    async def test_run_agent_turn_timeout_still_increments_metrics(
        self, toolkit, metrics,
    ):
        """T006(f): timeout still increments agent_turns and turn_durations_ms."""
        async def _slow_runner(brief: dict, *, timeout_s: float) -> dict:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise

        toolkit._agent_turn_runner = _slow_runner
        brief = {"kind": "tests", "milestone_goal": "goal", "scope": "scope",
                 "done_when": "done", "forbidden_paths": [], "milestone_index": 0}

        result = await toolkit.run_agent_turn(brief, timeout_s=0.1)

        assert result["exit_state"] == "timeout"
        assert metrics.agent_turns == 1
        assert len(metrics.turn_durations_ms) == 1
        assert metrics.turn_durations_ms[0] > 0

    async def test_run_agent_turn_runner_exception(self, toolkit, metrics):
        """T006(g): runner raising returns exit_state "error"."""
        async def _error_runner(brief: dict, *, timeout_s: float) -> dict:
            raise ValueError("Simulated error")

        toolkit._agent_turn_runner = _error_runner
        brief = {"kind": "tests", "milestone_goal": "goal", "scope": "scope",
                 "done_when": "done", "forbidden_paths": [], "milestone_index": 0}

        result = await toolkit.run_agent_turn(brief, timeout_s=5.0)

        assert result["exit_state"] == "error"
        assert "Simulated error" in result["output_tail"]
        assert metrics.agent_turns == 1

    async def test_run_agent_turn_no_runner(self, toolkit):
        """T006: no runner raises RuntimeError."""
        brief = {"kind": "tests", "milestone_goal": "goal", "scope": "scope",
                 "done_when": "done", "forbidden_paths": [], "milestone_index": 0}

        with pytest.raises(RuntimeError, match="no agent_turn_runner"):
            await toolkit.run_agent_turn(brief, timeout_s=5.0)
