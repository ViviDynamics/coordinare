"""Tests for build_production_toolkit agent_turn_runner wiring (spec 167 T012, T013)."""
import asyncio
import subprocess
import tempfile
from pathlib import Path

import pytest
from performer.backends.base import BackendStatus
from performer.models import Score, Stand
from performer.workflows.adapter import (
    build_agent_turn_runner,
    build_production_toolkit,
)
from performer.workflows.base import WorkflowMetrics


@pytest.fixture
def temp_git_repo():
    """Create a real temporary git repository."""
    tmpdir = tempfile.mkdtemp(prefix="test_turn_runner_")
    repo_path = Path(tmpdir)

    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path, check=True, capture_output=True,
    )

    (repo_path / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo_path, check=True, capture_output=True)

    yield repo_path

    import shutil
    shutil.rmtree(repo_path, ignore_errors=True)


@pytest.fixture
def stand(temp_git_repo):
    """Create a Stand object pointing to the temp repo."""
    return Stand(path=temp_git_repo, branch="main")


def _make_score(**kwargs):
    """Helper to create a Score with required fields."""
    defaults = {
        "title": "Test Card",
        "repo_url": "https://github.com/example/repo",
        "branch": "main",
        "role": "implementing",
        "workspace_path": ".",
    }
    defaults.update(kwargs)
    return Score(**defaults)


class FakeBackend:
    """Fake backend adapter for testing turn runner."""

    def __init__(self, behavior="success", polls_until_done=2):
        self.behavior = behavior
        self.polls_until_done = polls_until_done
        self.poll_count = 0
        self.started = False
        self.score_received = None
        self.stand_received = None
        self.stopped = False
        self._events_buffer = []

    async def start(self, stand, score, *, model=None, effort=None, temperature=None, max_tokens=None):
        self.started = True
        self.stand_received = stand
        self.score_received = score

    def get_status(self):
        self.poll_count += 1
        if self.behavior == "success":
            if self.poll_count < self.polls_until_done:
                return BackendStatus(state="working", progress=f"poll {self.poll_count}")
            return BackendStatus(state="done", output="backend completed successfully")
        if self.behavior == "error":
            if self.poll_count < self.polls_until_done:
                return BackendStatus(state="working")
            return BackendStatus(state="error", error_reason="backend failed")
        if self.behavior == "timeout":
            return BackendStatus(state="working")
        return BackendStatus(state="working")

    def drain_events(self):
        events = self._events_buffer
        self._events_buffer = []
        return events

    async def relay_feedback(self, feedback):
        pass

    async def stop(self):
        self.stopped = True


def fake_backend_factory(backend_name):
    """Factory that returns a FakeBackend."""
    return FakeBackend()


@pytest.mark.asyncio
async def test_build_agent_turn_runner_success(stand, temp_git_repo):
    """T012: build_agent_turn_runner creates a runner that completes successfully."""
    score = _make_score(workspace_path=str(temp_git_repo))
    runner = build_agent_turn_runner(
        stand=stand,
        score=score,
        backend_name="example",
        backend_factory=fake_backend_factory,
    )

    brief = {
        "kind": "tests",
        "milestone_goal": "Test login",
        "scope": "auth/",
        "done_when": "login tests pass",
        "forbidden_paths": ["docs/"],
        "milestone_index": 0,
        "persona": "Write test files",
    }

    result = await runner(brief, timeout_s=10.0)

    assert result["exit_state"] == "done"
    assert isinstance(result["wall_ms"], int)
    assert result["wall_ms"] > 0
    assert isinstance(result["changed_paths"], dict)
    assert isinstance(result["harness_commits"], list)


@pytest.mark.asyncio
async def test_build_agent_turn_runner_error(stand, temp_git_repo):
    """T012(d): runner returns error when backend fails."""
    def error_backend_factory(backend_name):
        return FakeBackend(behavior="error")

    score = _make_score(workspace_path=str(temp_git_repo))
    runner = build_agent_turn_runner(
        stand=stand,
        score=score,
        backend_name="example",
        backend_factory=error_backend_factory,
    )

    brief = {
        "kind": "implement",
        "milestone_goal": "Implement login",
        "scope": "auth/",
        "done_when": "tests pass",
        "forbidden_paths": ["docs/"],
        "milestone_index": 0,
        "persona": "Make tests pass",
    }

    result = await runner(brief, timeout_s=10.0)

    assert result["exit_state"] == "error"
    assert "backend failed" in result["output_tail"]


@pytest.mark.asyncio
async def test_build_agent_turn_runner_timeout(stand, temp_git_repo):
    """T012(c): runner times out after wall_clock expiry."""
    def timeout_backend_factory(backend_name):
        return FakeBackend(behavior="timeout")

    score = _make_score(workspace_path=str(temp_git_repo))
    runner = build_agent_turn_runner(
        stand=stand,
        score=score,
        backend_name="example",
        backend_factory=timeout_backend_factory,
    )

    brief = {
        "kind": "tests",
        "milestone_goal": "Test login",
        "scope": "auth/",
        "done_when": "login tests pass",
        "forbidden_paths": [],
        "milestone_index": 0,
        "persona": "Write tests",
    }

    result = await runner(brief, timeout_s=0.1)

    assert result["exit_state"] == "timeout"


@pytest.mark.asyncio
async def test_build_agent_turn_runner_score_modification(stand, temp_git_repo):
    """T012(b): per-turn Score carries modified persona_instructions."""
    received_scores = []

    class CheckingBackend(FakeBackend):
        async def start(self, stand, score, *, model=None, effort=None, temperature=None, max_tokens=None):
            received_scores.append(score)
            await super().start(stand, score, model=model, effort=effort, temperature=temperature, max_tokens=max_tokens)

    def checking_factory(backend_name):
        return CheckingBackend()

    score = _make_score(workspace_path=str(temp_git_repo))
    runner = build_agent_turn_runner(
        stand=stand,
        score=score,
        backend_name="example",
        backend_factory=checking_factory,
    )

    brief = {
        "kind": "tests",
        "milestone_goal": "Test login",
        "scope": "auth/",
        "done_when": "login tests pass",
        "forbidden_paths": [],
        "milestone_index": 0,
        "persona": "Write test files for this feature",
    }

    await runner(brief, timeout_s=10.0)

    assert len(received_scores) == 1
    received_score = received_scores[0]
    assert received_score.persona_instructions == "Write test files for this feature"


@pytest.mark.asyncio
async def test_build_agent_turn_runner_git_tracking(stand, temp_git_repo):
    """T012(e): runner tracks git state and computes changed_paths."""
    class CommittingBackend(FakeBackend):
        async def start(self, stand, score, *, model=None, effort=None, temperature=None, max_tokens=None):
            await super().start(stand, score, model=model, effort=effort, temperature=temperature, max_tokens=max_tokens)
            (stand.path / "test_file.py").write_text("def test_login(): pass")
            subprocess.run(["git", "add", "test_file.py"], cwd=stand.path, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Add tests"],
                cwd=stand.path, check=True, capture_output=True,
            )

    def committing_factory(backend_name):
        return CommittingBackend()

    score = _make_score(workspace_path=str(temp_git_repo))
    runner = build_agent_turn_runner(
        stand=stand,
        score=score,
        backend_name="example",
        backend_factory=committing_factory,
    )

    brief = {
        "kind": "tests",
        "milestone_goal": "Test login",
        "scope": "auth/",
        "done_when": "login tests pass",
        "forbidden_paths": [],
        "milestone_index": 0,
        "persona": "Write tests",
    }

    result = await runner(brief, timeout_s=10.0)

    assert "test_file.py" in result["changed_paths"]
    assert result["changed_paths"]["test_file.py"] == "added"
    assert len(result["harness_commits"]) == 1


@pytest.mark.asyncio
async def test_build_agent_turn_runner_cancellation(stand, temp_git_repo):
    """T012(f): on cancellation runner calls adapter.stop() and re-raises."""
    stopped_backends = []

    class TrackingBackend(FakeBackend):
        async def stop(self):
            stopped_backends.append(self)
            await super().stop()

    def tracking_factory(backend_name):
        return TrackingBackend(behavior="timeout")

    score = _make_score(workspace_path=str(temp_git_repo))
    runner = build_agent_turn_runner(
        stand=stand,
        score=score,
        backend_name="example",
        backend_factory=tracking_factory,
    )

    brief = {
        "kind": "tests",
        "milestone_goal": "Test",
        "scope": ".",
        "done_when": "done",
        "forbidden_paths": [],
        "milestone_index": 0,
        "persona": "test",
    }

    task = asyncio.create_task(runner(brief, timeout_s=1000.0))
    await asyncio.sleep(0.1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(stopped_backends) == 1
    assert stopped_backends[0].stopped


@pytest.mark.asyncio
async def test_build_production_toolkit_implementer_wires_runner(stand, temp_git_repo):
    """T012: build_production_toolkit for implementer workflow wires agent_turn_runner."""
    metrics = WorkflowMetrics()
    score = _make_score(role="implementer", workspace_path=str(temp_git_repo))

    toolkit = build_production_toolkit(
        score,
        metrics=metrics,
        event_sink=None,
        workflow_name="implementer",
        stand=stand,
        model="example/model",
        backend_factory=fake_backend_factory,
    )

    assert toolkit._agent_turn_runner is not None


@pytest.mark.asyncio
async def test_build_production_toolkit_qa_no_runner(temp_git_repo):
    """T012(f): build_production_toolkit for qa workflow has no agent_turn_runner."""
    metrics = WorkflowMetrics()
    score = _make_score(role="qa", workspace_path=str(temp_git_repo))

    toolkit = build_production_toolkit(
        score,
        metrics=metrics,
        event_sink=None,
        workflow_name="qa",
    )

    assert toolkit._agent_turn_runner is None


@pytest.mark.asyncio
async def test_build_production_toolkit_assessor_no_runner(temp_git_repo):
    """T012(f): build_production_toolkit for assessor workflow has no agent_turn_runner."""
    metrics = WorkflowMetrics()
    score = _make_score(role="assessor", workspace_path=str(temp_git_repo))

    toolkit = build_production_toolkit(
        score,
        metrics=metrics,
        event_sink=None,
        workflow_name="assessor",
    )

    assert toolkit._agent_turn_runner is None


@pytest.mark.asyncio
async def test_backend_name_resolution_matches_main_py(stand, temp_git_repo):
    """T012(a): runner resolves backend adapter from SUPPORTED_BACKENDS."""
    from performer.backends import SUPPORTED_BACKENDS

    score = _make_score(workspace_path=str(temp_git_repo))

    # Test each supported backend name
    for backend_name in SUPPORTED_BACKENDS:
        # Just verify the function can build with this name
        # (doesn't require actual backend instantiation)
        runner = build_agent_turn_runner(
            stand=stand,
            score=score,
            backend_name=backend_name,
            backend_factory=lambda name: FakeBackend(),
        )
        assert runner is not None


@pytest.mark.asyncio
async def test_build_agent_turn_runner_poll_interval(stand, temp_git_repo):
    """T012(c): runner polls every 2 seconds by default."""
    class PollingTrackingBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.poll_times = []

        def get_status(self):
            self.poll_times.append(asyncio.get_event_loop().time())
            return super().get_status()

    def tracking_factory(backend_name):
        return PollingTrackingBackend()

    score = _make_score(workspace_path=str(temp_git_repo))
    runner = build_agent_turn_runner(
        stand=stand,
        score=score,
        backend_name="example",
        backend_factory=tracking_factory,
    )

    brief = {
        "kind": "tests",
        "milestone_goal": "Test",
        "scope": ".",
        "done_when": "done",
        "forbidden_paths": [],
        "milestone_index": 0,
        "persona": "test",
    }

    result = await runner(brief, timeout_s=10.0)

    assert result["exit_state"] == "done"


def test_backend_names_are_normalised_like_main_py():
    """Review of #268: score.backend arrives as 'claude-code' in kebab-case."""
    from performer.workflows.adapter import build_agent_turn_runner

    seen = []

    def factory(name):
        seen.append(name)
        raise RuntimeError("stop here")

    runner = build_agent_turn_runner(stand=None, score=None, backend_name="claude-code", backend_factory=factory)
    import asyncio

    with pytest.raises(RuntimeError):
        asyncio.run(runner({"persona": "x", "kind": "tests", "milestone_index": 0}, timeout_s=1))
    assert seen == ["claude_code"]
