"""Test that the closer workflow toolkit is wired correctly (no command runner, etc)."""
from __future__ import annotations

from performer.models import Score
from performer.workflows.adapter import build_production_toolkit
from performer.workflows.base import WorkflowMetrics


def _score(**kwargs) -> Score:
    defaults = {
        "title": "Test PR",
        "description": "Test description",
        "repo_url": "https://github.com/org/repo",
        "branch": "test-branch",
        "github_token": "ghp_test",
    }
    defaults.update(kwargs)
    return Score(**defaults)


class TestCloserToolkitBuilder:
    """Closer toolkit is write-free: no command_runner or screenshot_capture."""

    def test_closer_toolkit_has_model_call(self) -> None:
        """Closer workflow has model_call."""
        toolkit = build_production_toolkit(
            _score(),
            metrics=WorkflowMetrics(),
            event_sink=None,
            workflow_name="closer",
        )
        assert toolkit._model_call is not None

    def test_closer_toolkit_no_command_runner(self) -> None:
        """Closer workflow has no command_runner (write-free)."""
        toolkit = build_production_toolkit(
            _score(),
            metrics=WorkflowMetrics(),
            event_sink=None,
            workflow_name="closer",
        )
        assert toolkit._command_runner is None

    def test_closer_toolkit_no_screenshot_capture(self) -> None:
        """Closer workflow has no screenshot_capture."""
        toolkit = build_production_toolkit(
            _score(),
            metrics=WorkflowMetrics(),
            event_sink=None,
            workflow_name="closer",
        )
        assert toolkit._screenshot_capture is None

    def test_closer_toolkit_no_dom_reader(self) -> None:
        """Closer workflow has no dom_reader."""
        toolkit = build_production_toolkit(
            _score(),
            metrics=WorkflowMetrics(),
            event_sink=None,
            workflow_name="closer",
        )
        assert toolkit._dom_reader is None

    def test_closer_toolkit_has_metrics(self) -> None:
        """Closer workflow has metrics."""
        toolkit = build_production_toolkit(
            _score(),
            metrics=WorkflowMetrics(),
            event_sink=None,
            workflow_name="closer",
        )
        assert toolkit.metrics is not None
